#!/usr/bin/env python3
"""MEASURES THE REALISED DISTRIBUTION of the app's own draw against the configured percentages.

A distribution claim needs a measurement, not an argument. This imports `app/lib/model.py` — the exact
module the running app imports — and hammers `model.draw()`, then checks:

  1. **Per prize**, the observed share against the configured percentage, as a two-sided binomial z-score
     with a Bonferroni-corrected threshold. Reported with the 99.9% interval so a reader can see the
     sampling error rather than take "matches" on trust.
  2. **Overall**, a chi-square goodness-of-fit against the declared percentages.
  3. **Within each prize**, that its own segments are chosen UNIFORMLY — the half of the model that
     decides where the wheel stops. A prize split 3 ways whose segments came up 50/25/25 would still
     give the right prize odds and would look wrong on the wheel, so it is checked separately.

⛔ WHY IT IMPORTS THE APP'S MODULE RATHER THAN REIMPLEMENTING THE DRAW: a distribution proved against a
   reimplementation proves nothing about what the app ships. If this file had its own weighted-choice, the
   two could drift and the measurement would still pass.

Usage:  python3 tools/measure_distribution.py [--draws 500000] [--seed 12345]
Exits non-zero if any check fails, so it can gate a deploy.
"""
import argparse, math, os, random, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from lib import model                                                    # noqa: E402


# ---------------------------------------------------------------- statistics, stdlib only
def norm_sf(z):
    """P(Z > z) for a standard normal. `math.erfc` is exact enough and is in the stdlib."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def chi2_sf(x, df):
    """Upper tail of the chi-square distribution: the regularised incomplete gamma Q(df/2, x/2).

    Series for the lower tail when x is small relative to a, Lentz continued fraction otherwise — the
    standard split, because the series converges slowly in exactly the region the fraction is good in.
    """
    a, xx = df / 2.0, x / 2.0
    if xx <= 0:
        return 1.0
    lg = math.lgamma(a)
    if xx < a + 1.0:                                    # series for P(a,x)
        term = 1.0 / a
        total = term
        n = 1
        while n < 10000:
            term *= xx / (a + n)
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
            n += 1
        return 1.0 - total * math.exp(-xx + a * math.log(xx) - lg)
    # continued fraction for Q(a,x)
    tiny = 1e-300
    b, c, d = xx + 1.0 - a, 1.0 / tiny, 1.0 / (xx + 1.0 - a)
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return h * math.exp(-xx + a * math.log(xx) - lg)


# ---------------------------------------------------------------- the configurations measured
def configs():
    """Awkward on purpose: the default set, the spec's own example, a heavy split, and a long tail."""
    def mk(name, raw):
        return name, [model.coerce_prize(p, i) for i, p in enumerate(raw)]
    return [
        mk("default (5/15/30/50, splits 1/2/3/4)", model.default_prizes()),
        mk("the spec's example: a 50% prize split 3 ways", [
            dict(id="a", name="Big", chance=50.0, splits=3),
            dict(id="b", name="Mid", chance=35.0, splits=2),
            dict(id="c", name="Small", chance=15.0, splits=1)]),
        mk("heavy splits, uneven odds", [
            dict(id="a", name="A", chance=62.5, splits=8),
            dict(id="b", name="B", chance=12.5, splits=4),
            dict(id="c", name="C", chance=12.5, splits=1),
            dict(id="d", name="D", chance=12.5, splits=2)]),
        mk("long tail incl. a 0.5% grand prize", [
            dict(id="g", name="Grand", chance=0.5, splits=1),
            dict(id="s", name="Second", chance=4.5, splits=1),
            dict(id="t", name="Third", chance=20.0, splits=2),
            dict(id="c", name="Consolation", chance=75.0, splits=6)]),
    ]


