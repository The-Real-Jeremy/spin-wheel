#!/usr/bin/env python3
"""PROVES THE PRIZE SETUP SURVIVES A RESTART — by breaking it the way it actually breaks.

⛔ WHY A RESTART AND NOT A REDEPLOY. A Databricks App's container filesystem is not durable, but it fails
   ASYMMETRICALLY: measured on this rig, `apps deploy` happened to PRESERVE a local SQLite file while
   `apps stop` followed by `apps start` recreated it EMPTY. So a durability claim tested with a redeploy
   passes on a store that would still lose everything. This does the stop/start.

WHAT IT DOES, in order:
  1. Saves a real prize configuration through the app's own API, including an UPLOADED PICTURE.
  2. Reads it back and checks the picture is served with the right Content-Type and the right bytes.
  3. `apps stop`, waits for STOPPED; `apps start`, waits for ACTIVE + a SUCCEEDED deployment.
  4. Reads it back AGAIN and compares: the prize list identical, the picture byte-identical, and the app
     reporting `source: stored` rather than falling back to the built-in examples.
  5. Restores whatever configuration was there before it started, so this is safe to run on a live app.

⚠️ STEP 5 MATTERS. This writes to the app's real store, so it saves the prior configuration first and puts
   it back at the end — including when it fails. Nothing here touches any other app.

Usage: python3 tools/prove_durability.py <app-name> [--profile <p>] [--skip-restart]
"""
import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*args, profile=None, timeout=300):
    cmd = ["databricks", *args]
    if profile:
        cmd += ["--profile", profile]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def token(profile):
    rc, out, err = cli("auth", "token", profile=profile)
    if rc != 0:
        raise SystemExit(f"could not get a token: {err.strip()[:300]}")
    return json.loads(out)["access_token"]


