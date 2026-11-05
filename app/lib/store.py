"""DURABLE STORAGE — the prize configuration, the uploaded pictures, and the spin log.

## WHY THIS IS NOT A FILE IN THE CONTAINER

A Databricks App's local filesystem does not survive the app being restarted. Measured on this rig
(measured): `apps deploy` happened to preserve a local SQLite
file, and `apps stop` followed by `apps start` recreated it EMPTY. For a prize wheel that is the worst
possible failure — the host configures ten prizes and their photos, the app is restarted at some point
before the event, and the wheel comes up with the built-in placeholders. Nothing errors.

So the authoritative store is the **app's own service principal's workspace home**,
`/Users/<DATABRICKS_CLIENT_ID>/<app-name>-state/`, reached over `/api/2.0/workspace/{mkdirs,import,export}`
with the `DATABRICKS_CLIENT_ID`/`SECRET` that Apps injects into every container. That needs **no grant, no
group membership and no extra `user_api_scopes` entry** — measured, both directions. It is also
*auditable*: an admin can list, read and back up a workspace file, and cannot see a file inside a container.

Round-trip byte-identity for BINARY objects was measured before this was designed (2026-09-23; re-run
`python3 tools/prove_durability.py` to check a deployment): a PNG imported with `format: AUTO` comes back byte-identical from
`export?direct_download=true`, as `object_type: FILE`, with `.png`, `.bin` and no extension alike. The
object ceiling is **exactly 10,485,760 bytes** — hence `images.MAX_BYTES` at 2 MB, comfortably under.

⚠️ ONE EXTENSION TO AVOID, and it is not obvious: `databricks sync` and `workspace import` turn a
   `.py`/`.sql`/`.ipynb` into a NOTEBOOK object at an EXTENSIONLESS path. Image extensions are unaffected
   (checked, not assumed), but nothing here should ever be given one of those.

## THE THREE STATES, AND WHY THE THIRD ONE HAS TO EXIST

`read_config()` answers **present** / **absent** / **could-not-determine** (denied, unreachable, malformed).
A store that cannot be read must never render as empty: "nothing is configured" and "I could not look" are
different answers, and code that conflates them shows the host a wheel of placeholders on a stage while
their real configuration sits safely in the workspace. The game page renders all three differently and
`/api/health` reports which one is in force.

⛔ `mkdirs` RETURNS 200 FOR A DIRECTORY THE PRINCIPAL CANNOT WRITE TO, and the following `import` then
   answers **404** — a denial wearing a 404, right after a success that meant nothing. Only the import's
   own status is believed here, and a 404 straight after creating the parent is reported as a PERMISSION
   problem, because reporting it as "missing" sends the reader hunting for a folder that is there.
"""
import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_NAME = "config.json"
HISTORY_NAME = "history.json"
IMAGE_DIR = "images"
HISTORY_CAP = 500                # newest first; a hosted event does not need more and one object stays small
VERSION = 1


