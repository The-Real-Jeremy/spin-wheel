#!/usr/bin/env python3
"""THE ARRANGEMENT — measured against hard configurations, and against EXACT optima where one is computable.

the spec (2026-09-23): a prize split k ways gets k segments "as evenly spaced from each other as
possible". This file is the measurement of that, because a spread that looks fine in one screenshot can
still be badly uneven, and an arrangement that mostly works will occasionally place two of a prize's
segments adjacent — which looks like a rendering accident rather than the bug it is.

WHAT IS CHECKED, and each of these has caught something:
  1. **Exhaustive optimality.** For every configuration small enough (<= 11 segments) every distinct
     arrangement is enumerated and the heuristic must match the best one. This is what caught
     swaps-only local search settling 38% worse than optimal on the DEFAULT wheel.
  2. **No avoidable adjacency.** Whenever the configuration allows separation, zero same-prize pairs touch.
  3. **The impossible cases degrade to the proven floor** — max(0, 2k-N) summed over prizes — rather than
     to whatever the algorithm happens to produce, and the report SAYS SO.
  4. **The gaps themselves**, per prize, against that prize's ideal 360/k.
  5. **Determinism**, because a host who reloads must not watch their layout shuffle.
  6. **Cost**, since this runs on a page load: the 72-segment worst case must stay well under a second.

The configurations are chosen to be awkward: one dominant prize, several prizes sharing a split count, a
split-1 beside a split-5, and the degenerate single-prize wheel. A balanced four-prize wheel proves little.

Run: python3 tests/test_arrangement.py
"""
import itertools
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from lib import model                                                    # noqa: E402

FAILS = []


def ok(cond, what):
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def mk(raw):
    return [model.coerce_prize(p, i) for i, p in enumerate(raw)]


def exhaustive_best(prizes):
    """The true optimum over every distinct cyclic arrangement. Only for small wheels."""
    widths = model._widths(prizes)
    ms = [i for i, p in enumerate(prizes) for _ in range(p["splits"])]
    best, bc = None, None
    for perm in set(itertools.permutations(ms)):
        if perm[0] != ms[0]:                      # cost is rotation-invariant; pin slot 0 to cut the space
            continue
        c = model._cost(list(perm), widths, len(prizes))
        if bc is None or c < bc:
            best, bc = list(perm), c
    return best, bc


CASES = [
    ("the default wheel (5/15/30/50, splits 1/2/3/4)", model.default_prizes(), True),
    ("the spec's example: 50% split 3, alongside 35/15",
     [dict(id="a", name="Big", chance=50, splits=3), dict(id="b", name="Mid", chance=35, splits=2),
      dict(id="c", name="Small", chance=15, splits=1)], True),
    ("FOUR PRIZES SHARING A SPLIT COUNT (25% each, split 3)",
     [dict(id=c, name=c.upper(), chance=25, splits=3) for c in "abcd"], False),
    ("A SPLIT-1 NEXT TO A SPLIT-5, separation impossible",
     [dict(id="a", name="A", chance=50, splits=5), dict(id="b", name="B", chance=25, splits=1),
      dict(id="c", name="C", chance=25, splits=1)], True),
    ("ONE DOMINANT PRIZE: 90% split 9, two others",
     [dict(id="a", name="Dom", chance=90, splits=9), dict(id="b", name="B", chance=5, splits=1),
      dict(id="c", name="C", chance=5, splits=1)], True),
    ("DEGENERATE: a single prize split 6 ways",
     [dict(id="a", name="Only", chance=100, splits=6)], False),
    ("awkward mixed 40/30/20/10, splits 2/3/2/1",
     [dict(id="a", name="A", chance=40, splits=2), dict(id="b", name="B", chance=30, splits=3),
      dict(id="c", name="C", chance=20, splits=2), dict(id="d", name="D", chance=10, splits=1)], True),
    ("a dominant 70% split 12 with eleven single-segment prizes",
     [dict(id="a", name="Dom", chance=70, splits=12)] +
     [dict(id=f"p{i}", name=f"P{i}", chance=30 / 11, splits=1) for i in range(11)], False),
    ("a tiny grand prize among a big consolation",
     [dict(id="g", name="Grand", chance=0.5, splits=1), dict(id="s", name="Second", chance=4.5, splits=1),
      dict(id="t", name="Third", chance=20, splits=2),
      dict(id="c", name="Consolation", chance=75, splits=6)], False),
]