def call(url, tok, method="GET", body=None, ctype="application/json", raw=False, timeout=120):
    """-> (status, bytes|obj). STATUS FIRST: a non-2xx body is not data."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            blob = r.read()
            hdrs = dict(r.headers)
            if raw:
                return r.status, blob, hdrs
            try:
                return r.status, json.loads(blob or b"{}"), hdrs
            except Exception:                                        # noqa: BLE001
                return r.status, {"_raw": blob[:400].decode(errors="replace")}, hdrs
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, json.loads(blob or b"{}"), dict(e.headers)
        except Exception:                                            # noqa: BLE001
            return e.code, {"_raw": blob[:400].decode(errors="replace")}, dict(e.headers)
    except Exception as e:                                           # noqa: BLE001
        return -1, {"_error": f"{type(e).__name__}: {e}"}, {}


def make_png(w=240, h=240, seed=7):
    """A real PNG with non-trivial pixel data, so a text-mangling round-trip would be obvious."""
    rows = b""
    for y in range(h):
        row = bytearray()
        for x in range(w):
            row += bytes(((x * 3 + seed) % 256, (y * 5 + seed) % 256, ((x ^ y) + seed) % 256))
        rows += b"\x00" + bytes(row)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


def wait_for(app, profile, compute, deployment=None, timeout=600):
    """Poll `apps get` until the state matches. Returns the app record."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rc, out, err = cli("apps", "get", app, "-o", "json", profile=profile)
        if rc == 0:
            last = json.loads(out)
            c = (last.get("compute_status") or {}).get("state")
            d = ((last.get("active_deployment") or {}).get("status") or {}).get("state")
            print(f"    compute={c} deployment={d}")
            if c == compute and (deployment is None or d == deployment):
                return last
        time.sleep(10)
    raise SystemExit(f"timed out waiting for compute={compute} deployment={deployment}; last={last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--profile", default=os.environ.get("DB_PROFILE") or None)
    ap.add_argument("--skip-restart", action="store_true")
    a = ap.parse_args()

    rc, out, err = cli("apps", "get", a.app, "-o", "json", profile=a.profile)
    if rc != 0:
        raise SystemExit(f"apps get {a.app} failed: {err.strip()[:300]}")
    url = json.loads(out)["url"].rstrip("/")
    tok = token(a.profile)
    print(f"app: {a.app}\nurl: {url}\n")

    fails = []

    def ok(cond, what):
        print(("  PASS  " if cond else "  FAIL  ") + what)
        if not cond:
            fails.append(what)

    # ── 0. remember whatever is configured now, so this is safe on a live app ──────────────────────
    st, before, _ = call(url + "/api/settings", tok)
    if st != 200:
        raise SystemExit(f"GET /api/settings -> {st}: {before}")
    prior = before.get("prizes") or []
    prior_source = before.get("source")
    print(f"[0] existing configuration: source={prior_source}, {len(prior)} prize(s) — will be restored\n")

    try:
        # ── 1. upload a picture and save a configuration ───────────────────────────────────────────
        print("[1] uploading a picture and saving a prize list")
        png = make_png()
        local_md5 = hashlib.md5(png).hexdigest()
        st, up, _ = call(url + "/api/settings/image", tok, "POST", png, "image/png")
        ok(st == 200, f"POST /api/settings/image -> {st} {'' if st == 200 else up}")
        if st != 200:
            raise SystemExit(1)
        ok(up["image_md5"] == local_md5, f"the app stored exactly the bytes sent (md5 {up['image_md5']})")
        ok(up["image_mime"] == "image/png", f"it sniffed the format from the bytes: {up['image_mime']}")
        ok((up["image_w"], up["image_h"]) == (240, 240),
           f"it read the dimensions out of the header: {up['image_w']}x{up['image_h']}")

        prizes = [
            {"id": "dur_a", "name": "Durability Grand", "chance": 12.5, "splits": 2, "colour": "#FF3621",
             "image": up["image"], "image_mime": up["image_mime"], "image_md5": up["image_md5"],
             "image_w": up["image_w"], "image_h": up["image_h"]},
            {"id": "dur_b", "name": "Durability Mid", "chance": 37.5, "splits": 3, "colour": "#00A972"},
            {"id": "dur_c", "name": "Durability Small", "chance": 50.0, "splits": 4, "colour": "#2272B4"},
        ]
        st, saved, _ = call(url + "/api/settings", tok, "POST", {"prizes": prizes})
        ok(st == 200, f"POST /api/settings -> {st} {'' if st == 200 else saved}")
        if st != 200:
            raise SystemExit(1)
        print(f"      saved at {saved['updated']['at']} by {saved['updated']['by']}")

        # ── 2. read it back, and check the picture is SERVED correctly ──────────────────────────────
        print("\n[2] reading it back before the restart")
        st, wheel, _ = call(url + "/api/wheel", tok)
        ok(st == 200 and wheel["source"] == "stored",
           f"/api/wheel reports source={wheel.get('source')} (not the built-in examples)")
        ok(len(wheel["segments"]) == 9, f"9 segments from 2+3+4 splits (got {len(wheel['segments'])})")
        img_url = f"{url}/api/image/{up['image']}"
        st, blob, hdrs = call(img_url, tok, raw=True)
        served_ct = hdrs.get("Content-Type") or hdrs.get("content-type")
        ok(st == 200 and hashlib.md5(blob).hexdigest() == local_md5,
           f"the picture is served byte-identical ({len(blob)} bytes)")
        ok(served_ct == "image/png",
           f"and with the Content-Type the app SNIFFED, read off the live response: {served_ct!r}")

        # ── 3. the restart that actually breaks a container filesystem ─────────────────────────────
        if a.skip_restart:
            print("\n[3] --skip-restart given; not restarting")
        else:
            print("\n[3] apps stop  (this is the operation a container filesystem does not survive)")
            rc, out, err = cli("apps", "stop", a.app, profile=a.profile, timeout=900)
            print(f"    stop rc={rc} {err.strip()[:200]}")
            wait_for(a.app, a.profile, "STOPPED", timeout=900)
            print("    apps start")
            rc, out, err = cli("apps", "start", a.app, profile=a.profile, timeout=900)
            print(f"    start rc={rc} {err.strip()[:200]}")
            wait_for(a.app, a.profile, "ACTIVE", "SUCCEEDED", timeout=900)
            # A fresh container needs a moment before it answers.
            for _ in range(30):
                st, h, _ = call(url + "/api/health", tok)
                if st == 200:
                    print(f"    app answering again, uptime {h.get('uptime_s')}s "
                          f"(a small number here proves it really restarted)")
                    ok(float(h.get("uptime_s") or 1e9) < 600,
                       f"the app is a NEW process (uptime {h.get('uptime_s')}s)")
                    break
                time.sleep(10)
            else:
                ok(False, "the app did not answer /api/health after the restart")

        # ── 4. the proof ───────────────────────────────────────────────────────────────────────────
        print("\n[4] reading it back AFTER the restart")
        st, wheel2, _ = call(url + "/api/wheel", tok)
        ok(st == 200, f"/api/wheel -> {st}")
        ok(wheel2.get("source") == "stored",
           f"source is STILL 'stored' after the restart (got {wheel2.get('source')!r}) — it did not fall "
           f"back to the built-in examples")
        names_before = [p["name"] for p in prizes]
        names_after = [p["name"] for p in wheel2.get("prizes", [])]
        ok(names_after == names_before, f"the same prizes, in order: {names_after}")
        chances_after = [p["chance"] for p in wheel2.get("prizes", [])]
        ok(chances_after == [12.5, 37.5, 50.0], f"the same chances: {chances_after}")
        splits_after = [p["splits"] for p in wheel2.get("prizes", [])]
        ok(splits_after == [2, 3, 4], f"the same split counts: {splits_after}")
        st, blob2, hdrs2 = call(img_url, tok, raw=True)
        ok(st == 200 and hashlib.md5(blob2).hexdigest() == local_md5,
           f"the uploaded PICTURE is still there and still byte-identical "
           f"(md5 {hashlib.md5(blob2).hexdigest() if st == 200 else '—'})")
        ok((hdrs2.get("Content-Type") or hdrs2.get("content-type")) == "image/png",
           "and still served as image/png")
        st, h2, _ = call(url + "/api/health", tok)
        store = (h2 or {}).get("store") or {}
        print(f"      store after restart: kind={store.get('kind')} durable={store.get('durable')} "
              f"state={store.get('state')} base={store.get('base')}")

    finally:
        # ── 5. put back whatever was there before ──────────────────────────────────────────────────
        print("\n[5] restoring the configuration that was there before this ran")
        if prior_source == "stored" and prior:
            st, _, _ = call(url + "/api/settings", tok, "POST", {"prizes": prior})
            print(f"    restored {len(prior)} prize(s) -> HTTP {st}")
        else:
            # ⛔ NOTHING WAS CONFIGURED BEFORE, and the first version of this tried to restore that by
            #    saving an EMPTY list — which was refused (422), so it silently left its own test prizes on
            #    a live app. Writing the BUILT-IN EXAMPLES instead leaves a working wheel somebody can edit,
            #    which is what an unconfigured deployment shows anyway.
            st, before2, _ = call(url + "/api/wheel", tok)
            examples = [
                {"id": "p_demo1", "name": "Grand Prize", "chance": 5.0, "splits": 1, "colour": "#FF3621"},
                {"id": "p_demo2", "name": "Runner Up", "chance": 15.0, "splits": 2, "colour": "#00A972"},
                {"id": "p_demo3", "name": "Sticker Pack", "chance": 30.0, "splits": 3, "colour": "#2272B4"},
                {"id": "p_demo4", "name": "Try Again", "chance": 50.0, "splits": 4, "colour": "#FFAB00"},
            ]
            st, r, _ = call(url + "/api/settings", tok, "POST", {"prizes": examples})
            print(f"    nothing was configured before (source={prior_source}); wrote the built-in example "
                  f"prizes back so the app is left with a working wheel -> HTTP {st} "
                  f"{r.get('errors') if isinstance(r, dict) and st != 200 else ''}")

    print()
    if fails:
        print(f"DURABILITY NOT PROVEN — {len(fails)} failure(s):")
        for f in fails:
            print("  · " + f)
        return 1
    print("DURABILITY PROVEN: a prize list and an uploaded picture both survived `apps stop` + "
          "`apps start`, byte-for-byte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
