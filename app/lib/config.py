"""EVERYTHING THE APP READS FROM ITS ENVIRONMENT, resolved in one place with the states named.

⛔ THERE IS NO SETTINGS PASSWORD, DELIBERATELY (owner, 2026-09-23): the app runs on the
   hosts' own laptops and contestants only press the stop button. The gate was removed ENTIRELY — the variable, the sessions, the per-IP backoff and the UI — not
   disabled behind a flag, because a half-removed feature is how a gate comes back as a bug. The threat
   model is that the app only ever runs on the hosts' own machines and the contestant never drives it.

The one thing here worth reading rather than skimming is how the app decides whether it has durable
storage: `durable_mode()` answers from what the environment actually provides, never from an intention.
"""
import os

def _env(name, default=""):
    v = os.environ.get(name)
    # The Apps API refuses an env entry with no value, so a "none" sentinel is how a bundle variable says
    # "unset". Treated identically to absent.
    if v is None:
        return default
    v = v.strip()
    if v == "" or v.lower() == "none":
        return default
    return v


class Config:
    def __init__(self, env=None):
        e = env or os.environ

        # ── identity / branding ────────────────────────────────────────────────────────────────────
        self.app_name = _env("DATABRICKS_APP_NAME") or _env("APP_NAME") or "spin-wheel"
        self.event_name = _env("EVENT_NAME", "Spin to Win")
        self.build_id = _env("BUILD_ID", "local")
        self.theme = _env("THEME", "midnight")

        # ── the spin, which is OPERATOR-STOPPED ────────────────────────────────────────────────────
        # The wheel accelerates to `cruise_deg_s` and holds there until a human presses STOP; the winner is
        # drawn at that moment and the wheel then decelerates onto it. See static/js/game.js for the
        # deceleration, which is solved rather than scheduled.
        self.cruise_deg_s = _float(_env("CRUISE_DEG_S", "660"), 660.0, 120.0, 1800.0)
        self.spinup_seconds = _float(_env("SPINUP_SECONDS", "1.15"), 1.15, 0.3, 6.0)
        # The wheel must turn at least this much after STOP, so it reads as a wheel slowing down rather
        # than a wheel being switched off.
        self.spindown_min_turns = _float(_env("SPINDOWN_MIN_TURNS", "2"), 2.0, 1.0, 8.0)
        # Shape of the deceleration: theta = 1 - (1-t)**n. Bigger n = harder initial braking and a longer
        # tail. It also fixes the duration, because the initial velocity has to match the cruise speed
        # exactly — there is no abrupt change at the press.
        self.spindown_exponent = _float(_env("SPINDOWN_EXPONENT", "3.4"), 3.4, 1.6, 8.0)

        # ── where durable state lives ──────────────────────────────────────────────────────────────
        # Apps injects these into every container; they are the app's own service principal.
        # ⛔ APPS INJECTS `DATABRICKS_HOST` WITH NO SCHEME — measured on the first real deployment:
        #    `<workspace>.cloud.databricks.com`, not `https://…`. Concatenating that with a path gives
        #    urllib `ValueError: unknown url type`, so every workspace call fails and the app reports its
        #    prize list as UNREADABLE. It fails loudly rather than silently, which is why the deploy
        #    verifier caught it — but it is normalised here so it cannot happen again.
        self.host = _normalise_host(_env("DATABRICKS_HOST"))
        self.client_id = _env("DATABRICKS_CLIENT_ID")
        self.client_secret = _env("DATABRICKS_CLIENT_SECRET")
        # Laptop only: a personal token, so the workspace store can be exercised off-container. Never set
        # in a deployment — a shipped token would be a credential in an artefact.
        self.dev_token = _env("SW_DEV_TOKEN")
        # An explicit override exists for the workspace that has locked the SP home down. Default is
        # DERIVED from the SP's own client id and is not configurable by accident: a path a human can set
        # is a path two deployments can be pointed at, and this one holds the odds.
        self.state_path = _env("STATE_PATH") or (
            f"/Users/{self.client_id}/{self.app_name}-state" if self.client_id else "")
        # Fallback for a laptop with no credentials at all: a local directory, which is EPHEMERAL in a
        # container and says so everywhere it surfaces.
        self.local_state_dir = _env("LOCAL_STATE_DIR", "/tmp/spin-wheel-state")

        self.port = int(_env("DATABRICKS_APP_PORT", _env("PORT", "8000")))

    # ------------------------------------------------------------------ introspection for /api/health
    def durable_mode(self):
        """`workspace` when this container can keep state across a restart, `local` when it cannot."""
        if self.host and self.state_path and (self.dev_token or (self.client_id and self.client_secret)):
            return "workspace"
        return "local"

    def public(self):
        """Safe to serve to a browser: no secret and no token."""
        return {
            "app_name": self.app_name,
            "event_name": self.event_name,
            "build_id": self.build_id,
            "theme": self.theme,
            "cruise_deg_s": self.cruise_deg_s,
            "spinup_seconds": self.spinup_seconds,
            "spindown_min_turns": self.spindown_min_turns,
            "spindown_exponent": self.spindown_exponent,
            "durable_mode": self.durable_mode(),
        }


def _normalise_host(raw):
    """-> `https://host` with no trailing slash, or "" — whatever shape the environment supplied."""
    h = (raw or "").strip().rstrip("/")
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h
    return "https://" + h


def _float(raw, default, lo, hi):
    try:
        return min(hi, max(lo, float(raw)))
    except (TypeError, ValueError):
        return default
