# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy Spin to Win (browser only)
# MAGIC
# MAGIC Two ways, chosen with the **mode** widget:
# MAGIC - **child**: add it to an App Catalog that you deployed from `app-catalog-main/` (the default).
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
PARENT = os.path.dirname(SRC)
guess = next((os.path.join(PARENT, d) for d in sorted(os.listdir(PARENT))
              if d.startswith("app-catalog") and os.path.isfile(os.path.join(PARENT, d, "catalog", "server.mjs"))), "")

dbutils.widgets.dropdown("mode", "child", ["child", "standalone"], "1. Mode")
dbutils.widgets.text("catalog_folder", guess, "2. (child) App Catalog folder")
dbutils.widgets.text("catalog_app_name", "app-catalog", "3. (child) App Catalog app name")
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

def ensure_app(name, description, scopes):
    """Create the app if missing; add any missing scopes (a PATCH is a full replacement, so keep the rest)."""
    a = get_app(name)
    if a is None:
        print(f"creating app {name} …")
        api.do("POST", "/api/2.0/apps", body={"name": name, "description": description, "user_api_scopes": sorted(scopes)})
        a = wait_compute(name, {"ACTIVE", "STOPPED", "ERROR"})
    have = set(a.get("user_api_scopes") or []) - DEFAULT_SCOPES
    if not scopes <= have:
        a = wait_compute(name, {"ACTIVE", "STOPPED", "ERROR"})
        body = {"name": name, "description": a.get("description", ""), "user_api_scopes": sorted(have | scopes),
                "resources": a.get("resources") or []}
        for k in ("budget_policy_id", "usage_policy_id"):
            if a.get(k):
                body[k] = a[k]
        api.do("PATCH", f"/api/2.0/apps/{name}", body=body)
        print(f"{name}: scopes now {sorted(have | scopes)}")
    return get_app(name)

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
    dep = api.do("POST", f"/api/2.0/apps/{name}/deployments", body={"source_code_path": source, "mode": "SNAPSHOT"})
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
        f"catalog_folder must be the App Catalog folder (with catalog/server.mjs); got {CAT!r}. Import app-catalog-main.zip first."
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
    a = ensure_app(CAT_APP, "App Catalog: one Databricks App that hosts many apps.", SCOPES)
    a = deploy(CAT_APP, CAT)
    url = a.get("url")
    print("\nCATALOG URL:", url)
    print("THIS APP   :", f"{url}/apps/{CHILD}/")
    st, body = try_get(f"{url}/_catalog/api/health")
    if st == 200:
        h = json.loads(body)
        mine = [c for c in h.get("children", []) if c["name"] == CHILD]
        print("health: offline =", h.get("offline"), "|", CHILD, "→", mine[0].get("prepare") if mine else "NOT FOUND", mine[0].get("problems") if mine else "")
    else:
        print("(open", f"{url}/_catalog/api/health", "in the browser: this app should show \"prepare\": \"ready\")")
else:
    NAME = dbutils.widgets.get("standalone_app_name").strip()
    assert NAME and NAME == NAME.lower(), "app name: lowercase letters, digits and '-'"
    a = ensure_app(NAME, "Spin to Win", SCOPES)
    a = deploy(NAME, APP_ROOT)
    url = a.get("url")
    print("\nAPP URL:", url)
    st, body = try_get(f"{url}/api/health")
    print("health:", st if st else "(open the URL in the browser)", (body or "")[:200] if st else "")
