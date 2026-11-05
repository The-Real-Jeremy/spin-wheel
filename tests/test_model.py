#!/usr/bin/env python3
"""Pins the model's two contracts: the ANGLE CONVENTION (so the renderer's JS twin cannot drift from it)
and the GEOMETRY (segments close the circle, splits are spread, a draw maps onto a real slot).

The distribution itself is measured separately and at volume by tools/measure_distribution.py — this file
is about structure, that one is about frequencies. Run: python3 tests/test_model.py
"""
import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from lib import model

FAILS = []
def ok(cond, what):
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)

P = [dict(id="a", name="A", chance=50.0, splits=3, colour="#1"),
     dict(id="b", name="B", chance=30.0, splits=2, colour="#2"),
     dict(id="c", name="C", chance=20.0, splits=1, colour="#3")]
P = [model.coerce_prize(p, i) for i, p in enumerate(P)]

print("\n== geometry ==")
segs, meta = model.build_segments(P)
ok(len(segs) == 6, f"6 slots for splits 3+2+1 (got {len(segs)})")
ok(abs(sum(s["size"] for s in segs) - 360.0) < 1e-9, "sizes sum to exactly 360")
ok(abs(segs[0]["start"]) < 1e-12 and abs(segs[-1]["start"] + segs[-1]["size"] - 360) < 1e-9,
   "ring starts at 0 and closes at 360")
for s in segs:
    if s["prize_id"] == "a":
        # 50% of the wheel is 180 degrees of arc; split 3 ways that is 60 degrees per segment, each
        # carrying 50/3 = 16.667 PERCENT of the chance. Degrees and percent are not the same number and
        # this assertion exists because the first version of it confused them.
        ok(abs(s["size"] - 60.0) < 1e-9, f"A's segments are 60 degrees of arc (got {s['size']:.4f})")
        ok(abs(s["size"] / 360.0 * 100.0 - 50.0/3) < 1e-9, "...which is 16.667% of the chance each")
        break
ok(meta["scaled"] is False and meta["declared_total"] == 100.0, "total 100 -> not scaled")
per = {}
for s in segs:
    per.setdefault(s["prize_id"], []).append(s["index"])
ok(per["a"] == sorted(per["a"]) and len(per["a"]) == 3, "A holds 3 slots")
# ⛔ THIS ASSERTION WAS TOO WEAK AND IS NOW THE STRONG ONE. It used to check only that A's three slots
# were not ALL consecutive, which passes on the exact defect the owner's 2026-09-23 spec addition is about:
# two of the three touching. The full arrangement measurement lives in tests/test_arrangement.py.
order = [s["prize_index"] for s in segs]
ok(not any(order[i] == order[(i + 1) % len(order)] for i in range(len(order))),
   "NO two segments of the same prize are adjacent anywhere on the wheel (cyclically)")
print(f"  ..  slot order: {[s['prize_id'] for s in segs]}")

print("\n== angle convention (the renderer's JS mirrors these two lines) ==")
for i, s in enumerate(segs):
    th = model.theta_for_segment(segs, i, from_theta=0.0, turns=0)
    ok(model.segment_at(segs, th) == i, f"theta_for_segment({i}) lands back on {i}")
th = model.theta_for_segment(segs, 2, from_theta=137.3, turns=6)
ok(model.segment_at(segs, th) == 2, "lands on 2 from an arbitrary start, 6 turns on")
ok(th - 137.3 >= 360 * 6, "target is at least 6 full turns ahead (never spins backwards)")
ok(model.segment_at(segs, 0.0) == 0, "theta=0 puts slot 0 (which starts at 12 o'clock) under the pointer")
ok(model.pointer_wheel_angle(90.0) == 270.0, "pointer_wheel_angle(90) == 270")

print("\n== draw maps onto real slots ==")
rng = random.Random(7)
bad = 0
for _ in range(20000):
    pi, nth = model.draw(P, rng)
    si = model.segment_index_for(segs, pi, nth)
    if si < 0 or segs[si]["prize_index"] != pi or segs[si]["nth"] != nth:
        bad += 1
ok(bad == 0, f"20000 draws all map onto the right slot ({bad} bad)")

print("\n== validate refuses what it should ==")
e, w = model.validate(P); ok(not e, "a good config has no errors")
bad_total = [dict(p) for p in P]; bad_total[0]["chance"] = 40.0
e, w = model.validate(bad_total)
ok(any("90" in x for x in e), f"a 90% total is refused AND the total is named: {e[:1]}")
zero = [dict(p) for p in P]; zero[2]["chance"] = 0.0
e, w = model.validate(zero); ok(any("cannot be won" in x for x in e), "a 0% prize is refused by name")
sliver = [model.coerce_prize(dict(id="x", name="X", chance=1.0, splits=12), 0),
          model.coerce_prize(dict(id="y", name="Y", chance=99.0, splits=1), 1)]
e, w = model.validate(sliver)
ok(not e and any("too thin" in x for x in w), "0.3 degree slivers WARN but do not refuse")

print("\n== a config that does not total 100 still closes the circle, and says so ==")
segs2, meta2 = model.build_segments(bad_total)
ok(abs(sum(s["size"] for s in segs2) - 360.0) < 1e-9, "geometry still spans 360")
ok(meta2["scaled"] is True and meta2["declared_total"] == 90.0,
   "meta flags scaled=True and carries the real total 90 (drives the on-screen banner)")

print("\n%s  (%d failures)" % ("ALL GREEN" if not FAILS else "FAILURES: " + "; ".join(FAILS), len(FAILS)))
sys.exit(1 if FAILS else 0)
