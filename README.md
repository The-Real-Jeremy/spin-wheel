# Spin to Win — a 3D prize wheel, as a Databricks App

## Install (browser only: no terminal, no CLI, works air-gapped)

Everything the app needs is in this repo: standard-library Python only. Two ways to install it; both use the `DEPLOY` notebook.

### A) Install on its own (a standalone Databricks App)
1. On this GitHub page: **Code → Download ZIP** (`spin-wheel-main.zip`). No need to unzip it.
2. In Databricks: **Workspace** → a folder (e.g. your home) → **⋮ → Import** → drop the `.zip` → **Import**.
   Databricks unzips it into `spin-wheel-main/`.
3. Open **`spin-wheel-main/DEPLOY`**, attach **Serverless**, set **mode = `standalone`** (and `standalone_app_name`
   if you want a different name), then **Run all**.
4. The last cell prints the **app URL**.

### B) Install as a child of the [App Catalog](https://github.com/The-Real-Jeremy/app-catalog)
1. Install the App Catalog first (its README, same four steps), so `app-catalog-main/` is in your workspace.
2. On this GitHub page: **Code → Download ZIP** (`spin-wheel-main.zip`).
3. In Databricks: open **the same folder that holds `app-catalog-main/`** → **⋮ → Import** → drop the `.zip`.
   It unzips into `spin-wheel-main/` next to the catalog.
4. Open **`spin-wheel-main/DEPLOY`**, attach **Serverless**, leave **mode = `child`** (`catalog_folder` is filled in
   for you), then **Run all**. It copies the app into the catalog's `child_apps/spin_wheel/`, adds the scopes the app
   needs to the catalog app, and redeploys the catalog. The last cell prints `<catalog URL>/apps/spin_wheel/`.

**Budget policies:** child mode never creates an app; it redeploys the existing catalog, so a workspace policy
requirement cannot stop it. For standalone mode in a workspace that requires a budget policy, set the
`budget_policy_id` widget, or create the app in the UI first (pick the policy, add the scopes this app needs), then
Run all. If exactly one policy is in use, it is picked automatically and the app bills against it; set `budget_policy_id` to choose explicitly.

**To update** either way: first **rename or delete the existing `spin-wheel-main/` folder**. Importing a ZIP onto a
folder that already exists is refused ("already exists"), and nothing is overwritten. Then import the new ZIP and run
`DEPLOY` again. In child mode, the previous copy inside the catalog is kept as `.bak-child-…`.

---

A tilted, image-bearing prize wheel for a hosted event. The host presses **Spin**; the wheel accelerates to
a cruising speed and holds there. The contestant walks up and presses **Stop**; the winner is drawn at that
moment and the wheel decelerates onto it. To the right of the wheel, an enlarged picture of whatever the
pointer is currently over — switching the whole time the wheel turns, settling on the prize that is won.

A **Settings** page adds and removes prizes, sets each one's chance of winning, uploads its picture, and
says how many segments that prize is split into.

No third-party packages. No `requirements.txt`. No CDN, no web font, no outbound call at runtime except to
the workspace's own API. That is not minimalism for its own sake: a blocked CDN host does not fail fast, it
**hangs**, and the page then paints nothing at all — which in front of an audience is the worst outcome
available.

---

## Running it

### On the workspace (one command)

```bash
DATABRICKS_CONFIG_PROFILE=<profile> databricks bundle deploy -t dev
```

That creates the app, writes `app/app.yaml` from the bundle's variables, starts and deploys the app, and
then **refuses to finish** unless the running app's own `/api/health` confirms it is serving this code. See
`bootstrap/verify_deployed.py` for what "confirms" means — it is not the exit code.

> ⛔ **Put the auth in the environment, not in `-p`.** The postdeploy hook shells out to the CLI and
> inherits only the environment. With `-p <profile>` alone, `DATABRICKS_CONFIG_PROFILE` is empty and the
> hook's own calls go to the **default** profile's workspace, silently. The predeploy hook hard-stops on it.

Deploy it somewhere else with the host-free target:

```bash
DATABRICKS_CONFIG_PROFILE=<their-profile> databricks bundle deploy -t portable \
  --var app_name=spin-wheel --var event_name="Their Event"
```

### Without the CLI (UI only, also air-gapped)
See **Install** at the top: the `DEPLOY` notebook does both paths from the browser.

