# Why this is built this way

Seven decisions, each with the alternative that was rejected and the reason. Written down because most of
them are invisible in the code and every one of them would be re-argued otherwise.

---

## 1. The draw decides; the animation presents it

There are two ways to build a prize wheel:

**(A) Physics decides.** Spin with a random impulse, decelerate, and whatever segment the pointer lands on
is the winner. The declared percentages are then only as true as the segment boundaries and the stopping
distribution happen to make them.

**(B) The draw decides.** Pick the winning prize by a weighted random draw over the declared percentages,
pick one of that prize's segments uniformly, then animate the wheel to land exactly there.

**This is (B).** The reason is that it makes the wrong answer *unrepresentable* rather than merely unlikely.
Under (B) the realised distribution **is** the declared distribution by construction — one `random.random()`
against a cumulative sum, and nothing downstream can bend it. Under (A) the realised distribution is an
emergent property of floating-point boundaries and a deceleration model, so it can drift while everything
still looks right. This is a real giveaway at a hosted event, where *"the odds we configured are the odds
that happened"* is the one property that must hold. It is also what commercial prize wheels do.

Two things (B) buys that are worth naming:

* **The arrangement is cosmetic.** Where a prize's segments sit changes nothing about its chance. That is
  what lets §2 optimise the layout for looks without anybody re-arguing the odds. Under (A) the layout
  *is* the odds.
* **`splits` costs nothing in fairness.** A 50% prize split three ways is three segments of 16.67%; the draw
  still picks the *prize* at 50% and only then picks which of its three segments to stop on.

**Measured, not argued.** `tools/measure_distribution.py` runs the real `model.draw()` 500,000 times against
each of four awkward configurations — a dominant prize, heavy splits, a long tail with a 0.5% grand prize —
and checks per-prize frequencies with a Bonferroni-corrected binomial z-score, the whole configuration with
a chi-square, and segment choice *within* each prize for uniformity. All consistent (`p` 0.14–0.81). One
uniformity `p` came back at 0.006, so that prize was re-run over 12 seeds (min `p` 0.11) to confirm sampling
noise rather than bias, rather than leaving a number a reader would reasonably query.

---

## 2. The arrangement has a stated objective, and it is lexicographic

the spec: a prize split *k* ways gets *k* segments *"as evenly spaced from each other as possible"*.
"As possible" is doing real work — with mixed split counts and unequal widths, perfect spacing is generally
unreachable — so this needs an objective rather than a vibe.

**The objective, in this order:**
1. minimise the number of **adjacent pairs of segments belonging to the same prize**;
2. then minimise `Σ ((gap − 360/k) / (360/k))²` over each prize's cyclic centre-to-centre gaps.

**Why lexicographic and not one blended score.** Adjacency is the specific visible defect: two segments of
one prize touching merge into one block, which is exactly what splitting is meant to prevent. A single
weighted score will trade one adjacency for a small evenness gain, and that trade is never the one wanted.
Ranking adjacency first makes it unrepresentable.

**Why *relative* deviation.** 10° off a 180° ideal is invisible; 10° off a 45° ideal is obvious. Absolute
squared error ranks those equal, which is the wrong ranking.

**The algorithm.** A greedy angular pass (always place whichever prize is most *overdue in angle*, tried
from four phase offsets) then bounded steepest-descent local search over two move types. **Both move types
are needed, and that was measured rather than assumed**: with 2-swaps alone the default four-prize wheel
settled at a deviation of 0.3613 while exhaustive search over all 12,600 arrangements found 0.2613 — a real
local minimum, 38% worse. Adding the **insertion** move (lift one segment out, drop it elsewhere) reaches
the exhaustive optimum on every configuration small enough to enumerate. `tests/test_arrangement.py` pins
that comparison, so simplifying back to swaps-only fails a test instead of quietly degrading the wheel.

**When it is impossible, it says so.** On a cycle of *N* slots, *k* segments of one prize form at most
`min(k, N−k)` runs, so at least `max(0, 2k − N)` of its pairs must touch. That floor is computed, compared
with what was achieved, and reported on the settings page by name — *which* prize, *how many* must touch,
that this is the best possible, and what to change. A constraint that cannot be satisfied is reported, not
quietly dropped. Measured: the arrangement hits that floor **exactly** in all four impossible configurations
tested, and zero avoidable adjacency everywhere else.

