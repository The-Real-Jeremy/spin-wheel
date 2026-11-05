#!/usr/bin/env python3
"""ASSERTS THE END STATE OF A DEPLOY, and fails it if the running app disagrees. Runs in the postdeploy hook.

⛔ WHY THIS EXISTS: on Databricks Apps a deploy's own success output is not evidence, in both directions.
   Measured on Databricks Apps:
     · `bundle deploy` printed "Deployment complete!" while compute was STOPPED and there was no deployment;
     · `bundle sync` silently dropped gitignored files, and the app answered with an empty knowledge base;
     · `apps deploy` printed `Error: ... active deployment in progress` and exited 1 for a deployment that
       SUCCEEDED at that exact second;
     · an unauthenticated GET returns **200 with the login page for every path**, including assets that were
       deliberately deleted — so a check built on `status == 200` cannot fail, which makes it worse than no
       check. The login HTML is tens of KB where this app's pages are a few KB, which is the cheap tell.

SO THE VERIFICATION HIERARCHY USED HERE, weakest to strongest:
  1. `apps get` — `compute_status` ACTIVE **and** `active_deployment.status.state` SUCCEEDED. Two
     independent state machines; a probe during IN_PROGRESS returns a meaningless 502.
  2. `/api/health` returns 200 with a real body (checked for SIZE, so the login wall cannot pass).
  3. **`source_md5` from the running app equals the md5 computed over the LOCAL app.py + lib/*.py.** This is
     the one that identifies the CODE. `BUILD_ID` cannot: it is set at deploy time and proves a restart.
  4. Every static asset the page loads is fetched and compared BYTE-FOR-BYTE (md5) against the file on disk.
     Identity settles it instead of sampling it.

Usage: verify_deployed.py <app-name> [expected-build-id]
Environment: DB_PROFILE (optional) — the CLI profile to authenticate with.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
APP = os.path.join(REPO, "app")

# The files the browser actually loads. Listed rather than globbed so a file that stops being deployed is a
# FAILURE here instead of silently dropping out of the check.
ASSETS = [
    "static/index.html",
    "static/settings.html",
    "static/css/app.css",
    "static/css/settings.css",
    "static/js/wheel.js",
    "static/js/game.js",
    "static/js/settings.js",
    "static/img/favicon.svg",
]


def cli(*args, timeout=180):
    cmd = ["databricks", *args]
    profile = os.environ.get("DB_PROFILE") or ""
    if profile:
        cmd += ["--profile", profile]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def token():
    rc, out, err = cli("auth", "token")
    if rc != 0:
        raise SystemExit(f"[verify] could not get a bearer token: {err.strip()[:300]}")
    return json.loads(out)["access_token"]


def get(url, tok, timeout=60):
    """-> (status, bytes). Never raises on an HTTP error; the status is the point."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                                       # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}".encode()


def local_source_md5():
    """The same computation app.py does at import, over the same files, in the same order."""
    h = hashlib.md5()
    names = ["app.py"] + sorted("lib/" + f for f in os.listdir(os.path.join(APP, "lib"))
                                if f.endswith(".py"))
    for rel in names:
        with open(os.path.join(APP, rel), "rb") as f:
            h.update(rel.encode())
            h.update(f.read())
    return h.hexdigest(), names