### As a child of the [App Catalog](https://github.com/The-Real-Jeremy/app-catalog) (CLI alternative)
```bash
rsync -a --exclude .git spin-wheel/ <app-catalog>/child_apps/spin_wheel/
cd <app-catalog> && npm run deploy
```
The catalog finds the app root through `databricks.yml` (`app/`), runs `python app.py` on its own port
under `/apps/spin_wheel/`, and takes the card text from `catalog.yaml`. Inside the catalog the prizes
are stored under the **catalog's** service principal, so they are separate from a standalone deployment's.

### On a laptop

```bash
PORT=8731 python3 app/app.py          # http://127.0.0.1:8731
```

With no workspace credentials it keeps its state in `/tmp/spin-wheel-state`, which is **ephemeral** — both
pages say so in the loudest element they have. To exercise the real workspace store from a laptop:

```bash
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com \
SW_DEV_TOKEN=$(databricks auth token -p <profile> | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])') \
STATE_PATH=/Users/you@example.com/spin-wheel-dev PORT=8731 python3 app/app.py
```

---

## Running the game

| | |
|---|---|
| **Spin** | starts the wheel. It eases up to cruising speed over ~1.2s and then holds. |
| **Stop** | the same button, now red. The winner is drawn **when this is pressed**, and the wheel decelerates onto it over 4–6 seconds. Pressing it twice, holding the key, or pressing it again while it slows down cannot draw twice or change the result. |
| **Reset** | clears the result and returns to idle. Disabled while the wheel is moving. |
| **Space / Enter** | the same as pressing the big button. |
| **Full screen** | for the room's screen. |
| **Sound** | off by default. Ticks as segments pass, a chime on a win; all synthesised, no audio files. |

The state pill next to the button always says which state it is in — *Idle · Spinning up · Spinning ·
Stopping · Settled* — so whoever is running it never has to guess whether a press registered.

---

## Configuring the prizes

`/settings`. **There is no password**: this runs on the hosts' own laptops and the contestant never drives
it, so the gate was removed entirely rather than disabled.

* **Chance** — how often that prize wins, as a percentage.
* **Segments** — how many pieces of the wheel it is split into. A 50% prize split 3 ways becomes three
  segments of 16.67% each, **spread as evenly around the wheel as the other prizes allow**, so half the
  wheel is not one block. The panel shows the measured gaps against the ideal, and says so plainly when a
  configuration makes even spacing impossible.
* **Picture** — click the thumbnail. The browser re-encodes it (long edge capped at 900px, WebP, EXIF
  stripped) before upload; the server admits or refuses it on its **magic bytes** and serves it as what it
  actually is. A prize with no picture shows a lettered plate, deliberately.
* **Colour** — per prize.
* **Total chance must be 100%.** A save that does not total 100 is **refused, with the actual total named**.
  It is never silently normalised, because then the number typed would not be the number in force.
  *Balance to 100%* rewrites the visible numbers first, so the odds on screen are always the odds in use.

The preview is the **server's** geometry, not a second implementation, so what you see while editing is what
the wheel will be.

### Limits

| | |
|---|---|
| prizes | 24 |
| segments per prize | 12 |
| segments in total | 72 |
| minimum chance | 0.01% (a prize that cannot be won is refused — it is a trap at an event) |
| picture | 2 MB, 1400px long edge, PNG / JPEG / WebP / GIF. **SVG is refused** — it is the one image format that can carry script. |

---

## Where the setup is kept, and why

The prize list and the uploaded pictures live in the **app's own service principal's workspace home**,
`/Users/<DATABRICKS_CLIENT_ID>/spin-wheel-state/`, reached over the Workspace API with the credentials Apps
injects into every container. No grant, no group membership, no extra `user_api_scopes`.

**A container filesystem would lose them.** Measured on this rig: `apps deploy` happened to preserve a local
file while `apps stop` + `apps start` recreated it **empty**. So a durability claim tested with a redeploy
passes on a store that would still lose everything before the event.

**Proven, not asserted** (run it yourself; it prints its own evidence):

```bash
python3 tools/prove_durability.py spin-wheel --profile <profile>
```

configures prizes, uploads a picture, runs `apps stop` + `apps start`, and checks the prize list and the
picture come back byte-identical. It restores whatever was configured before it ran.

