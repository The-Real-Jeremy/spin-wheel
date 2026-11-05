/* THE DECELERATION MATHS, checked in isolation.
 *
 * The spin is operator-stopped, so the slow-down starts from whatever angle and speed the wheel happens to
 * be at when somebody presses STOP, and must still land exactly on the segment the draw chose. That is a
 * solved equation rather than an animation, and these are its four contracts:
 *
 *   · it lands EXACTLY on the target centre, from any starting angle and any speed;
 *   · it takes at least the configured minimum number of turns (so it reads as a wheel slowing down);
 *   · its FIRST instant matches the speed the wheel was already turning (no jolt at the press);
 *   · it never goes backwards except for the few degrees of deliberate settle at the very end.
 *
 * Run: node tests/test_spindown.mjs
 */
import { solveSpindown, spindownOffset, spinupSpeed } from '../app/static/js/wheel.js';

let fails = 0;
const ok = (c, m) => { console.log((c ? '  PASS  ' : '  FAIL  ') + m); if (!c) fails++; };

console.log('== the deceleration solver, over many (speed x target x start angle) combinations ==');
let cases = 0;
const problems = [];
for (const omega of [660, 594, 720, 300, 1200]) {
  for (const centre of [0, 17.3, 180, 271.5, 359.9]) {
    for (const theta0 of [0, 123.4, -456.7, 5000.2, 359.999]) {
      cases++;
      const plan = solveSpindown({ theta0, omega0: omega, targetCentre: centre, minTurns: 2,
                                   exponent: 3.4, targetSeconds: 5.0, segmentSize: 36 });
      const end = theta0 + spindownOffset(1, plan);
      const landed = (((-end) % 360) + 360) % 360;
      const err = Math.min(Math.abs(landed - centre), 360 - Math.abs(landed - centre));
      if (err > 1e-9) problems.push(`landing off by ${err} (omega ${omega}, centre ${centre}, theta0 ${theta0})`);
      if (plan.turns < 2 - 1e-9) problems.push(`only ${plan.turns.toFixed(3)} turns (omega ${omega})`);

      /* Entry speed: the offset's slope at t=0, in degrees per second.
       * ⛔ CONTINUITY IS CONDITIONAL ON THE CLAMP, and this assertion used to ignore that — it failed the
       *    moment `maxSeconds` was added, at 300 deg/s, which is correct: capping the duration is exactly
       *    the thing that trades the velocity match away. So the contract is stated in two halves here:
       *    continuity holds whenever the plan is NOT clamped, and the clamp cannot fire at cruise (checked
       *    separately below). Asserting continuity unconditionally would mean either deleting the clamp or
       *    weakening the test; neither is right. */
      const dtn = 1e-7;
      const v0 = spindownOffset(dtn, plan) / (dtn * plan.duration);
      if (!plan.clamped && Math.abs(v0 - omega) / omega > 0.01) {
        problems.push(`entry speed ${v0.toFixed(1)} vs ${omega} (${((v0 / omega - 1) * 100).toFixed(1)}%)`);
      }
      if (plan.clamped && omega >= 594) {
        problems.push(`clamped at ${omega} deg/s, which is at or above the arming speed`);
      }

      /* Monotonic through the main phase — no reversal while it is still slowing down. */
      let prev = -Infinity;
      for (let t = 0; t <= 0.9; t += 0.001) {
        const v = spindownOffset(t, plan);
        if (v < prev - 1e-9) { problems.push(`reversed at t=${t.toFixed(3)} (omega ${omega})`); break; }
        prev = v;
      }
    }
  }
}
ok(problems.length === 0, `${cases} combinations: land EXACTLY on the centre, >= 2 turns, monotonic to `
  + `t=0.9, entry speed within 1% of the wheel's speed whenever the plan is not clamped, and never `
  + `clamped at or above the arming speed`
  + (problems.length ? `\n         first problems: ${problems.slice(0, 4).join('; ')}` : ''));

console.log('\n== duration and overshoot at the speed this actually runs at ==');
const plan = solveSpindown({ theta0: 0, omega0: 660, targetCentre: 100, minTurns: 2,
                             exponent: 3.4, targetSeconds: 5.0, segmentSize: 36 });
console.log(`  cruise 660 deg/s -> ${plan.turns.toFixed(2)} turns over ${plan.duration.toFixed(2)}s, `
  + `overshoot ${plan.overshoot.toFixed(2)} deg`);
ok(plan.duration > 3 && plan.duration < 7.5, `a cruise-speed stop takes 3-7.5s (${plan.duration.toFixed(2)}s)`);