def measure(name, prizes, draws, seed):
    segs, meta = model.build_segments(prizes)
    print(f"\n{'=' * 96}\n{name}\n  {len(prizes)} prizes · {meta['segment_count']} segments · "
          f"declared total {meta['declared_total']:g}% · thinnest segment {meta['thinnest']:.2f}°")
    print(f"  slot order: {'-'.join(s['prize_id'] for s in segs)}")

    rng = random.Random(seed)
    counts = [0] * len(prizes)
    seg_counts = [[0] * p["splits"] for p in prizes]
    for _ in range(draws):
        pi, nth = model.draw(prizes, rng)
        counts[pi] += 1
        seg_counts[pi][nth] += 1

    fails = []
    # Bonferroni across every prize in this config, at a family-wise 0.1%.
    alpha = 0.001 / max(1, len(prizes))
    # z such that two-sided tail = alpha. Solved by bisection so no table is needed.
    lo, hi = 0.0, 12.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if 2 * norm_sf(mid) > alpha:
            lo = mid
        else:
            hi = mid
    zcrit = hi

    print(f"\n  {'prize':<14}{'configured':>11}{'observed':>11}{'count':>10}"
          f"{'99.9% interval':>22}{'z':>8}")
    chi2 = 0.0
    for i, p in enumerate(prizes):
        exp_p = p["chance"] / model.declared_total(prizes)
        obs_p = counts[i] / draws
        se = math.sqrt(exp_p * (1 - exp_p) / draws)
        z = (obs_p - exp_p) / se if se > 0 else 0.0
        chi2 += (counts[i] - draws * exp_p) ** 2 / (draws * exp_p)
        lo_i, hi_i = (exp_p - 3.29 * se) * 100, (exp_p + 3.29 * se) * 100
        flag = "" if abs(z) <= zcrit else "  <-- OUTSIDE"
        if abs(z) > zcrit:
            fails.append(f"{name}: {p['name']} observed {obs_p*100:.4f}% vs configured {exp_p*100:g}% "
                         f"(z={z:+.2f}, |z| limit {zcrit:.2f})")
        print(f"  {p['name'][:13]:<14}{exp_p*100:>10.4f}%{obs_p*100:>10.4f}%{counts[i]:>10}"
              f"{f'[{lo_i:.4f}, {hi_i:.4f}]':>22}{z:>+8.2f}{flag}")

    df = len(prizes) - 1
    pval = chi2_sf(chi2, df) if df > 0 else 1.0
    verdict = "consistent" if pval > 0.001 else "INCONSISTENT"
    print(f"\n  chi-square {chi2:.3f} on {df} df -> p = {pval:.4f}  ({verdict} with the declared odds)")
    if pval <= 0.001:
        fails.append(f"{name}: chi-square p={pval:.5f} rejects the declared distribution")

    # Segment uniformity within each prize (this is the "where does it stop" half of the model).
    print(f"\n  segment choice within each prize (expected uniform):")
    for i, p in enumerate(prizes):
        k = p["splits"]
        if k < 2:
            print(f"    {p['name'][:13]:<14}1 segment — nothing to test")
            continue
        n = counts[i]
        exp = n / k
        c2 = sum((c - exp) ** 2 / exp for c in seg_counts[i]) if exp > 0 else 0.0
        pv = chi2_sf(c2, k - 1)
        shares = " ".join(f"{c / n * 100:.2f}%" for c in seg_counts[i]) if n else "—"
        mark = "" if pv > 0.001 else "  <-- NOT UNIFORM"
        if pv <= 0.001:
            fails.append(f"{name}: {p['name']}'s {k} segments are not uniform (p={pv:.5f})")
        print(f"    {p['name'][:13]:<14}{k} segments  {shares}   chi2={c2:.2f} p={pv:.3f}{mark}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=500_000)
    ap.add_argument("--seed", type=int, default=20260923)
    a = ap.parse_args()
    print(f"MEASURED AGAINST app/lib/model.py — {a.draws:,} draws per configuration, seed {a.seed}")
    fails = []
    for i, (name, prizes) in enumerate(configs()):
        fails += measure(name, prizes, a.draws, a.seed + i)
    print("\n" + "=" * 96)
    if fails:
        print(f"FAILED — {len(fails)} check(s):")
        for f in fails:
            print("  · " + f)
        return 1
    print("ALL CONFIGURATIONS: observed frequencies match the configured percentages within sampling "
          "error, and segment choice within every prize is uniform.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