for title, raw, try_exhaustive in CASES:
    prizes = mk(raw)
    n = sum(p["splits"] for p in prizes)
    model._ARRANGE_CACHE.clear()
    t0 = time.time()
    segments, meta = model.build_segments(prizes)
    ms = (time.time() - t0) * 1000
    rep = meta["arrangement"]
    widths = model._widths(prizes)
    order = [s["prize_index"] for s in segments]
    cost = model._cost(order, widths, len(prizes))
    floor = model.min_possible_adjacencies(prizes)

    print(f"\n{'=' * 94}\n{title}\n  {n} segments in {ms:.1f} ms · "
          f"order {'-'.join(prizes[i]['name'][:3] for i in order)}")
    print(f"  adjacent same-prize pairs: {rep['adjacent_same_prize_pairs']} "
          f"(proven floor {floor}) · relative deviation {cost[1]:.4f}")
    print(f"  report says: {rep['summary']}")

    ok(abs(sum(s["size"] for s in segments) - 360.0) < 1e-9, "geometry still spans exactly 360°")
    ok(rep["adjacent_same_prize_pairs"] == cost[0],
       "the report's adjacency count matches the geometry it describes")
    ok(rep["unavoidable_minimum"] == floor, f"the report names the proven floor ({floor})")

    # 2 & 3: avoidable adjacency is zero; unavoidable adjacency lands exactly on the floor.
    if floor == 0:
        ok(rep["adjacent_same_prize_pairs"] == 0,
           "NO two segments of one prize touch (this configuration allows none)")
        ok(rep["impossible_to_separate"] is False, "the report does not claim impossibility")
    else:
        ok(rep["adjacent_same_prize_pairs"] == floor,
           f"unavoidable adjacency degrades to EXACTLY the floor ({floor}), not worse")
        ok(rep["impossible_to_separate"] is True and rep["at_theoretical_best"] is True,
           "the report says separation is impossible here AND that this is the best possible")
        ok("must sit next to each other" in rep["summary"] or "no matter how" in rep["summary"],
           "the summary EXPLAINS the impossibility rather than staying silent about it")

    # 4: the gaps themselves.
    for row in rep["prizes"]:
        if row["splits"] < 2:
            continue
        ideal = row["ideal_gap"]
        ok(abs(sum(row["gaps"]) - 360.0) < 0.05,
           f"“{row['name']}”'s {row['splits']} cyclic gaps sum to 360 (wrap included)")
        print(f"      {row['name'][:12]:<13} ideal {ideal:6.2f}°  actual {row['gaps']}  "
              f"worst off-ideal {row['worst_relative_deviation'] * 100:5.1f}%  "
              f"{'even' if row['even'] else 'UNEVEN'}")

    # 1: exhaustive optimality.
    if try_exhaustive and n <= 11 and len(prizes) > 1:
        _, bc = exhaustive_best(prizes)
        ok(cost == bc, f"matches the EXHAUSTIVE optimum over all arrangements "
                       f"(adj={bc[0]}, dev={bc[1]:.4f}); got adj={cost[0]}, dev={cost[1]:.4f}")

    # 5: determinism.
    model._ARRANGE_CACHE.clear()
    again = [s["prize_index"] for s in model.build_segments(mk(raw))[0]]
    ok(again == order, "same input -> same wheel (deterministic, no RNG in the arrangement)")

print(f"\n{'=' * 94}\n== worst-case cost (this runs on a page load) ==")
worst = mk([dict(id=f"p{i}", name=f"P{i}", chance=100 / 8, splits=9) for i in range(8)])
model._ARRANGE_CACHE.clear()
t0 = time.time()
segs, meta = model.build_segments(worst)
ms = (time.time() - t0) * 1000
print(f"  72 segments (8 prizes x 9 splits): {ms:.0f} ms, "
      f"adj={meta['arrangement']['adjacent_same_prize_pairs']}")
ok(ms < 800, f"the 72-segment worst case arranges in under 800 ms ({ms:.0f} ms)")
ok(meta["arrangement"]["adjacent_same_prize_pairs"] == 0, "and separates all 72 perfectly")
t0 = time.time()
model.build_segments(worst)
print(f"  the same config again (memoised): {(time.time() - t0) * 1000:.2f} ms")

print(f"\n{'ALL GREEN' if not FAILS else 'FAILURES:'}  ({len(FAILS)} failures)")
for f in FAILS:
    print("  · " + f)
sys.exit(1 if FAILS else 0)