Deterministic (no RNG) so a reload does not reshuffle the host's layout, and memoised — the 72-segment worst
case takes 75ms once, then 0.1ms.

---

## 3. The spin is operator-stopped, and the deceleration is solved rather than scheduled

the owner's model: the wheel accelerates and keeps going; a contestant presses **Stop**; it decelerates onto
their prize.

**The winner is drawn at the moment Stop is pressed.** Two reasons, and the second is the one that matters:
it reads as honest — the contestant's press produces the outcome — and a winner drawn at spin-up would have
to live somewhere for the whole spin. Anywhere the client can see it, the prize is inspectable before the
wheel stops, with people standing at the laptop. Drawing on the stop request removes that entirely, and the
distribution is identical either way.

**The wheel cruises; it does not keep accelerating.** Taken literally, "stops accelerating" means an
ever-faster wheel, which becomes a strobing blur that reads as broken. It eases to 660°/s (1.8 turns a
second) and holds: visibly alive and fast, checked on screen at that speed for readability.

**The deceleration is four guarantees, each structural rather than tuned:**

* **It lands exactly.** `delta` is chosen from the congruence class that puts the target centre under the
  pointer (`delta ≡ −centre − θ₀ mod 360`), so the landing is exact by construction, not by a tolerance.
* **No speed change at the press.** With `e(u) = 1 − (1−u)^n` the initial slope is fixed, so rather than
  picking a duration and accepting a jolt, the **duration is derived**:
  `T = (delta + overshoot)·n / (0.9·ω₀)`. The first frame of the deceleration continues at exactly the speed
  the wheel was already turning. Duration is therefore a *consequence*; `targetSeconds` only chooses which
  legal `delta` to use.
* **It never reverses and always takes ≥ 2 turns**, so it reads as a wheel slowing down rather than being
  switched off.
* **It settles rather than stopping dead.** The last tenth is a damped cosine that is *exactly* zero at the
  end, because `cos(1.5π) = 0` — so the settle cannot leave the wheel a fraction of a degree off. The
  overshoot is capped at a quarter of the **target segment's own width**, so the wheel can never overshoot
  far enough to show the next prize in the panel before coming back.

**The degenerate press** — Stop a fifth of a second in — is handled twice. Stop does not *arm* until the
wheel is at cruise, and a press during spin-up is **queued** and honoured the instant it arms, rather than
silently dropped (a dropped press looks like a broken button at an event). The solver also exposes a
`clamped` flag for the impossible case, and the browser harness asserts it stays false on every real spin.

**Stop is idempotent three times over**, because it is a physical button pressed by an excited stranger: the
state machine refuses a second press, the draw request carries a token the server answers identically on a
replay (measured: three calls, one `spin_id`, one log row), and `e.repeat` drops a held key.

---

## 4. The enlarged panel is derived from the wheel's rotation

Every frame: `idx = wheel.segmentAt(wheel.theta)`, looked up in the geometry **the server computed**, using
the same `theta` the renderer just drew with. Not a timer, not a precomputed schedule, and never the known
winner. So *"the panel names one prize while the result announces another"* is not a reachable state.

The geometry lives on the server and is shipped to the browser as data, so the picture, the pointer readout
and the draw cannot disagree about where a boundary is. The JS `segmentAt` is a two-line mirror of
`model.segment_at`, and both are pinned by tests.

**What happens at speed, decided deliberately.** At 660°/s a boundary passes every ~25ms and a cross-fade
per boundary would strobe. So there are two regimes, chosen from the *measured* angular speed: above 300°/s
switch instantly with a speed-scaled motion blur (reads as a reel); below it cross-fade, with a 90ms floor
so no fade is faster than the eye can follow and a ceiling of the segment's own dwell time so a fade never
outlives the segment that started it.

The current segment is also **highlighted on the wheel**, drawn from the same `segmentAt()` — so the
relationship is visible on screen and a disagreement would be obvious instantly. And the page carries the
assertion itself: at rest, if the panel and the draw disagree, it **says so** rather than announcing one
thing while showing another.

---

## 5. Canvas 2D, not WebGL — and one `drawImage` per frame

A tilted disc is one linear transform, so a 3D engine buys nothing and costs a vendored megabyte that has to
be served from the app (there is no CDN by design). The 3D reading comes from four cheap things: a 20° tilt,
an extruded rim whose per-segment colours travel with the wheel, directional shading, and a contact shadow.

