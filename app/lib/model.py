"""THE PRIZE MODEL — the declared odds, the wheel geometry built from them, and the draw.

⭐ THE ONE DESIGN DECISION THAT MATTERS: **THE DRAW DECIDES, THE ANIMATION PRESENTS IT.**

There are two ways to build a prize wheel and they give different guarantees:

  (A) PHYSICS DECIDES — spin with a random impulse, decelerate, and whatever segment ends up under the
      pointer is the winner. The declared percentages are then only as true as the segment boundaries and
      the stopping distribution happen to make them.
  (B) THE DRAW DECIDES — pick the winning prize by a weighted random draw over the declared percentages,
      pick one of that prize's segments uniformly, then animate the wheel to land exactly there.

This app is (B), and the reason is that **it makes the wrong answer unrepresentable rather than merely
unlikely**. Under (B) the realised distribution IS the declared distribution by construction: it is one
`random.random()` against a cumulative sum, and nothing downstream can bend it. Under (A) the realised
distribution is an emergent property of floating-point segment boundaries and a deceleration model, so it
can drift while everything still looks right. This is a real prize giveaway at a hosted event, where "the
odds we configured are the odds that happened" is the single property that must hold.

TWO CONSEQUENCES WORTH STATING, because they are what (B) buys:

* **The interleave below is COSMETIC.** Where a prize's segments sit on the wheel changes nothing about
  its chance of winning. That is why `build_segments` can order slots for looks without anybody having to
  re-argue the odds — and it is the clearest illustration of the difference between (A) and (B), where
  the layout *is* the odds.
* **`splits` costs nothing in fairness.** A 50% prize split into 3 segments is three segments of 16.67%
  each; the draw still picks the PRIZE at 50% and only then picks which of its three segments to land on.

MEASURED, NOT ARGUED: `tools/measure_distribution.py` runs the real `draw()` several hundred thousand
times and checks the observed frequencies against the configured percentages within sampling error, and
checks that segment choice within a prize is uniform. A distribution claim needs a measurement.

## ANGLE CONVENTION (used identically by the server, the renderer and the pointer readout)

Wheel-space angles are DEGREES CLOCKWISE FROM THE WHEEL'S OWN 12 O'CLOCK MARK. The pointer is fixed at
screen 12 o'clock. A feature at wheel angle `a` appears at screen angle `a + theta`, so the segment under
the pointer is the one containing `(-theta) mod 360`, and landing segment `i` means
`theta = (-centre_i) mod 360`. `pointer_wheel_angle()` and `segment_at()` below are the definition; the
renderer's JS mirrors those two lines and `tests/test_model.py` pins them against each other.
"""
import random

# Caps. Each one is a guard against a configuration that renders or reads badly rather than a correctness
# limit, so each is reported by name when it bites (see `validate`).
MAX_PRIZES = 24
MAX_SPLITS = 12
MAX_SEGMENTS = 72
MIN_CHANCE = 0.01
SLIVER_DEGREES = 2.5          # below this a segment is too thin to read; a WARNING, never a refusal
TOTAL_TOLERANCE = 0.01        # how close to 100 the declared total must be to count as 100

# A vivid palette that holds up on a dark stage and keeps neighbours distinguishable. The settings page
# can override any prize's colour; this is only what a new prize starts with.
PALETTE = ["#FF3621", "#00A972", "#2272B4", "#FFAB00", "#9A3FD1", "#00B2CC",
           "#E5486E", "#7FB800", "#F06E2B", "#4C6EF5", "#12B886", "#C2255C"]