# ====================================================================== the workspace-backed store
class WorkspaceStore:
    """Durable. Acts as the app's own service principal (or, on a laptop, a dev token)."""

    kind = "workspace"
    durable = True

    def __init__(self, host, base_path, client_id=None, client_secret=None, token=None, ttl=5.0):
        self.host = (host or "").rstrip("/")
        self.base = (base_path or "").rstrip("/")
        self.client_id, self.client_secret, self.dev_token = client_id, client_secret, token
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cfg_cache = None                  # (read_at, doc, state, detail)
        self._img_cache = {}                    # name -> (md5, bytes)  — bounded by IMG_CACHE_MAX
        self._tok_cache = None                  # (expires_at, token)
        self.last_error = None

    IMG_CACHE_MAX = 40

    # ---------------------------------------------------------------- plumbing
    def available(self):
        return bool(self.host and self.base and
                    (self.dev_token or (self.client_id and self.client_secret)))

    def _token(self):
        if self.dev_token:
            return self.dev_token
        now = time.time()
        if self._tok_cache and self._tok_cache[0] > now + 60:
            return self._tok_cache[1]
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.host + "/oidc/v1/token", data=b"grant_type=client_credentials&scope=all-apis",
            method="POST", headers={"Authorization": "Basic " + basic,
                                   "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
        tok = payload["access_token"]
        self._tok_cache = (now + float(payload.get("expires_in") or 3600), tok)
        return tok

    def _call(self, method, path, body=None, raw=False, timeout=60):
        """-> (status, text_or_bytes). STATUS FIRST, ALWAYS.

        A non-2xx body is not data. The token call is inside the try because it is a network call like any
        other and this runs at boot — an exception escaping here would take the app down before it served
        a page, over a settings file.
        """
        try:
            auth = "Bearer " + self._token()
        except Exception as e:                                       # noqa: BLE001
            return -1, f"could not get a service-principal token: {type(e).__name__}: {e}"
        req = urllib.request.Request(
            self.host + path, data=(json.dumps(body).encode() if body is not None else None),
            method=method, headers={"Authorization": auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, (r.read() if raw else r.read().decode())
        except urllib.error.HTTPError as e:
            blob = e.read()
            return e.code, (blob[:600] if raw else blob.decode(errors="replace")[:600])
        except Exception as e:                                       # noqa: BLE001
            return -1, f"{type(e).__name__}: {e}"

    def _get(self, rel, raw=False):
        p = f"{self.base}/{rel}"
        return self._call("GET", "/api/2.0/workspace/export?direct_download=true&path=" +
                          urllib.parse.quote(p), raw=raw)

    def _put(self, rel, data):
        """-> (ok, detail). `data` is bytes."""
        p = f"{self.base}/{rel}"
        parent = p.rsplit("/", 1)[0]
        self._call("POST", "/api/2.0/workspace/mkdirs", {"path": parent})
        st, text = self._call("POST", "/api/2.0/workspace/import", {
            "path": p, "format": "AUTO", "overwrite": True,
            "content": base64.b64encode(data).decode()})
        if st == 200:
            return True, None
        if st == 404:
            return False, (f"could not write {p} (HTTP 404 immediately after creating the folder, which "
                           f"is how this API reports 'not allowed'). The app's service principal cannot "
                           f"write there.")
        return False, f"could not write {p} (HTTP {st}): {str(text)[:200]}"

    def _delete(self, rel):
        p = f"{self.base}/{rel}"
        st, text = self._call("POST", "/api/2.0/workspace/delete", {"path": p, "recursive": False})
        return st in (200, 404), (None if st in (200, 404) else f"HTTP {st}: {str(text)[:160]}")

    def list_objects(self, rel=""):
        p = self.base + (f"/{rel}" if rel else "")
        st, text = self._call("GET", "/api/2.0/workspace/list?path=" + urllib.parse.quote(p))
        if st != 200:
            return None
        try:
            return json.loads(text or "{}").get("objects") or []
        except Exception:                                            # noqa: BLE001
            return None

    # ---------------------------------------------------------------- config document
    def read_config(self, force=False):
        """-> (doc, state, detail). state: present | absent | denied | unavailable | error."""
        if not self.available():
            return {}, "unavailable", ("this container has no service-principal credentials, so there is "
                                       "nowhere durable to keep the prize list.")
        if not force and self._cfg_cache and time.time() - self._cfg_cache[0] < self.ttl:
            _, d, s, det = self._cfg_cache
            return d, s, det
        st, text = self._get(CONFIG_NAME)
        if st == 200:
            try:
                doc = json.loads(text or "{}")
                if not isinstance(doc, dict):
                    raise ValueError("not an object")
                state, detail = "present", None
            except Exception as e:                                   # noqa: BLE001
                doc, state, detail = {}, "error", f"the stored prize list is not valid json ({e})"
        elif st == 404:
            doc, state, detail = {}, "absent", "no prizes have been configured on this deployment yet."
        elif st in (401, 403):
            doc, state, detail = {}, "denied", (
                f"the app's service principal may not read {self.base}/{CONFIG_NAME} (HTTP {st}). "
                f"Settings cannot survive a restart until that is fixed.")
        else:
            doc, state, detail = {}, "error", f"HTTP {st}: {str(text)[:200]}"
        self.last_error = detail if state in ("denied", "error") else None
        self._cfg_cache = (time.time(), doc, state, detail)
        return doc, state, detail

    def write_config(self, doc):
        """-> (ok, detail). Never raises; a failed save must be reportable in the UI."""
        if not self.available():
            return False, ("this container has no service-principal credentials, so a change could not be "
                           "made to survive a restart — it was NOT saved.")
        payload = dict(doc)
        payload["_version"] = VERSION
        body = json.dumps(payload, indent=1, sort_keys=True).encode()
        with self._lock:
            ok, why = self._put(CONFIG_NAME, body)
            if ok:
                self._cfg_cache = (time.time(), payload, "present", None)
            else:
                self.last_error = why
                self._cfg_cache = None
        return ok, why

    # ---------------------------------------------------------------- images
    def read_image(self, name, expect_md5=None):
        """-> (bytes, state). state: present | absent | error. Cached in memory by md5."""
        if not name or "/" in name or name.startswith("."):
            return None, "absent"
        hit = self._img_cache.get(name)
        if hit and (expect_md5 is None or hit[0] == expect_md5):
            return hit[1], "present"
        st, data = self._get(f"{IMAGE_DIR}/{name}", raw=True)
        if st == 404:
            return None, "absent"
        if st != 200 or not isinstance(data, bytes):
            return None, "error"
        if len(self._img_cache) >= self.IMG_CACHE_MAX:
            self._img_cache.clear()
        self._img_cache[name] = (hashlib.md5(data).hexdigest(), data)
        return data, "present"

    def write_image(self, name, data):
        ok, why = self._put(f"{IMAGE_DIR}/{name}", data)
        if ok:
            self._img_cache[name] = (hashlib.md5(data).hexdigest(), data)
        return ok, why

    def delete_image(self, name):
        if not name or "/" in name:
            return True, None
        self._img_cache.pop(name, None)
        return self._delete(f"{IMAGE_DIR}/{name}")

    # ---------------------------------------------------------------- spin history
    def read_history(self):
        st, text = self._get(HISTORY_NAME)
        if st != 200:
            return ([], "absent" if st == 404 else "error")
        try:
            rows = json.loads(text or "[]")
            return (rows if isinstance(rows, list) else []), "present"
        except Exception:                                            # noqa: BLE001
            return [], "error"

    def append_history(self, row):
        """Read-modify-write on one object. Serialised by `self._lock` within this container.

        ⚠️ HONEST LIMITATION: that lock is per process, so two containers spinning at the same instant
        could lose a row. Apps runs a single instance, and a prize wheel has one operator at one wheel, so
        the exposure is a lost LOG LINE and never a wrong RESULT — the result is decided and returned
        before this is called, and the wheel does not read this back. Stated rather than hidden.
        """
        with self._lock:
            rows, _ = self.read_history()
            rows.insert(0, row)
            del rows[HISTORY_CAP:]
            return self._put(HISTORY_NAME, json.dumps(rows, indent=1).encode())

    def clear_history(self):
        with self._lock:
            return self._put(HISTORY_NAME, b"[]")

    # ---------------------------------------------------------------- self-test (real syscalls)
    def selftest(self):
        """Round-trips a text AND a binary object through the real API, as this container's identity.

        This is what turns "durable storage is configured" into evidence. `/api/health` reports it, and
        the deploy verifier reads it — because an app that *believes* it has durable storage and a 403 it
        has never exercised looks identical to one that has.
        """
        out = {"mode": self.kind, "base": self.base, "available": self.available()}
        if not self.available():
            out["ok"] = False
            out["detail"] = "no credentials in this container"
            return out
        probe = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4      # binary, with bytes a text path mangles
        name = "_selftest.bin"
        ok, why = self._put(f"{IMAGE_DIR}/{name}", probe)
        out["write"] = ok
        if not ok:
            out.update(ok=False, detail=why)
            return out
        st, got = self._get(f"{IMAGE_DIR}/{name}", raw=True)
        out["read_status"] = st
        out["byte_identical"] = (st == 200 and got == probe)
        out["ok"] = bool(out["byte_identical"])
        if not out["ok"]:
            out["detail"] = (f"wrote {len(probe)} bytes and read back "
                             f"{len(got) if isinstance(got, bytes) else '?'} (HTTP {st})")
        self._delete(f"{IMAGE_DIR}/{name}")
        return out


# ====================================================================== the laptop / no-credentials store
class LocalStore:
    """A directory. Used only when there are NO workspace credentials — a laptop, or a container that has
    been denied. It is EPHEMERAL and every surface that shows it says so: `durable = False` drives a
    warning banner on both pages and a `durable: false` in `/api/health`.

    It exists so `python3 app/app.py` works on a laptop with nothing configured, not as a deployment mode.
    """

    kind = "local"
    durable = False

    def __init__(self, root):
        self.base = root
        self._lock = threading.Lock()
        self.last_error = None
        os.makedirs(os.path.join(root, IMAGE_DIR), exist_ok=True)

    def available(self):
        return True

    def _p(self, *bits):
        return os.path.join(self.base, *bits)

    def read_config(self, force=False):
        try:
            with open(self._p(CONFIG_NAME), "rb") as f:
                return json.loads(f.read().decode() or "{}"), "present", None
        except FileNotFoundError:
            return {}, "absent", "no prizes have been configured on this deployment yet."
        except Exception as e:                                        # noqa: BLE001
            return {}, "error", f"{type(e).__name__}: {e}"

    def write_config(self, doc):
        payload = dict(doc)
        payload["_version"] = VERSION
        try:
            with self._lock, open(self._p(CONFIG_NAME), "wb") as f:
                f.write(json.dumps(payload, indent=1, sort_keys=True).encode())
            return True, None
        except Exception as e:                                        # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def read_image(self, name, expect_md5=None):
        if not name or "/" in name:
            return None, "absent"
        try:
            with open(self._p(IMAGE_DIR, name), "rb") as f:
                return f.read(), "present"
        except FileNotFoundError:
            return None, "absent"
        except Exception:                                             # noqa: BLE001
            return None, "error"

    def write_image(self, name, data):
        try:
            with open(self._p(IMAGE_DIR, name), "wb") as f:
                f.write(data)
            return True, None
        except Exception as e:                                        # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def delete_image(self, name):
        try:
            os.remove(self._p(IMAGE_DIR, name))
        except FileNotFoundError:
            pass
        except Exception as e:                                        # noqa: BLE001
            return False, str(e)
        return True, None

    def read_history(self):
        try:
            with open(self._p(HISTORY_NAME), "rb") as f:
                rows = json.loads(f.read().decode() or "[]")
                return (rows if isinstance(rows, list) else []), "present"
        except FileNotFoundError:
            return [], "absent"
        except Exception:                                             # noqa: BLE001
            return [], "error"

    def append_history(self, row):
        with self._lock:
            rows, _ = self.read_history()
            rows.insert(0, row)
            del rows[HISTORY_CAP:]
            try:
                with open(self._p(HISTORY_NAME), "wb") as f:
                    f.write(json.dumps(rows, indent=1).encode())
                return True, None
            except Exception as e:                                    # noqa: BLE001
                return False, str(e)

    def clear_history(self):
        try:
            with open(self._p(HISTORY_NAME), "wb") as f:
                f.write(b"[]")
            return True, None
        except Exception as e:                                        # noqa: BLE001
            return False, str(e)

    def list_objects(self, rel=""):
        try:
            d = self._p(rel) if rel else self.base
            return [{"path": os.path.join(d, n)} for n in sorted(os.listdir(d))]
        except Exception:                                             # noqa: BLE001
            return None

    def selftest(self):
        probe = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        ok, why = self.write_image("_selftest.bin", probe)
        got, _ = self.read_image("_selftest.bin")
        self.delete_image("_selftest.bin")
        return {"mode": self.kind, "base": self.base, "available": True, "write": ok,
                "byte_identical": got == probe, "ok": bool(ok and got == probe),
                "detail": why or ("EPHEMERAL: this is a container/laptop directory, not durable storage. "
                                  "Anything configured here is lost when the app restarts.")}


def build(cfg):
    """Pick the store from what the environment actually provides, never from an intention."""
    ws = WorkspaceStore(cfg.host, cfg.state_path, cfg.client_id, cfg.client_secret, cfg.dev_token)
    if ws.available():
        return ws
    return LocalStore(cfg.local_state_dir)
