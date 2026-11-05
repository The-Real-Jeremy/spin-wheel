#!/usr/bin/env python3
"""SPIN TO WIN — a 3D prize wheel, served by the Python standard library.

The game page holds the wheel; to its right, an enlarged picture of whatever the pointer is currently over,
which keeps changing as the wheel turns and settles on the winner. A settings page adds and removes prizes,
sets each one's chance of winning, uploads its picture, and says how many segments that prize is split into.

NO THIRD-PARTY PACKAGES, NO requirements.txt, NO CDN, NO WEB FONT, NO OUTBOUND CALL except to the
workspace's own API. That is not minimalism for its own sake: a blocked CDN host does not fail fast, it
HANGS, and the page then paints nothing at all — which at a hosted event in front of an audience is the
worst failure available. Everything is vendored and same-origin, and `tools/verify_served.py` asserts it
from the running app.

WHERE TO READ NEXT
  lib/model.py  the odds, the geometry, and why the draw decides before the animation starts
  lib/store.py  why the prize list lives in the workspace and not in this container
  static/js/wheel.js  the renderer, and why the enlarged panel is derived from the wheel's rotation
"""
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib import images, model, store as storelib, wheel as wheellib    # noqa: E402
from lib.config import Config                                          # noqa: E402

STATIC = os.path.join(HERE, "static")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json",
        ".svg": "image/svg+xml", ".ico": "image/x-icon", ".png": "image/png",
        ".webp": "image/webp", ".txt": "text/plain; charset=utf-8"}

CFG = Config()
STORE = storelib.build(CFG)
BOOTED_AT = time.time()


def _source_fingerprint():
    """md5 over the server's OWN source as THIS container has it on disk.

    The static assets can be fetched and compared byte-for-byte, but `app.py` and `lib/` are never served —
    so without this a verifier can prove the browser got the right CSS while the server runs last week's
    logic. `BUILD_ID` cannot do this job: it is set at deploy time and proves a restart, not which code.
    """
    h = hashlib.md5()
    names = ["app.py"] + sorted("lib/" + f for f in os.listdir(os.path.join(HERE, "lib"))
                                if f.endswith(".py"))
    for rel in names:
        try:
            with open(os.path.join(HERE, rel), "rb") as f:
                h.update(rel.encode())
                h.update(f.read())
        except OSError:
            h.update(b"?" + rel.encode())
    return h.hexdigest(), names


SOURCE_MD5, SOURCE_FILES = _source_fingerprint()