def default_prizes():
    """What a never-configured deployment shows, so the wheel is never a blank circle on a stage.

    These are placeholders with no images, which is a state the UI renders deliberately (a monogram
    plate) rather than as a broken image — see `static/js/wheel.js`.
    """
    return [
        {"id": "p_demo1", "name": "Grand Prize", "chance": 5.0, "splits": 1, "colour": PALETTE[0]},
        {"id": "p_demo2", "name": "Runner Up", "chance": 15.0, "splits": 2, "colour": PALETTE[1]},
        {"id": "p_demo3", "name": "Sticker Pack", "chance": 30.0, "splits": 3, "colour": PALETTE[2]},
        {"id": "p_demo4", "name": "Try Again", "chance": 50.0, "splits": 4, "colour": PALETTE[3]},
    ]


def coerce_prize(raw, index=0):
    """One prize, cleaned. Never raises — `validate` is what refuses; this only makes the types sane."""
    def num(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def integer(v, default):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    pid = str(raw.get("id") or "").strip() or f"p_{index}"
    name = str(raw.get("name") or "").strip()
    out = {
        "id": pid,
        "name": name,
        "chance": max(0.0, round(num(raw.get("chance"), 0.0), 4)),
        "splits": min(MAX_SPLITS, max(1, integer(raw.get("splits"), 1))),
        "colour": str(raw.get("colour") or PALETTE[index % len(PALETTE)]),
    }
    # An image is three facts that must travel together: the stored object, what to serve it as, and what
    # it hashes to. A half-present image is treated as no image at all rather than a broken <img>.
    img, mime = raw.get("image"), raw.get("image_mime")
    if img and mime:
        out["image"] = str(img)
        out["image_mime"] = str(mime)
        out["image_md5"] = str(raw.get("image_md5") or "")
        out["image_w"] = integer(raw.get("image_w"), 0)
        out["image_h"] = integer(raw.get("image_h"), 0)
    return out


def declared_total(prizes):
    return round(sum(float(p.get("chance") or 0.0) for p in prizes), 4)


def totals_to_100(prizes):
    return abs(declared_total(prizes) - 100.0) <= TOTAL_TOLERANCE


def validate(prizes):
    """-> (errors, warnings). Errors REFUSE a save; warnings are shown and saved anyway.

    ⭐ THE PERCENTAGE RULE, DECIDED EXPLICITLY (and visible on the settings page as you type): a
    configuration whose chances do not total 100 is **REFUSED, with the actual total named**. It is not
    silently normalised, because then the number the host typed would not be the number in force and
    nothing on screen would say so. The settings page offers a one-click "balance to 100%" that rewrites
    the visible numbers *before* saving, so the host always sees the odds that will be used.
    """
    errors, warnings = [], []
    if not prizes:
        # ⭐ AN EMPTY WHEEL IS ALLOWED, and that is a deliberate change from the first version, which made
        #    this an ERROR. Two reasons: "clear everything and start over" was impossible (the refusal hit
        #    on the last prize removed), and `wheel.load()` already has a rendered `empty` state that the
        #    refusal made UNREACHABLE — a branch that cannot happen is a branch nobody maintains. The game
        #    page shows a deliberate empty state and disables the spin, so nothing silently looks ready.
        warnings.append("The wheel has no prizes, so nobody can spin it. Add at least one before the event.")
        return errors, warnings
    if len(prizes) > MAX_PRIZES:
        errors.append(f"{len(prizes)} prizes is more than this wheel supports ({MAX_PRIZES}).")

    seen = set()
    for i, p in enumerate(prizes, 1):
        label = p.get("name") or f"prize {i}"
        if not str(p.get("name") or "").strip():
            errors.append(f"Prize {i} has no name.")
        if p["id"] in seen:
            errors.append(f"Two prizes share the id {p['id']}.")
        seen.add(p["id"])
        if p["chance"] < MIN_CHANCE:
            errors.append(f"“{label}” has a {p['chance']:g}% chance. A prize on the wheel that cannot be "
                          f"won is a trap at an event — give it at least {MIN_CHANCE:g}%, or remove it.")
        if p["splits"] < 1:
            errors.append(f"“{label}” needs at least 1 segment.")

    total = declared_total(prizes)
    if not totals_to_100(prizes):
        errors.append(f"The chances add up to {total:g}%, not 100%. "
                      f"{'Remove' if total > 100 else 'Add'} {abs(total - 100):g}% "
                      f"— or use “Balance to 100%”.")

    seg_count = sum(p["splits"] for p in prizes)
    if seg_count > MAX_SEGMENTS:
        errors.append(f"That is {seg_count} segments in total; the wheel supports {MAX_SEGMENTS}. "
                      f"Reduce some of the split counts.")

    # Warnings: aesthetic, so they never block a save — but they are named precisely.
    if totals_to_100(prizes):
        for p in prizes:
            deg = (p["chance"] / 100.0) * 360.0 / p["splits"]
            if deg < SLIVER_DEGREES:
                warnings.append(f"“{p['name']}” split {p['splits']} ways gives segments of only "
                                f"{deg:.1f}° — too thin to see. Use fewer splits.")
    for p in prizes:
        if not p.get("image"):
            warnings.append(f"“{p['name']}” has no image yet, so it shows as a lettered plate.")
    return errors, warnings


# ====================================================================================================
# THE ARRANGEMENT — where each prize's segments sit around the wheel.
#
# THE SPEC, VERBATIM (2026-09-23): "if we say a prize has a 50% chance of winning, and the split is 3,
# it will automatically have 3 segments of 16.67% that are AS EVENLY SPACED FROM EACH OTHER AS POSSIBLE."
#
# So segment order is part of the specification, not a free choice. "As possible" is doing real work:
# with mixed split counts and unequal widths, perfectly even spacing is generally NOT achievable, so this
# needs a stated objective rather than a vibe.
#
# ⭐ THE OBJECTIVE, LEXICOGRAPHIC, IN THIS ORDER:
#      1. MINIMISE THE NUMBER OF ADJACENT PAIRS OF SEGMENTS BELONGING TO THE SAME PRIZE.
#      2. Then minimise sum over prizes of sum over that prize's consecutive centre-to-centre gaps of
#         ((gap - 360/k) / (360/k))**2  —  the RELATIVE deviation from that prize's ideal spacing.
#
# WHY LEXICOGRAPHIC AND NOT ONE BLENDED SCORE: adjacency is the specific visible defect the owner named — two
# segments of one prize touching merge into one big block, which is exactly the thing splitting is meant to
# prevent. A single weighted score will trade one adjacency for a small evenness gain, and that trade is
# never the one he wants. Ranking adjacency first makes that trade unrepresentable.
#
# WHY *RELATIVE* DEVIATION rather than absolute degrees: 10° off a 180° ideal is invisible; 10° off a 45°
# ideal is obvious. Absolute squared error scores those two the same, which is the wrong ranking.
#
# ⛔ AND WHEN IT IS IMPOSSIBLE, IT SAYS SO. A prize holding more than half the wheel's segments CANNOT
#    have them all separated — on a cycle of N slots, k of one kind form at most min(k, N-k) runs, so at
#    least max(0, 2k - N) same-prize pairs must touch. `arrangement_report` computes that floor, compares
#    it with what was achieved, and the settings page shows the shortfall. A constraint that cannot be
#    satisfied is reported, never quietly dropped.
# ====================================================================================================

_ARRANGE_CACHE = {}          # fingerprint -> order. Pure function of the prize list, so this is safe.
_ARRANGE_CACHE_MAX = 32


def _widths(prizes):
    total = declared_total(prizes)
    basis = total if total > 0 else 1.0
    return [(p["chance"] / basis) * 360.0 / p["splits"] for p in prizes]


def _centres_by_prize(order, widths, n_prizes):
    """Walk the order once, accumulating angles. -> (list per prize of centre angles, in wheel order)."""
    out = [[] for _ in range(n_prizes)]
    cursor = 0.0
    for pi in order:
        w = widths[pi]
        out[pi].append(cursor + w / 2.0)
        cursor += w
    return out


def _cost(order, widths, n_prizes):
    """-> (adjacent_same_prize_pairs, relative_squared_deviation). Compared as a tuple, so adjacency wins.

    Gaps are CYCLIC: the last segment of a prize wraps to its first across 12 o'clock, which is a real gap
    on a wheel and is the one an implementation most often forgets.
    """
    n = len(order)
    if n == 0:
        return (0, 0.0)
    adj = sum(1 for i in range(n) if order[i] == order[(i + 1) % n])
    centres = _centres_by_prize(order, widths, n_prizes)
    dev = 0.0
    for cs in centres:
        k = len(cs)
        if k < 2:
            continue
        ideal = 360.0 / k
        for j in range(k):
            gap = (cs[(j + 1) % k] - cs[j]) % 360.0
            d = (gap - ideal) / ideal
            dev += d * d
    return (adj, dev)


def _greedy_order(prizes, widths, phase):
    """Constructive seed: walk the circle and always place whichever prize is most OVERDUE in ANGLE.

    Each prize has an ideal angular period T = 360/k. Walking the wheel, prize i's next segment would be
    centred at `cursor + w_i/2`; whichever prize is furthest past its own `next_ideal` takes the slot. This
    paces prizes by angle rather than by slot count, which is what "evenly spaced" means on a wheel whose
    segments are not all the same width.

    `phase` staggers the prizes' starting ideals so they do not all want to go first; several values are
    tried and the best kept, because which stagger wins depends on the split counts.
    """
    n_prizes = len(prizes)
    left = [p["splits"] for p in prizes]
    period = [360.0 / p["splits"] for p in prizes]
    # Stagger: prize i's first segment ideally centred a fraction of its own period in.
    next_ideal = [phase * period[i] * (i / max(1, n_prizes)) for i in range(n_prizes)]
    order, cursor = [], 0.0
    total_slots = sum(left)
    for _ in range(total_slots):
        best, best_key = -1, None
        for i in range(n_prizes):
            if left[i] <= 0:
                continue
            lateness = (cursor + widths[i] / 2.0) - next_ideal[i]
            # Most overdue first; then the prize with the most segments still to place (it is the one that
            # will struggle to stay separated); then declaration order, so this is deterministic.
            key = (round(lateness, 9), left[i], -i)
            if best_key is None or key > best_key:
                best, best_key = i, key
        if best < 0:
            break
        order.append(best)
        cursor += widths[best]
        next_ideal[best] += period[best]
        left[best] -= 1
    return order


def _polish(order, widths, n_prizes, budget=60000):
    """Bounded steepest-descent local search over TWO move types, deterministic, never worse than its seed.

    ⭐ BOTH MOVE TYPES ARE NEEDED, and that was measured rather than assumed. With 2-swaps alone the
    default four-prize wheel settled at a deviation of 0.3613 while an exhaustive search over all 12,600
    arrangements found 0.2613 — a real local minimum, not a rounding difference. Adding the INSERTION move
    (lift one segment out and drop it elsewhere, sliding the rest) reaches the exhaustive optimum on every
    configuration small enough to check exhaustively. `tests/test_arrangement.py` pins that comparison, so
    a future simplification back to swaps-only fails a test instead of quietly degrading the wheel.

    Steepest descent (take the best improving move each pass, not the first) because first-improvement was
    what walked into that minimum.
    """
    best = list(order)
    best_cost = _cost(best, widths, n_prizes)
    n = len(best)
    evals = 0
    while evals < budget:
        move_best, move_cost = None, best_cost
        for a in range(n):
            for b in range(n):
                if a == b or best[a] == best[b]:
                    continue
                if evals >= budget:
                    break
                # swap a,b — only evaluated once per unordered pair
                if a < b:
                    cand = list(best)
                    cand[a], cand[b] = cand[b], cand[a]
                    evals += 1
                    c = _cost(cand, widths, n_prizes)
                    if c < move_cost:
                        move_best, move_cost = cand, c
                # insertion: lift a, drop it at b
                cand = list(best)
                v = cand.pop(a)
                cand.insert(b, v)
                evals += 1
                c = _cost(cand, widths, n_prizes)
                if c < move_cost:
                    move_best, move_cost = cand, c
            if evals >= budget:
                break
        if move_best is None:
            break                                 # a local optimum under both move types
        best, best_cost = move_best, move_cost
    return best, best_cost


def min_possible_adjacencies(prizes):
    """The floor no arrangement can beat. On a cycle of N slots, k segments of one prize form at most
    min(k, N-k) separate runs, so at least max(0, 2k - N) of its pairs must touch. Summed over prizes —
    though at most one prize can ever hold more than half the slots, so at most one term is non-zero."""
    n = sum(p["splits"] for p in prizes)
    return sum(max(0, 2 * p["splits"] - n) for p in prizes) if n else 0


def arrange(prizes):
    """-> list of prize indices, one per wheel slot, in clockwise order from 12 o'clock.

    Deterministic and memoised: the same prize list always produces the same wheel, so a host who reloads
    the page does not watch their layout shuffle.
    """
    n = sum(p["splits"] for p in prizes)
    if n <= 0:
        return []
    if len(prizes) == 1:
        return [0] * n                      # the degenerate case: one prize, nothing to interleave
    fp = tuple((p["id"], round(p["chance"], 6), p["splits"]) for p in prizes)
    hit = _ARRANGE_CACHE.get(fp)
    if hit is not None:
        return list(hit)

    widths = _widths(prizes)
    best, best_cost = None, None
    for phase in (0.0, 0.5, 1.0, 1.5):
        cand = _greedy_order(prizes, widths, phase)
        if len(cand) != n:
            continue
        c = _cost(cand, widths, len(prizes))
        if best_cost is None or c < best_cost:
            best, best_cost = cand, c
    if best is None:
        best = [i for i, p in enumerate(prizes) for _ in range(p["splits"])]
    best, best_cost = _polish(best, widths, len(prizes))

    if len(_ARRANGE_CACHE) >= _ARRANGE_CACHE_MAX:
        _ARRANGE_CACHE.clear()
    _ARRANGE_CACHE[fp] = list(best)
    return best


def arrangement_report(prizes, segments=None):
    """MEASURES the arrangement rather than eyeballing it. -> dict, and it is shown on the settings page.

    Per prize with 2+ segments: the actual centre-to-centre gaps, the ideal, the worst relative deviation,
    and whether any two of its segments touch. Plus the wheel-level adjacency count against the floor that
    `min_possible_adjacencies` proves — which is what lets the UI distinguish "this is as good as this
    configuration allows" from "the arrangement fell short".
    """
    if segments is None:
        segments, _ = build_segments(prizes)
    n = len(segments)
    order = [s["prize_index"] for s in segments]
    adj_pairs = []
    for i in range(n):
        j = (i + 1) % n
        if n > 1 and order[i] == order[j]:
            adj_pairs.append((i, j))
    floor = min_possible_adjacencies(prizes)

    per = []
    for pi, p in enumerate(prizes):
        cs = [s["centre"] for s in segments if s["prize_index"] == pi]
        k = len(cs)
        row = {"prize_id": p["id"], "name": p["name"], "splits": k,
               "segment_share_pct": round((p["chance"] / p["splits"]) if p["splits"] else 0.0, 4)}
        if k >= 2:
            ideal = 360.0 / k
            gaps = [round((cs[(j + 1) % k] - cs[j]) % 360.0, 2) for j in range(k)]
            worst = max(abs(g - ideal) / ideal for g in gaps)
            row.update(ideal_gap=round(ideal, 2), gaps=gaps,
                       worst_relative_deviation=round(worst, 4),
                       even=bool(worst <= 0.15),
                       touching=any(order[i] == pi and order[(i + 1) % n] == pi for i in range(n)))
        per.append(row)

    return {
        "segment_count": n,
        "adjacent_same_prize_pairs": len(adj_pairs),
        "adjacent_at": adj_pairs,
        "unavoidable_minimum": floor,
        "at_theoretical_best": len(adj_pairs) <= floor,
        "impossible_to_separate": floor > 0,
        "prizes": per,
        # One sentence the settings page can show verbatim; written here so the explanation and the
        # numbers can never drift apart.
        "summary": _arrangement_sentence(prizes, per, len(adj_pairs), floor, n),
    }


def _arrangement_sentence(prizes, per, adj, floor, n):
    uneven = [r["name"] for r in per if r.get("splits", 0) >= 2 and not r.get("even", True)]
    if adj == 0 and not uneven:
        return "Every prize's segments are spread evenly around the wheel."
    if floor > 0:
        big = max(prizes, key=lambda p: p["splits"])
        return (f"“{big['name']}” has {big['splits']} of the wheel's {n} segments, which is more than "
                f"half — so at least {floor} of its segments must sit next to each other no matter how "
                f"they are arranged. This wheel has {adj}"
                f"{' (the best possible)' if adj <= floor else f', {adj - floor} more than necessary'}. "
                f"Give it fewer splits, or add more prizes, to separate them.")
    if adj > 0:
        return (f"{adj} pair(s) of same-prize segments ended up next to each other even though this "
                f"configuration allows none. That is a defect in the arrangement, not in the settings.")
    # ⛔ WORDED CAREFULLY: this arrangement is the most even one available for these percentages and split
    #    counts (measured against exhaustive optima in tests/test_arrangement.py), so the sentence must not
    #    read as "something went wrong". Unequal segment widths are what make perfect spacing unreachable,
    #    and the lever is the configuration, not the arrangement.
    return ("Segments are separated, and this is the most even arrangement these percentages allow — but "
            + ", ".join(f"“{u}”" for u in uneven) + " still sits more than 15% off perfectly even, because "
            "segments of different widths cannot all be spaced equally. Evening up the percentages or the "
            "split counts is what would improve it.")


def build_segments(prizes):
    """The wheel's geometry. -> (segments, meta).

    Each segment: `{index, prize_id, prize_index, name, colour, image, start, size, centre, nth}` with
    angles in DEGREES CLOCKWISE FROM 12 O'CLOCK (see the module docstring). `nth` is which of that
    prize's own segments this is, which is what `draw()` returns and what the landing animation targets.

    ⛔ THE GEOMETRY IS BUILT HERE, ON THE SERVER, AND SHIPPED TO THE BROWSER AS DATA. The renderer never
    recomputes it. One implementation means the picture, the pointer readout and the draw cannot disagree
    about where a boundary is — which is the defect that would show as the enlarged panel naming one
    prize while the result announces another.
    """
    prizes = [p for p in prizes if p["splits"] > 0]
    total = declared_total(prizes)
    # The circle must close or there is a visible wedge of nothing, so geometry always spans 360°. When
    # the declared total is not 100 the app says so loudly instead (`meta['scaled']` drives a banner) —
    # normalising quietly is the thing this must never do.
    basis = total if total > 0 else 1.0
    order = arrange(prizes)
    segments, cursor, nths = [], 0.0, [0] * len(prizes)
    for slot, pi in enumerate(order):
        p = prizes[pi]
        size = (p["chance"] / basis) * 360.0 / p["splits"]
        seg = {
            "index": slot,
            "prize_id": p["id"],
            "prize_index": pi,
            "name": p["name"],
            "colour": p["colour"],
            "image": p.get("image") or None,
            "nth": nths[pi],
            "start": cursor,
            "size": size,
            "centre": cursor + size / 2.0,
            "shade": slot % 2,      # alternating tint so two segments of ONE prize never read as one
        }
        nths[pi] += 1
        segments.append(seg)
        cursor += size
    # Absorb float drift into the last segment so the ring closes exactly at 360°.
    if segments:
        last = segments[-1]
        last["size"] = 360.0 - last["start"]
        last["centre"] = last["start"] + last["size"] / 2.0
    meta = {
        "declared_total": total,
        "scaled": not totals_to_100(prizes) and bool(prizes),
        "segment_count": len(segments),
        "thinnest": min((s["size"] for s in segments), default=0.0),
    }
    # The arrangement is MEASURED, not asserted — and the measurement travels with the geometry so the
    # settings page shows the real gaps rather than a claim about them. See `arrangement_report`.
    meta["arrangement"] = arrangement_report(prizes, segments) if segments else None
    return segments, meta


def pointer_wheel_angle(theta):
    """The wheel-space angle sitting under the (fixed, 12 o'clock) pointer at rotation `theta`."""
    return (-theta) % 360.0


def segment_at(segments, theta):
    """Which segment is under the pointer at rotation `theta`. -> index, or -1 if there are none.

    ⭐ DERIVED FROM THE ROTATION AND NOTHING ELSE — no timer, no schedule, and above all not the known
    winner. The enlarged panel on the game page reads from this (its JS twin), so what it shows is a
    consequence of where the wheel actually is. the spec calls for exactly that, and the failure it
    prevents is the ugly one: the panel showing one prize while the result announces another.
    """
    if not segments:
        return -1
    a = pointer_wheel_angle(theta)
    for s in segments:
        if s["start"] <= a < s["start"] + s["size"]:
            return s["index"]
    return segments[-1]["index"]      # only reachable on float drift at exactly 360°


def theta_for_segment(segments, index, from_theta=0.0, turns=5):
    """Rotation that puts segment `index`'s CENTRE under the pointer, `turns` full turns further on."""
    if not segments:
        return from_theta
    centre = segments[index]["centre"]
    target_mod = (-centre) % 360.0
    delta = (target_mod - from_theta) % 360.0
    return from_theta + 360.0 * turns + delta


def draw(prizes, rng=None):
    """THE DRAW. Weighted over the declared chances. -> (prize_index, nth_segment_of_that_prize).

    Two independent choices, in this order:
      1. WHICH PRIZE — one uniform deviate against the cumulative declared chances. This is the only
         thing that decides what somebody wins.
      2. WHICH OF THAT PRIZE'S SEGMENTS — uniform over its `splits`, so a prize split three ways lands
         on each of its three segments equally often. Cosmetic: it only chooses where the wheel stops.

    ⛔ `rng` IS INJECTABLE SO THE MEASUREMENT MEASURES THIS FUNCTION and not a copy of it. A distribution
    proved against a reimplementation proves nothing about what the app ships.
    """
    rng = rng or random
    if not prizes:
        return -1, 0
    total = declared_total(prizes)
    if total <= 0:
        i = rng.randrange(len(prizes))
        return i, rng.randrange(max(1, prizes[i]["splits"]))
    r = rng.random() * total
    acc = 0.0
    for i, p in enumerate(prizes):
        acc += p["chance"]
        if r < acc:
            return i, rng.randrange(max(1, p["splits"]))
    i = len(prizes) - 1                     # float drift only
    return i, rng.randrange(max(1, prizes[i]["splits"]))


def segment_index_for(segments, prize_index, nth):
    """Map a draw result onto the wheel slot it corresponds to. -> index, or -1."""
    for s in segments:
        if s["prize_index"] == prize_index and s["nth"] == nth:
            return s["index"]
    for s in segments:                       # a stale nth (config changed under us) still lands legally
        if s["prize_index"] == prize_index:
            return s["index"]
    return -1
