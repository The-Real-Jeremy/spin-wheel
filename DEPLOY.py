# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy Spin to Win (browser only)
# MAGIC
# MAGIC Two ways, chosen with the **mode** widget:
# MAGIC - **child**: add it to App-ception, deployed from `app-ception-main/` (the default).
# MAGIC   It copies this folder into the catalog's `child_apps/spin_wheel/`, adds the scopes this app needs to the
# MAGIC   catalog app, and redeploys the catalog.
# MAGIC - **standalone**: deploy it as its own Databricks App.
# MAGIC
# MAGIC Needs no CLI, pip or npm: only the Databricks SDK the notebook already has. Everything the app needs is in
# MAGIC this folder.

# COMMAND ----------

import os, re, shutil, time, json
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
SRC = "/Workspace" + os.path.dirname(ctx.notebookPath().get())
# A ZIP import can still be unzipping when Run all starts: wait until the file count here stops changing.
# Count through the Workspace API: the notebook's /Workspace file view can lag the import by minutes.
from databricks.sdk import WorkspaceClient as _WC
_wsapi = _WC().workspace
def _nfiles(p):
    return sum(_nfiles(o.path) if o.object_type.value == "DIRECTORY" else 1 for o in _wsapi.list(p))
_n, _t0, _since = _nfiles(SRC[len("/Workspace"):]), time.time(), time.time()
print(f"checking the import is complete: {_n} files", flush=True)
while time.time() - _since < 20 and time.time() - _t0 < 180:
    time.sleep(5)
    _m = _nfiles(SRC[len("/Workspace"):])
    if _m != _n:
        _n, _since = _m, time.time()
        print(f"  still importing: {_n} files …", flush=True)
print(f"import complete: {_n} files" if time.time() - _since >= 20 else f"still changing after 3 min ({_n} files); carrying on")
PARENT = os.path.dirname(SRC)
# The App-ception folder next to this one: app-ception-main, then the pre-rename app-catalog-main, then the
# first sibling folder that holds catalog/server.mjs (an old copy renamed aside must not beat the real one).
_cands = [d for d in sorted(os.listdir(PARENT)) if os.path.isfile(os.path.join(PARENT, d, "catalog", "server.mjs"))]
guess = next((os.path.join(PARENT, d) for d in ("app-ception-main", "app-catalog-main") if d in _cands),
             os.path.join(PARENT, _cands[0]) if _cands else "")

dbutils.widgets.dropdown("mode", "child", ["child", "standalone"], "1. Mode")
dbutils.widgets.text("catalog_folder", guess, "2. (child) App-ception folder")
dbutils.widgets.text("catalog_app_name", "app-ception", "3. (child) App-ception app name")
dbutils.widgets.text("budget_policy_id", "", "5. (standalone) Budget policy id, if your workspace requires one")
dbutils.widgets.text("standalone_app_name", "spin-wheel", "4. (standalone) App name")
MODE = dbutils.widgets.get("mode")
CHILD = "spin_wheel"
APP_ROOT = os.path.join(SRC, "app")      # what a standalone deploy ships
SCOPES = set([])                         # user-authorization scopes this app uses
print("this folder:", SRC, "| mode:", MODE)
assert os.path.isdir(APP_ROOT), f"{APP_ROOT} is missing: was the whole repo imported?"

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
api = WorkspaceClient().api_client
DEFAULT_SCOPES = {"iam.access-control:read", "iam.current-user:read"}

# An app that was just started or is mid-deployment refuses updates for a while ("pending deployment",
# "deployment in progress", compute STARTING). Wait it out: retry every 20 s for up to about 20 minutes.
import re as _re
_BUSY = _re.compile(r"pending|in progress|STARTING|STOPPING|another deployment|active deployment", _re.I)
def retry_pending(what, fn, minutes=20, every=20):
    t0 = time.time()
    while True:
        try:
            return fn()
        except Exception as e:
            if not _BUSY.search(str(e)) or time.time() - t0 > minutes * 60:
                raise
            print(f"  {what}: the app is busy ({str(e)[:100]}); retrying in {every}s …", flush=True)
            time.sleep(every)

# ── Budget / usage policies ──────────────────────────────────────────────────────────────────────────
# A workspace can require every serverless resource (including an app) to name a budget policy; then a
# create without one fails with "Unable to use fallback policies, please manually select a policy".
# Policies are account objects with no workspace list API, so the candidates are the policy ids already
# in use on apps and jobs you can see.
POLICY_ERR = "fallback polic"