**How it stays smooth.** The wheel *face* is rendered once into an offscreen canvas in flat wheel space —
segments, medallions, names and all — and re-rendered only when the prize list or the size changes. Per
frame the only work is `translate → scaleY(cos tilt) → rotate(θ) → drawImage(face)`, because
`screen = translate ∘ scaleY ∘ rotate ∘ wheelspace` decomposes exactly. Everything else per frame (rim
slices, gloss, hub, pointer, highlight) is a handful of paths. Nothing touches layout: transforms and alpha
only, never `left`/`top`/`width`.

**Measured: 2806 frames, zero over 20ms, worst single frame 17.6ms, mean 114 fps** against the deployed
origin. An earlier run reported **7 frames over 50ms with a worst of 208ms** — that was the *harness*: CDP
`Runtime.evaluate` polling every 150ms plus a mid-spin screenshot stall the main thread. It was settled by
running both ways in one session and printing them side by side, and the harness now does no CDP work
between Stop and settle. The deceleration is reported separately from the whole spin, because it is the part
the room watches and an average over the whole spin would hide a hitch in it.

---

## 6. Durable state in the service principal's workspace home

A Databricks App's container filesystem does not survive a restart, and it fails **asymmetrically**: on this
rig `apps deploy` preserved a local file while `apps stop` + `apps start` recreated it **empty**. For a prize
wheel that is the worst available failure — the host configures ten prizes and their photos, something
restarts the app, and the wheel comes up with placeholders. Nothing errors.

So the store is `/Users/<DATABRICKS_CLIENT_ID>/prize-wheel-state/`, written as the app's own service
principal with the credentials Apps injects. No grant, no group membership, no extra scope. It is also
*auditable*: an admin can list, read and back up a workspace file, and cannot see a file inside a container.

**Binary round-trip byte-identity was measured before the design depended on it** — a PNG imported with
`format: AUTO` comes back byte-identical from `export?direct_download=true`, as `object_type: FILE`, and the
per-object ceiling is exactly **10,485,760 bytes** (hence a 2 MB picture cap, comfortably under).

Rejected: **`/Shared`** (a stock workspace grants `users: CAN_MANAGE`, so any *user of the app* could rewrite
the odds); **a UC Volume** (needs grants, and `/Volumes` is not mountable in an Apps container — it would
mean an adapter over HTTPS anyway); **the container filesystem** (above).

**Three read states, not two.** `present` / `absent` / `could-not-determine`. A store that cannot be read
must never render as empty: *"nothing is configured"* and *"I could not look"* are different answers, and
conflating them puts a wheel of placeholders on a stage with nothing on screen saying so. The game page
renders all three differently, and `/api/health` reports which is in force.

---

## 7. Two smaller ones, stated so they are not mistaken for oversights

**The percentage rule is a refusal, not a normalisation.** A save whose chances do not total 100 is refused
**with the actual total named**. Silently normalising would mean the number the host typed is not the number
in force, with nothing on screen saying so. *Balance to 100%* rewrites the visible numbers *before* saving,
so the odds they can see are always the odds that will be used. (The geometry still spans 360° for an
externally-tampered config, and a banner names the real total — the circle has to close.)

**The picture trust boundary is where it is, and no further.** The browser re-encodes (canvas → WebP, long
edge 900px, EXIF and any trailing payload gone); the server has no image codec, so it *admits or refuses*:
magic-byte format sniff, byte cap, and pixel dimensions parsed out of the header. It serves what it
**sniffed**, never what the filename claimed, and the deploy verifier reads the `Content-Type` off the live
response rather than trusting this paragraph. SVG is refused outright — it is the one image format that can
carry script, and there is no upside for a photo of a prize.

---

## Things deliberately not built

* **No authentication on the settings page.** Removed on request (2026-09-23); the threat model is that this
  runs on the hosts' own laptops and the contestant never drives it. Removed *entirely* rather than disabled
  behind a flag, because a half-removed gate is how one comes back as a bug.
* **No per-prize stock or "remove after won".** Not asked for, and it would change the odds mid-event, which
  is a materially different product.
* **No multi-wheel or multi-event support.** One wheel, one config, one log.
* **No sound files.** Ticks and the win chime are synthesised with WebAudio; muted by default.