# ====================================================================== request handling
class Handler(BaseHTTPRequestHandler):
    server_version = "spin-wheel"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- helpers
    def log_message(self, fmt, *args):
        # One line per request, on stdout, which is where `databricks apps logs` reads from.
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _viewer(self):
        """Who Databricks Apps says is looking. Used only to stamp changes and spins."""
        h = self.headers
        email = (h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username") or "").strip()
        return email or "anonymous"

    def _client_ip(self):
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return fwd or self.client_address[0]

    def _https(self):
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _send(self, status, body, ctype="application/json", extra=None, cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        # Same-origin only, and no inline handlers anywhere in the app — so a CSP this strict costs
        # nothing and removes a whole class of problem from a page that accepts uploads.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                         "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self, cap=256 * 1024):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > cap:
            return None
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:                                              # noqa: BLE001
            return None

    def _raw_body(self, cap):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > cap:
            return None
        return self.rfile.read(n)

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        try:
            u = urlparse(self.path)
            path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
            fn = ROUTES.get((method, path))
            if fn:
                return fn(self, q)
            if method == "GET" and path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if method == "GET" and path.startswith("/api/image/"):
                return self._image(path[len("/api/image/"):])
            self._send(404, {"error": f"no route for {method} {path}"})
        except BrokenPipeError:
            pass                                    # a browser navigating away mid-response
        except Exception:                                              # noqa: BLE001
            traceback.print_exc()
            try:
                self._send(500, {"error": "internal error — see the app log"})
            except Exception:                                          # noqa: BLE001
                pass

    # ---------------------------------------------------------------- static assets
    def _static(self, rel):
        # Resolve and confine: a path that escapes STATIC is a 404, not a traversal.
        target = os.path.realpath(os.path.join(STATIC, rel))
        if not target.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(target):
            return self._send(404, {"error": "not found"})
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            data = f.read()
        etag = '"%s"' % hashlib.md5(data).hexdigest()
        if (self.headers.get("If-None-Match") or "") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, data, MIME.get(ext, "application/octet-stream"),
                   extra=[("ETag", etag)], cache="no-cache")

    # ---------------------------------------------------------------- prize images
    def _image(self, name):
        """Served with the mime that was SNIFFED from the bytes at upload time, never from the filename.

        Content-addressed: the page requests `?v=<md5>`, so a changed picture busts the cache and an
        unchanged one is fetched once. That matters for smoothness — an image request landing mid-spin is
        a frame budget spent on the network.
        """
        name = os.path.basename(name)
        loaded = wheellib.load(STORE)
        meta = next((p for p in loaded["prizes"] if p.get("image") == name), None)
        data, state = STORE.read_image(name, expect_md5=(meta or {}).get("image_md5") or None)
        if state == "absent":
            return self._send(404, {"error": "no such image"})
        if state != "present" or data is None:
            # ⭐ NOT a 404: "I could not look" is a different answer from "it is not there", and a 404 here
            #    would make a permissions problem look like a missing upload.
            return self._send(503, {"error": "the image store could not be read"})
        ctype = (meta or {}).get("image_mime")
        if ctype not in images.ALLOWED:
            # Re-sniff rather than trust a stored value that is not in the allow-list.
            try:
                ctype = images.sniff(data)[0]
            except images.Rejected:
                return self._send(415, {"error": "stored object is not a usable image"})
        self._send(200, data, ctype, cache="public, max-age=31536000, immutable")

    # ---------------------------------------------------------------- pages
    def _page(self, filename):
        with open(os.path.join(STATIC, filename), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8", cache="no-cache")


# ====================================================================== API endpoints
def api_wheel(h, q):
    """Everything the game page needs to draw: the prizes, THE SEGMENT GEOMETRY, and the states."""
    loaded = wheellib.load(STORE)
    h._send(200, {
        "config": CFG.public(),
        "prizes": loaded["prizes"],
        "segments": loaded["segments"],
        "meta": loaded["meta"],
        "source": loaded["source"],
        "store": {"kind": loaded["store_kind"], "durable": loaded["durable"],
                  "state": loaded["store_state"], "detail": loaded["store_detail"]},
        "warnings": loaded["warnings"],
        "errors": loaded["errors"],
        "updated": loaded["updated"],
    })


def api_draw(h, q):
    """THE DRAW — performed when the operator/contestant presses STOP, not when the wheel starts.

    ⭐ WHY AT THE STOP AND NOT AT THE START (the owner's model, 2026-09-23). It reads as honest: the press is
       what produces the outcome. And a winner drawn at spin-up would have to live somewhere for the whole
       spin — anywhere the client can see it, the prize is inspectable before the wheel stops, with people
       standing at the laptop. Drawing here removes that entirely, at no cost: the distribution is
       identical either way (see lib/model.py).

    ⭐ IDEMPOTENT BY TOKEN. The caller sends a `token` it generated for this one press; the same token
       always returns the SAME result and draws once. The page also guards its own button, but that guard
       cannot survive a retried request or a double-fired event — so idempotency is a property of the
       protocol here, not only of the click handler. A physical button pressed by an excited stranger gets
       hit twice.
    """
    body = h._json_body(4096) or {}
    token = str(body.get("token") or "").strip()[:80]
    payload, err = wheellib.draw_once(STORE, token=token, who=h._viewer())
    if err:
        return h._send(409, {"error": err})
    h._send(200, payload)


def api_history(h, q):
    rows, state = STORE.read_history()
    try:
        limit = max(1, min(200, int((q.get("limit") or ["25"])[0])))
    except ValueError:
        limit = 25
    h._send(200, {"state": state, "count": len(rows), "rows": rows[:limit]})


def api_health(h, q):
    """Reports RESOLVED PATHS AND REAL SYSCALLS, not intentions — which is what makes it evidence.

    `source_md5` is the point of the endpoint: it is a hash of the server's own files as this container has
    them, so a verifier can tell which CODE is answering. A build stamp cannot do that (it is set at deploy
    time and a partial sync passes it).
    """
    loaded = wheellib.load(STORE)
    st = STORE.selftest() if (q.get("selftest") or [""])[0] == "1" else None
    h._send(200, {
        "ok": True,
        "app": CFG.app_name,
        "build_id": CFG.build_id,
        "source_md5": SOURCE_MD5,
        "source_files": len(SOURCE_FILES),
        "uptime_s": round(time.time() - BOOTED_AT, 1),
        "python": sys.version.split()[0],
        "store": {"kind": STORE.kind, "durable": getattr(STORE, "durable", False),
                  "base": getattr(STORE, "base", None), "state": loaded["store_state"],
                  "detail": loaded["store_detail"], "available": STORE.available()},
        "prizes": {"source": loaded["source"], "count": len(loaded["prizes"]),
                   "segments": loaded["meta"]["segment_count"],
                   "declared_total": loaded["meta"]["declared_total"],
                   "with_images": sum(1 for p in loaded["prizes"] if p.get("image"))},
        # There is no settings gate: the page is open by design (owner, 2026-09-23). Stated here rather
        # than omitted, so nothing reading this endpoint has to infer it.
        "settings_gate": {"present": False,
                          "reason": "removed on request; runs on the hosts' own laptops"},
        "selftest": st,
    })


def api_settings_get(h, q):
    loaded = wheellib.load(STORE)
    h._send(200, {
        "config": CFG.public(),
        "prizes": loaded["prizes"],
        "meta": loaded["meta"],
        "source": loaded["source"],
        "store": {"kind": loaded["store_kind"], "durable": loaded["durable"],
                  "state": loaded["store_state"], "detail": loaded["store_detail"],
                  "base": getattr(STORE, "base", None)},
        "warnings": loaded["warnings"],
        "errors": loaded["errors"],
        "updated": loaded["updated"],
        "limits": {"max_prizes": model.MAX_PRIZES, "max_splits": model.MAX_SPLITS,
                   "max_segments": model.MAX_SEGMENTS, "max_image_bytes": images.MAX_BYTES,
                   "max_image_dim": images.MAX_DIM, "palette": model.PALETTE},
    })


def api_preview(h, q):
    """The geometry these edits WOULD produce, without saving. Gated, because it reveals the prize list.

    ⭐ THE SETTINGS PAGE PREVIEWS THE SERVER'S OWN GEOMETRY rather than computing a second copy in JS. That
    is what lets the host watch "as evenly spaced as possible" actually happen while they type, and it means
    the preview cannot disagree with the wheel they are about to save. Validation problems come back too,
    but nothing is stored and a bad total is NOT a refusal here — it is a refusal at save time.
    """
    body = h._json_body() or {}
    raw = body.get("prizes")
    if not isinstance(raw, list):
        return h._send(400, {"error": "expected a prizes array"})
    prizes = [model.coerce_prize(p, i) for i, p in enumerate(raw)]
    segments, meta = model.build_segments(prizes)
    errors, warnings = (model.validate(prizes) if prizes else ([], []))
    h._send(200, {"prizes": prizes, "segments": segments, "meta": meta,
                  "errors": errors, "warnings": warnings})


def api_settings_save(h, q):
    body = h._json_body() or {}
    raw = body.get("prizes")
    if not isinstance(raw, list):
        return h._send(400, {"error": "expected a prizes array"})
    ok, errors, warnings, loaded = wheellib.save(STORE, raw, who=h._viewer())
    if not ok:
        return h._send(422, {"ok": False, "errors": errors, "warnings": warnings})
    # Any image object no longer referenced by a prize is removed, so a long event does not accumulate
    # orphans in the workspace folder. A delete that fails is reported, never silent.
    keep = {p.get("image") for p in loaded["prizes"] if p.get("image")}
    removed, failed = [], []
    listing = STORE.list_objects(storelib.IMAGE_DIR)
    if listing is not None:
        for obj in listing:
            name = os.path.basename(obj.get("path") or "")
            if name and name not in keep and not name.startswith("_"):
                okd, why = STORE.delete_image(name)
                (removed if okd else failed).append(name if okd else f"{name}: {why}")
    h._send(200, {"ok": True, "warnings": warnings, "prizes": loaded["prizes"],
                  "meta": loaded["meta"], "updated": loaded["updated"],
                  "orphans_removed": removed, "orphans_failed": failed})


def api_settings_image(h, q):
    """Raw image bytes in the request body. No multipart — the page has a Blob from `canvas.toBlob`.

    What happens to the bytes: `images.sniff` reads the real format and dimensions out of the header, the
    caps are enforced, and the object is stored under a name derived from its own md5 so two prizes with
    the same picture share one object and a replaced picture never collides with a cached URL.
    """
    data = h._raw_body(images.MAX_BYTES + 4096)
    if not data:
        return h._send(400, {"error": f"no image bytes, or more than "
                                      f"{images.MAX_BYTES // 1024 // 1024} MB of them"})
    try:
        info = images.describe(data)
    except images.Rejected as e:
        return h._send(415, {"error": str(e)})
    name = info["image_md5"] + images.extension(info["image_mime"])
    ok, why = STORE.write_image(name, data)
    if not ok:
        return h._send(503, {"error": f"the picture was NOT stored: {why}"})
    # Read it straight back and compare, so "uploaded" means the bytes are retrievable and identical
    # rather than that a write returned 200.
    got, state = STORE.read_image(name, expect_md5=info["image_md5"])
    if state != "present" or hashlib.md5(got or b"").hexdigest() != info["image_md5"]:
        return h._send(503, {"error": "the picture was written but did not read back identical; "
                                      "it has NOT been attached to the prize."})
    h._send(200, {"ok": True, "image": name, **info})


def api_settings_selftest(h, q):
    h._send(200, STORE.selftest())


def api_history_clear(h, q):
    ok, why = STORE.clear_history()
    h._send(200 if ok else 503, {"ok": ok, "error": why})


ROUTES = {
    ("GET", "/"): lambda h, q: h._page("index.html"),
    ("GET", "/settings"): lambda h, q: h._page("settings.html"),
    ("GET", "/api/wheel"): api_wheel,
    ("POST", "/api/draw"): api_draw,
    ("GET", "/api/history"): api_history,
    ("POST", "/api/history/clear"): api_history_clear,
    ("GET", "/api/health"): api_health,
    ("GET", "/api/settings"): api_settings_get,
    ("POST", "/api/settings"): api_settings_save,
    ("POST", "/api/settings/image"): api_settings_image,
    ("POST", "/api/preview"): api_preview,
    ("GET", "/api/settings/selftest"): api_settings_selftest,
}


def main():
    print(f"[spin-wheel] app={CFG.app_name} build={CFG.build_id} source_md5={SOURCE_MD5}")
    print(f"[spin-wheel] store={STORE.kind} durable={getattr(STORE, 'durable', False)} "
          f"base={getattr(STORE, 'base', None)}")
    print("[spin-wheel] settings page: OPEN (no password, by design)")
    print(f"[spin-wheel] listening on 0.0.0.0:{CFG.port}")
    sys.stdout.flush()
    ThreadingHTTPServer(("0.0.0.0", CFG.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