def policy_candidates():
    ids = {}
    try:
        tok = None
        while True:
            r = api.do("GET", "/api/2.0/apps", query={"page_size": 100, **({"page_token": tok} if tok else {})})
            for a in r.get("apps", []):
                for k in ("budget_policy_id", "effective_budget_policy_id"):
                    if a.get(k):
                        ids.setdefault(a[k], set()).add(f"app {a['name']}")
            tok = r.get("next_page_token")
            if not tok:
                break
    except Exception as e:
        print("  (could not list apps:", str(e)[:100], ")")
    try:
        r = api.do("GET", "/api/2.1/jobs/list", query={"limit": 100})
        for j in r.get("jobs", []):
            for k in ("budget_policy_id", "effective_budget_policy_id"):
                v = j.get(k) or (j.get("settings") or {}).get(k)
                if v:
                    ids.setdefault(v, set()).add(f"job {(j.get('settings') or {}).get('name', j.get('job_id'))}")
    except Exception as e:
        print("  (could not list jobs:", str(e)[:100], ")")
    return ids

def keep_policy(a, body):
    """A PATCH is a full replacement: carry the app's policy over, or the update falls back and fails."""
    for k, eff in (("budget_policy_id", "effective_budget_policy_id"), ("usage_policy_id", "effective_usage_policy_id")):
        v = a.get(k) or a.get(eff)
        if v:
            body[k] = v
    return body

def create_app(name, description, scopes, policy_id):
    body = {"name": name, "description": description, "user_api_scopes": sorted(scopes)}
    if policy_id:
        body["budget_policy_id"] = policy_id
    try:
        api.do("POST", "/api/2.0/apps", body=body)
        return
    except Exception as e:
        if POLICY_ERR not in str(e) or policy_id:
            raise
        print("This workspace requires a budget policy on new apps. Looking for the ones in use …")
    cands = policy_candidates()
    for pid, users in cands.items():
        print(f"  {pid}   used by: {', '.join(sorted(users))[:120]}")
    if len(cands) == 1:
        pid = next(iter(cands))
        print("Exactly one policy is in use; using it:", pid)
        body["budget_policy_id"] = pid
        api.do("POST", "/api/2.0/apps", body=body)
        return
    raise RuntimeError(
        "Pick a budget policy: paste one of the ids above into the budget_policy_id widget and Run all again, "
        "OR create the app in the UI (Compute → Apps → Create app → Custom, name '" + name + "', choose the policy, "
        "add the user-authorization scopes " + (", ".join(sorted(scopes)) or "(none)") + ") and Run all again.")

def add_scopes(name, a, scopes):
    """Best effort: add missing scopes to an existing app, keeping its policy and resources."""
    have = set(a.get("user_api_scopes") or []) - DEFAULT_SCOPES
    if scopes <= have:
        return True
    body = keep_policy(a, {"name": name, "description": a.get("description", ""),
                           "user_api_scopes": sorted(have | scopes), "resources": a.get("resources") or []})
    try:
        retry_pending("scope update", lambda: api.do("PATCH", f"/api/2.0/apps/{name}", body=body))
        print(f"{name}: scopes now {sorted(have | scopes)}")
        return True
    except Exception as e:
        print(f"Could not add the scopes {sorted(scopes - have)} to {name} ({str(e)[:140]}).\n"
              f"Add them in the UI: Compute → Apps → {name} → Edit → User authorization → + Add scope. "
              "Deploying anyway; the app works once the scope is there.")
        return False


def get_app(name):
    try:
        return api.do("GET", f"/api/2.0/apps/{name}")
    except NotFound:
        return None

def wait_compute(name, states, minutes=15):
    st = None
    for _ in range(minutes * 12):
        a = get_app(name)
        st = (a.get("compute_status") or {}).get("state")
        if st in states:
            return a
        time.sleep(5)
    raise TimeoutError(f"compute of {name} did not reach {states}; last {st}")

def deploy(name, source):
    a = get_app(name)
    if (a.get("compute_status") or {}).get("state") != "ACTIVE":
        print("starting compute …")
        try:
            api.do("POST", f"/api/2.0/apps/{name}/start", body={})
        except Exception as e:
            print("  start:", str(e)[:120])
        wait_compute(name, {"ACTIVE"})
    print(f"deploying {source} → {name} …")
    # Starting a STOPPED app redeploys its last build, and a new deployment is refused while that runs.
    for _try in range(60):
        try:
            dep = api.do("POST", f"/api/2.0/apps/{name}/deployments", body={"source_code_path": source, "mode": "SNAPSHOT"})
            break
        except Exception as e:
            if "deployment in progress" not in str(e) or _try == 59:
                raise
            print(f"  the app is still finishing its previous deployment; retrying in 20 s ({_try + 1}/60) …", flush=True)
            time.sleep(20)
    t0 = time.time()
    while True:
        d = api.do("GET", f"/api/2.0/apps/{name}/deployments/{dep['deployment_id']}")
        st = (d.get("status") or {}).get("state")
        if st not in ("IN_PROGRESS", "PENDING", None):
            break
        if time.time() - t0 > 25 * 60:
            raise TimeoutError("deployment still running after 25 min: check Compute → Apps → Logs")
        time.sleep(10)
    print(f"deployment: {st} — {(d.get('status') or {}).get('message')} ({time.time() - t0:.0f}s)")
    assert st == "SUCCEEDED", "deployment failed: open Compute → Apps → this app → Logs"
    return get_app(name)