> ⛔ **`app_name` is the app's identity, not a label.** Changing it makes Terraform destroy and recreate the
> app, which mints a **new service principal** — and since the prize list lives under that principal's home,
> a rename orphans every configured prize and picture. Pick it once (the default is `spin-wheel`, the same name the DEPLOY notebook uses) and keep it.

---

## What is measured, and how to reproduce it

| Claim | How it is checked | Reproduce with |
|---|---|---|
| The realised odds are the configured odds | 500k draws x 4 awkward configurations through the **real** `model.draw()`; per-prize binomial z-scores with Bonferroni, chi-square goodness of fit, and segment-choice uniformity within each prize | `python3 tools/measure_distribution.py` |
| Each prize's segments are spread as evenly as possible | exhaustive optimum for every configuration ≤ 11 segments; zero avoidable adjacency; impossible cases land on the proven floor `max(0, 2k−N)` | `python3 tests/test_arrangement.py` |
| The enlarged panel matches the result, always | 1800 pointer lookups in the real page + every real spin end-to-end, plus a painted-and-unobstructed check on the panel canvas | `node tools/assert_panel_agrees.mjs` (writes `evidence/panel-agreement.json`) |
| The deceleration lands exactly and never jolts | 125 combinations of speed x target x start angle | `tests/test_spindown.mjs` |
| It is smooth | frame timing from the real page, reported for the whole spin **and** the deceleration separately, worst frame not the mean | `node tools/assert_panel_agrees.mjs` (writes `evidence/frame-timing.json`) |
| The deploy serves this code | served `source_md5` vs local, 8/8 assets byte-identical, storage self-test from inside the container, no off-origin URLs | `bootstrap/verify_deployed.py`, run on every deploy |

```bash
python3 tests/test_model.py                     # geometry + angle convention
python3 tests/test_arrangement.py               # the spread, against exact optima
node     tests/test_spindown.mjs                # the deceleration maths
python3 tools/measure_distribution.py           # the odds, at volume
node     tools/assert_panel_agrees.mjs <url> 6  # the browser pass (HEADED Chrome)
```

**Measured numbers as of 2026-09-23**, against the deployed origin: 2806 frames with **zero** over 20ms,
worst single frame **17.6ms**, mean 114 fps. Panel and result agreed on every spin, landing off by 0.000000°.

---

## Repo map

```
app/app.py              stdlib HTTP server, routes, the static handler
app/lib/model.py        the odds, the geometry, the arrangement, the draw   <- read this first
app/lib/wheel.py        service layer: load / save / draw_once (idempotent by token)
app/lib/store.py        the durable workspace store, and its three read states
app/lib/images.py       what may be stored as a picture, decided from the bytes
app/lib/config.py       everything read from the environment, with the states named
app/static/js/wheel.js  the renderer and the deceleration solver
app/static/js/game.js   the spin state machine and the enlarged panel
app/static/js/settings.js  the editor, the live preview, the browser-side re-encode
bootstrap/              app.yaml generation + the deploy verifier
tools/                  distribution, durability, the browser harness, the CDP driver
DESIGN.md               why it is built this way
```

---

## Known limitations, stated rather than hidden

* **The settings page is open to anyone who can open the app.** Deliberate (2026-09-23). If this ever runs
  somewhere guests can reach it on their own devices, the gate has to come back.
* **The spin log is a read-modify-write on one workspace object**, serialised by a lock inside one process.
  Apps runs a single instance and there is one wheel with one operator, so the exposure is a lost **log
  line**, never a wrong **result** — the result is decided and returned before the log is touched.
* **Labels rotate with the wheel**, so after a spin about half of them read upside down. That is how a
  physical prize wheel behaves, and it is why the enlarged panel exists. Each label is upright relative to
  its own segment; see `upright()` in `wheel.js`.
* **A prize with a small chance gets a small medallion**, because the picture is sized to fit its wedge. The
  enlarged panel is where a picture is seen properly.
* **The browser does the re-encoding**, not the server — there is no image codec in the standard library.
  The server's guarantee is *"a PNG/JPEG/WebP/GIF within the size and dimension caps, served as what it
  actually is"*. `app/lib/images.py` says exactly where that boundary sits.
* **If the workspace is rebuilt**, the app URL changes and the service
  principal is new, so the stored prize list is orphaned — reconfigure after any rebuild.