def main(argv):
    if len(argv) < 2:
        print("usage: verify_deployed.py <app-name> [expected-build-id]", file=sys.stderr)
        return 2
    name = argv[1]
    expected_build = argv[2] if len(argv) > 2 else None
    problems = []

    # ── 1. the two state machines ──────────────────────────────────────────────────────────────────
    url = None
    for attempt in range(30):
        rc, out, err = cli("apps", "get", name, "-o", "json")
        if rc != 0:
            time.sleep(5)
            continue
        app = json.loads(out)
        compute = (app.get("compute_status") or {}).get("state")
        dep = (app.get("active_deployment") or {})
        dep_state = ((dep.get("status") or {}).get("state"))
        url = app.get("url")
        print(f"[verify] attempt {attempt + 1}: compute={compute} deployment={dep_state}")
        if compute == "ACTIVE" and dep_state == "SUCCEEDED":
            break
        if dep_state in ("FAILED", "CANCELLED"):
            problems.append(f"the deployment state is {dep_state}: "
                            f"{(dep.get('status') or {}).get('message')}")
            break
        time.sleep(10)
    else:
        problems.append("the app never reached compute ACTIVE + deployment SUCCEEDED")

    if not url:
        print("[verify] ⛔ no app url; cannot check the running app", file=sys.stderr)
        for p in problems:
            print("  · " + p, file=sys.stderr)
        return 1
    url = url.rstrip("/")
    print(f"[verify] app url: {url}")
    tok = token()

    # ── 2 & 3. health, and WHICH CODE is answering ─────────────────────────────────────────────────
    status, body = get(url + "/api/health", tok)
    if status != 200:
        problems.append(f"/api/health returned {status}: {body[:200]!r}")
    elif len(body) > 20000:
        # The OAuth wall's login page is tens of KB. This endpoint's body is well under 2 KB.
        problems.append(f"/api/health returned {len(body)} bytes, which is the login wall and not the app")
    else:
        health = json.loads(body)
        want_md5, names = local_source_md5()
        got_md5 = health.get("source_md5")
        print(f"[verify] served source_md5={got_md5}  local={want_md5}  ({len(names)} py files)")
        if got_md5 != want_md5:
            problems.append(f"the RUNNING code is not this checkout: served source_md5 {got_md5} != "
                            f"local {want_md5}. A restart happened but the code is different.")
        if expected_build and health.get("build_id") != expected_build:
            problems.append(f"build_id is {health.get('build_id')!r}, expected {expected_build!r}")
        store = health.get("store") or {}
        print(f"[verify] store: kind={store.get('kind')} durable={store.get('durable')} "
              f"state={store.get('state')} base={store.get('base')}")
        if not store.get("durable"):
            problems.append("the deployed app reports NON-DURABLE storage — prize settings would not "
                            f"survive a restart. detail: {store.get('detail')}")
        prizes = health.get("prizes") or {}
        print(f"[verify] prizes: source={prizes.get('source')} count={prizes.get('count')} "
              f"segments={prizes.get('segments')}")

    # ── 3b. a REAL round-trip through the durable store, as the app's own identity ──────────────────
    status, body = get(url + "/api/health?selftest=1", tok, timeout=120)
    if status == 200 and len(body) < 20000:
        st = (json.loads(body) or {}).get("selftest") or {}
        print(f"[verify] storage selftest: ok={st.get('ok')} byte_identical={st.get('byte_identical')} "
              f"base={st.get('base')}")
        if not st.get("ok"):
            problems.append(f"the storage self-test FAILED inside the container: {st.get('detail')}")
    else:
        problems.append(f"the storage self-test could not be run (HTTP {status})")

    # ── 4. byte identity of every asset the browser loads ──────────────────────────────────────────
    print("[verify] asset identity (md5 of the SERVED bytes vs the file on disk):")
    for rel in ASSETS:
        with open(os.path.join(APP, rel), "rb") as f:
            want = hashlib.md5(f.read()).hexdigest()
        status, served = get(f"{url}/{rel}", tok)
        got = hashlib.md5(served).hexdigest() if status == 200 else None
        same = got == want
        print(f"    {'OK ' if same else 'BAD'} {rel:28} http={status} served={got} local={want}")
        if not same:
            problems.append(f"{rel}: served md5 {got} != local {want} (HTTP {status})")

    # ── 5. no CDN: the pages must reference nothing off-origin ─────────────────────────────────────
    for page in ("static/index.html", "static/settings.html"):
        status, served = get(f"{url}/{page}", tok)
        text = served.decode(errors="replace") if status == 200 else ""
        offsite = [tok_ for tok_ in ("http://", "https://") if tok_ in text]
        if offsite:
            problems.append(f"{page} references an off-origin URL ({offsite}); this app must be "
                            f"entirely same-origin because a blocked host HANGS rather than failing")

    print()
    if problems:
        print(f"[verify] ⛔ DEPLOY NOT VERIFIED — {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print("  · " + p, file=sys.stderr)
        return 1
    print(f"[verify] ✅ verified against the RUNNING app: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