def try_get(url):
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ctx.apiToken().get()}"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)[:160]

# COMMAND ----------

if MODE == "child":
    CAT = dbutils.widgets.get("catalog_folder").strip().rstrip("/")
    CAT_APP = dbutils.widgets.get("catalog_app_name").strip()
    assert CAT and os.path.isfile(f"{CAT}/catalog/server.mjs"), \
        f"catalog_folder must be the App-ception folder (with catalog/server.mjs); got {CAT!r}. Import app-ception-main.zip first."
    dest = f"{CAT}/child_apps/{CHILD}"
    if os.path.exists(dest):   # keep the previous copy, OUTSIDE child_apps so it is not discovered as an app
        bak = f"{CAT}/.bak-child-{CHILD}-{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(dest, bak)
        for _ in range(30):     # /Workspace FUSE: a renamed path lingers for a few seconds
            if not os.path.exists(dest):
                break
            time.sleep(1)
        print("previous copy kept at", bak)
    skip = lambda d, names: [n for n in names if n.startswith("DEPLOY") or n == ".git"]
    shutil.copytree(SRC, dest, ignore=skip)
    n_src = sum(len([f for f in fs if not f.startswith("DEPLOY")]) for r, _, fs in os.walk(SRC) if "/.git" not in r)
    n_dst = sum(len(fs) for _, _, fs in os.walk(dest))
    assert n_dst == n_src, f"copied {n_dst} of {n_src} files into {dest}"
    print(f"copied {n_dst} files into {dest}")
    # Child mode never creates or replaces an app: the child runs INSIDE the existing catalog app.
    a = get_app(CAT_APP)
    if a is None and CAT_APP == "app-ception" and get_app("app-catalog") is not None:
        CAT_APP, a = "app-catalog", get_app("app-catalog")   # an install made before the rename
        print("Using the pre-rename app 'app-catalog' (same URL and grants). Set catalog_app_name to choose another.")
    assert a is not None, f"no app {CAT_APP!r}: deploy App-ception first (its DEPLOY notebook), or set catalog_app_name"
    if SCOPES - (set(a.get("user_api_scopes") or []) - DEFAULT_SCOPES):
        add_scopes(CAT_APP, wait_compute(CAT_APP, {"ACTIVE", "STOPPED", "ERROR"}), SCOPES)
    a = deploy(CAT_APP, CAT)
    url = a.get("url")
    print("\nCATALOG URL:", url)
    print("THIS APP   :", f"{url}/apps/{CHILD}/")
    st, body = try_get(f"{url}/_catalog/api/health")
    h = None
    if st == 200 and body.lstrip().startswith("{"):   # the Apps proxy may answer the notebook's token with a sign-in page
        try:
            h = json.loads(body)
        except ValueError:
            h = None
    if h is not None:
        mine = [c for c in h.get("children", []) if c["name"] == CHILD]
        print("health: offline =", h.get("offline"), "|", CHILD, "→", mine[0].get("prepare") if mine else "NOT FOUND", mine[0].get("problems") if mine else "")
    else:
        print("(open", f"{url}/_catalog/api/health", "in the browser: this app should show \"prepare\": \"ready\")")
else:
    NAME = dbutils.widgets.get("standalone_app_name").strip()
    assert NAME and NAME == NAME.lower(), "app name: lowercase letters, digits and '-'"
    a = get_app(NAME)
    if a is None:
        print(f"creating app {NAME} …")
        create_app(NAME, "Spin to Win", SCOPES, dbutils.widgets.get("budget_policy_id").strip())
    a = wait_compute(NAME, {"ACTIVE", "STOPPED", "ERROR"})
    add_scopes(NAME, a, SCOPES)
    a = deploy(NAME, APP_ROOT)
    url = a.get("url")
    print("\nAPP URL:", url)
    st, body = try_get(f"{url}/api/health")
    ok = st == 200 and not (body or "").lstrip().lower().startswith("<!doctype")
    print("health:", (body or "")[:200] if ok else "(open the URL in the browser to check it)")