/* Every legal landing angle, to see the spread of durations an operator would actually experience. */
const durations = [];
for (let c = 0; c < 360; c += 3) {
  durations.push(solveSpindown({ theta0: 0, omega0: 660, targetCentre: c, minTurns: 2,
                                 exponent: 3.4, targetSeconds: 5.0, segmentSize: 36 }).duration);
}
const lo = Math.min(...durations), hi = Math.max(...durations);
console.log(`  across all 120 landing angles: ${lo.toFixed(2)}s to ${hi.toFixed(2)}s`);
ok(hi - lo < 2.5, `the spread of stop durations is under 2.5s (${(hi - lo).toFixed(2)}s), so it feels consistent`);
ok(lo > 3, `even the shortest stop takes over 3s (${lo.toFixed(2)}s)`);

const thin = solveSpindown({ theta0: 0, omega0: 660, targetCentre: 10, minTurns: 2,
                             exponent: 3.4, targetSeconds: 5, segmentSize: 2.5 });
ok(thin.overshoot <= 2.5 * 0.25 + 1e-9,
  `on a 2.5 deg sliver the overshoot is capped at a quarter of it (${thin.overshoot.toFixed(3)} deg) — so `
  + `the wheel cannot overshoot into the next prize and show it in the panel`);
ok(Math.abs(spindownOffset(1, plan) - plan.delta) < 1e-12,
  'the settle ends EXACTLY on delta, because cos(1.5*pi) is exactly zero');
ok(spindownOffset(0, plan) === 0, 'and starts exactly where the wheel already was');

console.log('\n== the clamp: what it costs, and that it cannot fire in the real flow ==');
const atArming = [];
for (let omega = 594; omega <= 900; omega += 6) {
  for (let c = 0; c < 360; c += 15) {
    atArming.push(solveSpindown({ theta0: c * 7.3, omega0: omega, targetCentre: c, minTurns: 2,
                                  exponent: 3.4, targetSeconds: 5.0, segmentSize: 36 }));
  }
}
ok(atArming.every((p) => !p.clamped),
  `across ${atArming.length} plans at or above the arming speed (594-900 deg/s, every landing angle), the `
  + `duration is NEVER clamped — so the velocity match always holds in the real flow`);
const slowClamped = solveSpindown({ theta0: 0, omega0: 60, targetCentre: 200, minTurns: 2, exponent: 3.4,
                                    targetSeconds: 5.0, segmentSize: 36 });
ok(slowClamped.clamped,
  'and a press at 60 deg/s DOES clamp — reported rather than hidden, which is why the page does not arm '
  + 'STOP until the wheel is at cruise and queues an early press instead of dropping it');

console.log('\n== the degenerate press: STOP the instant the wheel starts moving ==');
const early = solveSpindown({ theta0: 0, omega0: 30, targetCentre: 200, minTurns: 2,
                              exponent: 3.4, targetSeconds: 5.0, segmentSize: 36 });
ok(early.turns >= 2, `a press at 30 deg/s still turns at least 2 full turns (${early.turns.toFixed(2)}) `
  + `rather than flicking ${(200 / 360).toFixed(2)} of a turn to the answer`);
console.log(`  (it would take ${early.duration.toFixed(1)}s, which is why the page does not ARM stop until `
  + `the wheel is at cruise — see game.js)`);

console.log('\n== the spin-up ==');
ok(spinupSpeed(0, 660, 1.15) === 0, 'starts from a standstill');
ok(Math.abs(spinupSpeed(1.15, 660, 1.15) - 660) < 1e-9, 'reaches cruise exactly at the end of spin-up');
ok(spinupSpeed(9, 660, 1.15) === 660,
  'HOLDS cruise thereafter — it does not keep accelerating, which would strobe');
let last = -1, up = true;
for (let t = 0; t <= 1.15; t += 0.005) { const v = spinupSpeed(t, 660, 1.15); if (v < last) up = false; last = v; }
ok(up, 'monotonically increasing through the spin-up');
const a1 = spinupSpeed(0.02, 660, 1.15) - spinupSpeed(0.01, 660, 1.15);
const a2 = spinupSpeed(1.15, 660, 1.15) - spinupSpeed(1.14, 660, 1.15);
ok(a1 < 1 && a2 < 1, 'acceleration is near zero at BOTH ends (no jolt starting, no corner at the top)');

console.log(`\n${fails ? 'FAILURES: ' + fails : 'ALL GREEN'}  (${fails} failures)`);
process.exit(fails ? 1 : 0);
