"""THE SERVICE LAYER — ties the store to the model, and is the only place that decides what the
browser is told about the wheel.

⛔ THE GEOMETRY AND THE DRAW BOTH LIVE ON THIS SIDE, deliberately. The browser receives the segment list
   as data and never recomputes it, so the picture, the pointer readout and the result cannot disagree
   about where a boundary is. See the docstring of `model.py` for the probability argument.
"""
import threading
import time
import uuid

from . import model


def new_id(prefix="p"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def load(store):
    """Read the configured prizes. -> dict with the STATE spelled out.

    The `source` field is the one a caller must not skip:
      * `stored`            — a real configuration, read back from durable storage.
      * `default`           — nothing has ever been configured; these are the built-in placeholders.
      * `unreadable`        — ⭐ THE STORE COULD NOT BE READ. Not the same as empty, and the game page
                              renders it as a loud problem rather than as a wheel of placeholders.
    """
    doc, state, detail = store.read_config()
    if state == "present":
        raw = doc.get("prizes")
        if isinstance(raw, list) and raw:
            prizes = [model.coerce_prize(p, i) for i, p in enumerate(raw)]
            source = "stored"
        else:
            # A document that exists but holds no prizes is a deliberate empty wheel, not a failure.
            prizes, source = [], "empty"
        updated = {"at": doc.get("updated_at"), "by": doc.get("updated_by")}
    elif state == "absent":
        prizes, source, updated = [model.coerce_prize(p, i)
                                   for i, p in enumerate(model.default_prizes())], "default", {}
    else:
        prizes, source, updated = [], "unreadable", {}

    segments, meta = model.build_segments(prizes)
    errors, warnings = (model.validate(prizes) if prizes else ([], []))
    return {"prizes": prizes, "segments": segments, "meta": meta, "source": source,
            "store_state": state, "store_detail": detail, "updated": updated,
            "errors": errors, "warnings": warnings,
            "durable": bool(getattr(store, "durable", False)), "store_kind": store.kind}


def save(store, raw_prizes, who=None):
    """Validate then store. -> (ok, errors, warnings, loaded).

    Refuses on any error, including a total that is not 100% — see `model.validate` for why that is a
    refusal and not a silent normalisation.
    """
    prizes = [model.coerce_prize(p, i) for i, p in enumerate(raw_prizes or [])]
    errors, warnings = model.validate(prizes)
    if errors:
        return False, errors, warnings, None
    doc = {"prizes": prizes,
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "updated_by": who or "unknown"}
    ok, why = store.write_config(doc)
    if not ok:
        return False, [f"The prize list was NOT saved: {why}"], warnings, None
    return True, [], warnings, load(store)


# ⭐ IDEMPOTENCY FOR THE STOP PRESS. One press = one token = one draw, however many times the request
#    arrives. Bounded so a long event cannot grow it without limit; the order is insertion order, so the
#    oldest tokens fall off first and a token is only ever needed for the few seconds around its own press.
_RECENT = {}
_RECENT_MAX = 256
_RECENT_LOCK = threading.Lock()


def draw_once(store, token=None, who=None):
    """THE DRAW, performed when STOP is pressed, at most once per token. -> (payload, error).

    The order here is the whole design: the winner is decided first, by `model.draw`, and the animation is
    then told where to stop. The browser is given the target SEGMENT INDEX — not an angle — so the landing
    position is computed from the same geometry the renderer is drawing.

    ⛔ THE REPLAY CHECK COMES FIRST, before anything can fail. A second press must return the first
       result even if the prize list has changed underneath it, because the wheel is already decelerating
       towards a segment and a different answer now would be worse than a stale one.
    """
    if token:
        with _RECENT_LOCK:
            prior = _RECENT.get(token)
        if prior is not None:
            out = dict(prior)
            out["replayed"] = True          # so the page can tell, and so a log can
            return out, None

    loaded = load(store)
    if loaded["source"] == "unreadable":
        return None, ("The prize list could not be read, so no spin was drawn. " +
                      (loaded["store_detail"] or ""))
    segments = loaded["segments"]
    if not segments:
        return None, "There are no prizes configured yet, so there is nothing to spin for."

    prize_index, nth = model.draw(loaded["prizes"])
    seg_index = model.segment_index_for(segments, prize_index, nth)
    if seg_index < 0:
        return None, "The draw picked a prize that is not on the wheel; nothing was spun."
    prize = loaded["prizes"][prize_index]
    row = {
        "spin_id": new_id("s"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prize_id": prize["id"],
        "prize_name": prize["name"],
        "chance": prize["chance"],
        "segment_index": seg_index,
        "nth_segment": nth,
        "by": who or "guest",
    }
    # Best effort and deliberately not fatal: a wheel that refuses to spin because its audit log is
    # unwritable is worse at an event than a spin that is not logged. The failure is reported, not hidden.
    ok, why = store.append_history(row)
    payload = dict(row)
    payload["logged"] = bool(ok)
    payload["replayed"] = False
    if not ok:
        payload["log_error"] = why
    if token:
        with _RECENT_LOCK:
            _RECENT[token] = payload
            while len(_RECENT) > _RECENT_MAX:
                _RECENT.pop(next(iter(_RECENT)))
    return payload, None
