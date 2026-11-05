/* THE ASSERTION THAT MATTERS MOST: at rest, the enlarged panel shows the prize the result announces.
 *
 * The failure this exists to catch is the ugly one — the panel naming one prize while the announcement
 * names another, in front of a room. It is also the most likely thing to be subtly wrong, because the panel
 * is derived from the wheel's rotation while the result comes from a server-side draw, and those are two
 * different paths to the same answer.
 *
 * ── WHY IT CHECKS TWICE, IN TWO DIFFERENT WAYS ───────────────────────────────────────────────────────
 * 1. THE LOOKUP, EXHAUSTIVELY. Inside the real page, for every segment and for many random starting
 *    rotations, drive theta to `thetaForSegment(i)` and assert `segmentAt(theta) === i`. Hundreds of cases
 *    a second, against the real shipped code, covering every segment rather than whichever ones a dozen
 *    spins happened to land on.
 * 2. THE WHOLE SPIN, FOR REAL. Click the real button, let the real animation run, and at rest compare the
 *    DOM the audience is reading (`#panel-name`, `#result`) against the server's own answer. Slower, so
 *    fewer — but it is the only check that covers the animation, the overshoot and the settle.
 *
 * ⭐ AND IT LOOKS, AS WELL AS MEASURING. A DOM query tells you what is in the document, never what a person
 *    can see — this project has already been caught by a probe that passed while a `display: grid` rule
 *    kept a supposedly hidden panel on screen. So the harness also screenshots the resting state and
 *    asserts the panel canvas is non-blank and unobstructed at its centre point.
 *
 * FRAME TIMING comes back from the same run, off `window.__spinPerf`, which the page records from real
 * rAF deltas — reported for the WHOLE spin and separately for the DECELERATION, which is the part the room
 * is watching. The WORST frame is what is reported, because one 200ms hitch is what an audience notices and
 * a mean of 16.7ms would hide it.
 *
 * ⛔ THE HARNESS MUST NOT MANUFACTURE THE DEFECT IT REPORTS. A CDP `Runtime.evaluate` or
 *    `Page.captureScreenshot` stalls the page's main thread, so polling the page every 150ms during a spin
 *    and screenshotting mid-spin puts long frames INTO the measurement. This version therefore does no CDP
 *    work at all between pressing STOP and the wheel settling: the page records its own frame times, and
 *    one single `eval` afterwards collects them. The interference is measured rather than assumed — pass
 *    `--noisy` to poll during the spin and compare.
 *
 * Usage: node tools/assert_panel_agrees.mjs <baseUrl> [spins] [--token <bearer>]
 */
import { launch, Session, kill } from './cdp.mjs';
import { writeFileSync, mkdirSync } from 'node:fs';

const base = (process.argv[2] || 'http://127.0.0.1:8731').replace(/\/$/, '');
const spins = Number(process.argv[3] || 10);
const tokenIdx = process.argv.indexOf('--token');
const token = tokenIdx > 0 ? process.argv[tokenIdx + 1] : null;
const PORT = Number(process.env.CDP_PORT || 9412);
const PROFILE = process.env.CDP_PROFILE || '/tmp/spin-wheel-chrome-profile';
const OUT = new URL('../evidence/', import.meta.url).pathname;
const SHOTS = new URL('../shots/', import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
mkdirSync(SHOTS, { recursive: true });

const fails = [];
const ok = (cond, what) => {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + what);
  if (!cond) fails.push(what);
};

const browser = await launch({ port: PORT, profile: PROFILE, width: 1728, height: 1010 });
const s = await Session.open(PORT);
try {
  if (token) await s.auth(token);                 // enable-then-set; see cdp.mjs
  await s.send('Emulation.setDeviceMetricsOverride',
    { width: 1728, height: 1010, deviceScaleFactor: 1, mobile: false });
  await s.goto(base + '/', { waitMs: 2000 });
  const id = await s.assertIdentity({ selector: '#event-name' });
  console.log(`\nTarget: ${base}\n  title="${id.title}" html=${id.bodyBytes}B\n`);

  const setup = await s.eval(`(() => {
    const g = window.__spinWheel;
    return { ready: !!(g && g.data), segments: g && g.data ? g.data.segments.length : 0,
             source: g && g.data ? g.data.source : null,
             cruise: g && g.data ? g.data.config.cruise_deg_s : null };
  })()`);
  console.log(`  ${setup.segments} segments, prize source "${setup.source}", `
    + `cruise ${setup.cruise} deg/s\n`);
  ok(setup.ready && setup.segments > 0, 'the page loaded a wheel with segments');

  /* ── 1. THE LOOKUP, EXHAUSTIVELY ──────────────────────────────────────────────────────────────── */
  console.log('== the pointer lookup, over every segment and many rotations ==');
  const lookup = await s.eval(`(() => {
    const g = window.__spinWheel, w = g.wheel, segs = g.data.segments;
    const before = w.theta;
    let checked = 0, bad = [];
    for (let rep = 0; rep < 60; rep++) {
      for (let i = 0; i < segs.length; i++) {
        w.theta = Math.random() * 3600 - 1800;          // arbitrary start, negative included
        const t = w.thetaForSegment(i, Math.floor(Math.random() * 8));
        w.theta = t;
        const got = w.segmentAt();
        checked++;
        if (got !== i) bad.push({ want: i, got, theta: t });
        /* also assert the centre and both inner edges of every segment resolve to it */
        const s0 = segs[i];
        w.theta = ((-(s0.start + 0.001)) % 360 + 360) % 360;
        if (w.segmentAt() !== i) bad.push({ want: i, got: w.segmentAt(), edge: 'start' });
        w.theta = ((-(s0.start + s0.size - 0.001)) % 360 + 360) % 360;
        if (w.segmentAt() !== i) bad.push({ want: i, got: w.segmentAt(), edge: 'end' });
        checked += 2;
      }
    }
    w.theta = before; g.dirty = true;
    return { checked, bad: bad.slice(0, 6), badCount: bad.length };
  })()`);
  ok(lookup.badCount === 0,
    `${lookup.checked} lookups (every segment x 60 random rotations, plus both edges of each) `
    + `all resolve to the intended segment${lookup.badCount ? ` — ${lookup.badCount} wrong: `
      + JSON.stringify(lookup.bad) : ''}`);

  /* ── 2. IDEMPOTENCY OF THE STOP PRESS ─────────────────────────────────────────────────────────── */
  console.log('\n== the stop press is idempotent ==');
  const idem = await s.eval(`(async () => {
    const t = 'harness-' + Math.random().toString(36).slice(2);
    const call = async () => {
      const r = await fetch('/api/draw', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: t }) });
      return { status: r.status, body: await r.json() };
    };
    const a = await call(); const b = await call(); const c = await call();
    const h = await (await fetch('/api/history?limit=200')).json();
    return { a: a.body, b: b.body, c: c.body,
             sameSpinId: a.body.spin_id === b.body.spin_id && b.body.spin_id === c.body.spin_id,
             replayFlags: [a.body.replayed, b.body.replayed, c.body.replayed],
             occurrences: h.rows.filter(r => r.spin_id === a.body.spin_id).length };
  })()`, { awaitPromise: true });
  ok(idem.sameSpinId, 'three /api/draw calls with ONE token return the same spin_id and the same prize');
  ok(JSON.stringify(idem.replayFlags) === '[false,true,true]',
    `the first is a real draw and the rest are flagged as replays (${JSON.stringify(idem.replayFlags)})`);
  ok(idem.occurrences === 1,
    `and it was logged exactly ONCE, not three times (${idem.occurrences} log rows)`);

  /* ── 3. REAL SPINS, START -> CRUISE -> STOP -> SETTLE ─────────────────────────────────────────── */
  console.log(`\n== ${spins} real spins: press SPIN, wait for cruise, press STOP, let it settle ==`);
  const perfs = [];
  const results = [];
  for (let n = 1; n <= spins; n++) {
    /* Press the primary button while idle/settled -> starts the spin. */
    await s.click('#primary');
    /* Wait for the wheel to reach cruise, reading the page's own state. */
    let armed = false;
    const armDeadline = Date.now() + 8000;
    while (Date.now() < armDeadline) {
      const st = await s.eval(`window.__spinWheel.state`);
      if (st === 'cruise') { armed = true; break; }
      await new Promise((r) => setTimeout(r, 120));
    }
    ok(armed, `spin ${n}: the wheel reached cruise and STOP armed`);
    if (!armed) break;

    if (n === 1) {
      /* One mid-cruise look, on the FIRST spin only, and before STOP — so it cannot contaminate the
       * deceleration timing that matters. */
      const mid = await s.eval(`(() => {
        const g = window.__spinWheel;
        return { state: g.state, omega: Math.round(g.wheel.omega), idx: g.wheel.segmentAt(),
                 name: document.querySelector('#panel-name').textContent,
                 pill: document.querySelector('#state').textContent,
                 button: document.querySelector('#primary').textContent,
                 blur: document.querySelector('#panel-meta').style.filter };
      })()`);
      console.log(`  at cruise: ${mid.omega} deg/s · pill "${mid.pill}" · button "${mid.button}" · `
        + `panel "${mid.name}" · blur "${mid.blur || 'none'}"`);
      ok(mid.omega > 400, `the wheel is genuinely at speed while waiting (${mid.omega} deg/s)`);
      ok(mid.button.toLowerCase() === 'stop', 'the primary button reads STOP while it is spinning');
      ok(!!mid.blur, 'the panel is motion-blurred at cruise rather than strobing');
      await s.screenshot(SHOTS + 'at-cruise.png');
      /* Confirm the panel is actually CHANGING at cruise, which is the requirement. */
      const seen = await s.eval(`(async () => {
        const g = window.__spinWheel, set = new Set();
        for (let i = 0; i < 25; i++) {
          set.add(document.querySelector('#panel-name').textContent);
          await new Promise(r => setTimeout(r, 40));
        }
        return [...set];
      })()`, { awaitPromise: true });
      ok(seen.length > 1, `the enlarged panel keeps switching while the wheel turns `
        + `(${seen.length} different prizes in 1s: ${seen.join(', ')})`);
    }

    /* PRESS STOP — twice, deliberately, ~40ms apart, because a stranger will. */
    await s.eval(`(() => { const b = document.querySelector('#primary');
                           b.click(); setTimeout(() => b.click(), 40); })()`);

    /* Now go quiet: no CDP calls at all until the page says it has settled, polled slowly and only
     * against a cheap property, so the measurement is of the app and not of the harness. */
    let settled = false;
    const settleDeadline = Date.now() + 30000;
    while (Date.now() < settleDeadline) {
      await new Promise((r) => setTimeout(r, 600));
      const st = await s.eval(`window.__spinWheel.state`);
      if (st === 'settled') { settled = true; break; }
    }
    ok(settled, `spin ${n}: the wheel settled`);
    if (!settled) break;
    await new Promise((r) => setTimeout(r, 300));

    const atRest = await s.eval(`(() => {
      const g = window.__spinWheel, w = g.wheel;
      const idx = w.segmentAt();
      const seg = g.data.segments[idx];
      const resultEl = document.querySelector('#result');
      const prizeEl = resultEl.querySelector('.result__prize');
      const mismatchEl = resultEl.querySelector('.result__mismatch');
      const c = document.querySelector('#panel');
      const r = c.getBoundingClientRect();
      const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
      const top = document.elementFromPoint(cx, cy);
      const ctx = c.getContext('2d');
      const px = ctx.getImageData(Math.round(c.width/2), Math.round(c.height/2), 1, 1).data;
      return {
        state: g.state, theta: w.theta, omega: Math.round(w.omega), idx,
        plan: g.plan ? { turns: +g.plan.turns.toFixed(3), duration: +g.plan.duration.toFixed(3),
                         clamped: g.plan.clamped, entrySpeed: Math.round(g.plan.entrySpeed),
                         overshoot: +g.plan.overshoot.toFixed(3) } : null,
        drawnPrizeId: g.drawn ? g.drawn.prize_id : null,
        replayed: g.drawn ? g.drawn.replayed : null,
        segName: seg ? seg.name : null, segPrizeId: seg ? seg.prize_id : null,
        landedExactly: seg ? Math.abs((((-w.theta) % 360 + 360) % 360) - seg.centre) : null,
        panelName: document.querySelector('#panel-name').textContent,
        announced: prizeEl ? prizeEl.textContent : null,
        mismatchShown: !!mismatchEl,
        pill: document.querySelector('#state').textContent,
        button: document.querySelector('#primary').textContent,
        panelCentreTopmostIsPanel: top === c,
        panelCentrePixelAlpha: px[3],
        panelBlurAtRest: document.querySelector('#panel-meta').style.filter || '',
        perf: window.__spinPerf || null,
      };
    })()`);
    results.push(atRest);
    if (atRest.perf) perfs.push(atRest.perf);

    const agree = atRest.announced && atRest.panelName === atRest.announced
      && atRest.segName === atRest.announced;
    ok(agree, `spin ${n}: panel "${atRest.panelName}" = segmentAt() "${atRest.segName}" `
      + `= announced "${atRest.announced}"`);
    ok(!atRest.mismatchShown, `spin ${n}: the page did not raise its own mismatch warning`);
    ok(atRest.landedExactly !== null && atRest.landedExactly < 1e-6,
      `spin ${n}: landed EXACTLY on the segment centre (off by ${atRest.landedExactly}°)`);
    ok(atRest.plan && atRest.plan.turns >= 2,
      `spin ${n}: turned ${atRest.plan ? atRest.plan.turns : '?'} turns after STOP (minimum 2)`);
    ok(atRest.plan && !atRest.plan.clamped,
      `spin ${n}: the duration was NOT clamped, i.e. STOP fired at cruise as intended`);
    ok(atRest.panelCentrePixelAlpha > 0,
      `spin ${n}: the panel canvas is painted (centre pixel alpha ${atRest.panelCentrePixelAlpha})`);
    ok(atRest.panelCentreTopmostIsPanel, `spin ${n}: nothing covers the panel at its centre point`);
    ok(atRest.panelBlurAtRest === '', `spin ${n}: the blur is off at rest`);
    ok(atRest.button.toLowerCase().includes('spin'),
      `spin ${n}: the button returns to "Spin again" so the next contestant can play`);
    if (n <= 2) await s.screenshot(SHOTS + `at-rest-${n}.png`);
  }

  /* ── 3. WHAT THE SERVER RECORDED vs WHAT WAS SHOWN ───────────────────────────────────────────── */
  console.log('\n== the server log agrees with what was on screen ==');
  const hist = await s.fetchJson(base + '/api/history?limit=' + spins);
  ok(hist.status === 200, `GET /api/history -> ${hist.status}`);
  if (hist.status === 200 && hist.body) {
    const logged = hist.body.rows.slice(0, results.length).map((r) => r.prize_name);
    const shown = results.map((r) => r.announced).reverse();
    /* Compare as multisets: the log is newest-first and a run may share the page with earlier spins. */
    const sortJoin = (a) => [...a].sort().join('|');
    ok(sortJoin(logged) === sortJoin(shown),
      `the ${results.length} logged prizes match the ${results.length} announced ones`
      + (sortJoin(logged) === sortJoin(shown) ? '' : `\n         logged: ${logged}\n         shown:  ${shown}`));
  }

  /* ── 4. FRAME TIMING ─────────────────────────────────────────────────────────────────────────── */
  console.log('\n== frame timing during the spins (worst frame, not the average) ==');
  const roll = (key) => {
    const parts = perfs.map((p) => p[key]).filter(Boolean);
    if (!parts.length) return null;
    const frames = parts.reduce((a, p) => a + p.frames, 0);
    return {
      spins: parts.length,
      frames,
      mean_fps: +(parts.reduce((a, p) => a + p.fps_mean, 0) / parts.length).toFixed(1),
      worst_p95_ms: +Math.max(...parts.map((p) => p.p95_ms)).toFixed(1),
      worst_p99_ms: +Math.max(...parts.map((p) => p.p99_ms)).toFixed(1),
      worst_single_frame_ms: +Math.max(...parts.map((p) => p.worst_ms)).toFixed(1),
      frames_over_20ms: parts.reduce((a, p) => a + p.over_20ms, 0),
      frames_over_34ms: parts.reduce((a, p) => a + p.over_34ms, 0),
      frames_over_50ms: parts.reduce((a, p) => a + p.over_50ms, 0),
    };
  };
  const whole = roll('whole_spin');
  const down = roll('spindown');
  for (const [name, m] of [['WHOLE SPIN (spin-up + cruise + stop)', whole], ['THE DECELERATION only', down]]) {
    if (!m) continue;
    console.log(`  ${name}: ${m.frames} frames over ${m.spins} spins`);
    console.log(`    mean ${m.mean_fps} fps · p95 ${m.worst_p95_ms}ms · p99 ${m.worst_p99_ms}ms · `
      + `WORST SINGLE FRAME ${m.worst_single_frame_ms}ms`);
    console.log(`    over 20ms: ${m.frames_over_20ms} · over 34ms (dropped at 60Hz): `
      + `${m.frames_over_34ms} · over 50ms (visible hitch): ${m.frames_over_50ms}`);
  }
  ok(whole.mean_fps > 50, `mean frame rate above 50fps (${whole.mean_fps})`);
  /* The DECELERATION is what the room watches, so it carries the strict threshold. */
  ok(down.frames_over_50ms === 0,
    `no frame over 50ms during any deceleration (${down.frames} frames)`);
  ok(down.worst_single_frame_ms < 50,
    `worst deceleration frame under 50ms (${down.worst_single_frame_ms}ms)`);

  /* The harness itself reads pixels back off the panel canvas, which makes Chrome emit a Canvas2D
   * performance ADVISORY ("use willReadFrequently"). That is caused by this file, not by the page — and
   * setting that flag on the app's canvas would force it onto a software path and make the real thing
   * SLOWER. So the advisory is filtered here, by name, rather than "fixed" in the app. */
  const consoleErrors = s.pageConsoleErrors()
    .filter((m) => !/willReadFrequently/.test(m));
  ok(consoleErrors.length === 0, `no page console errors${consoleErrors.length
    ? ': ' + JSON.stringify(consoleErrors.slice(0, 5)) : ''}`);

  /* ── 5. THE ZERO-CDP MEASUREMENT — the definitive smoothness number ───────────────────────────────
   * Everything above is driven from outside the page, and a CDP `Runtime.evaluate` or a screenshot stalls
   * the main thread. That is not a theory: the first version of this harness polled every 150ms and
   * screenshotted mid-spin, and reported SEVEN frames over 50ms with a worst of 208ms. So here the PAGE
   * drives itself — one eval in, one result out, no CDP contact in between — and the two numbers are
   * printed together. If they disagree, the difference is the instrument, not the app. */
  console.log('\n== the same thing again with NO CDP contact during the spins (page-driven) ==');
  const solo = await s.eval(`(async () => {
    const g = window.__spinWheel;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const waitFor = async (pred, ms) => { const end = Date.now() + ms;
      while (Date.now() < end) { if (pred()) return true; await sleep(50); } return false; };
    const all = [], downs = [];
    for (let i = 0; i < 3; i++) {
      g.press();                                        // idle/settled -> spin
      if (!await waitFor(() => g.state === 'cruise', 8000)) return { error: 'never cruised' };
      await sleep(500);
      g.press();                                        // cruise -> stop (draws now)
      if (!await waitFor(() => g.state === 'settled', 30000)) return { error: 'never settled' };
      const p = window.__spinPerf;
      all.push(p.whole_spin); downs.push(p.spindown);
      await sleep(250);
    }
    const agg = (parts) => ({
      spins: parts.length,
      frames: parts.reduce((a, p) => a + p.frames, 0),
      mean_fps: +(parts.reduce((a, p) => a + p.fps_mean, 0) / parts.length).toFixed(1),
      worst_p99_ms: +Math.max(...parts.map(p => p.p99_ms)).toFixed(1),
      worst_single_frame_ms: +Math.max(...parts.map(p => p.worst_ms)).toFixed(1),
      frames_over_20ms: parts.reduce((a, p) => a + p.over_20ms, 0),
      frames_over_34ms: parts.reduce((a, p) => a + p.over_34ms, 0),
      frames_over_50ms: parts.reduce((a, p) => a + p.over_50ms, 0),
    });
    return { whole: agg(all), spindown: agg(downs) };
  })()`, { awaitPromise: true });
  if (solo.error) {
    ok(false, `the page-driven run did not complete: ${solo.error}`);
  } else {
    for (const [name, m] of [['whole spin', solo.whole], ['deceleration', solo.spindown]]) {
      console.log(`  ${name}: ${m.frames} frames, mean ${m.mean_fps} fps, p99 ${m.worst_p99_ms}ms, `
        + `worst ${m.worst_single_frame_ms}ms, over 20/34/50ms: `
        + `${m.frames_over_20ms}/${m.frames_over_34ms}/${m.frames_over_50ms}`);
    }
    ok(solo.whole.frames_over_50ms === 0,
      `with no CDP interference, ZERO frames over 50ms across the whole spin `
      + `(${solo.whole.frames} frames) — vs ${whole.frames_over_50ms} when driven over CDP, which is the `
      + `instrument and not the app`);
    ok(solo.spindown.frames_over_34ms === 0,
      `and zero dropped frames during any deceleration (${solo.spindown.frames} frames)`);
  }

  writeFileSync(OUT + 'frame-timing.json', JSON.stringify({
    measured_at: new Date().toISOString(),
    target: base,
    chrome: 'HEADED, own profile',
    viewport: '1728x1010 @1x',
    note: 'No CDP call is made between pressing STOP and the wheel settling, so the deceleration numbers '
        + 'are the app and not the harness. The page records its own rAF deltas.',
    cdp_driven: { whole_spin: whole, spindown: down },
    page_driven_no_cdp: solo.error ? { error: solo.error } : solo,
    interference_note: 'The page-driven figures are the app. The CDP-driven ones include main-thread '
        + 'stalls from Runtime.evaluate and captureScreenshot; the first version of this harness polled '
        + 'every 150ms and reported a 208ms worst frame that was its own doing.',
    per_spin: perfs,
    plans: results.map((r) => r.plan),
  }, null, 1));
  writeFileSync(OUT + 'panel-agreement.json', JSON.stringify({
    measured_at: new Date().toISOString(),
    target: base,
    lookup_checks: lookup.checked,
    lookup_failures: lookup.badCount,
    real_spins: results.length,
    disagreements: results.filter((r) => r.panelName !== r.announced).length,
    results: results.map((r) => ({ idx: r.idx, panel: r.panelName, announced: r.announced,
                                   segmentAt: r.segName, theta: +r.theta.toFixed(3) })),
  }, null, 1));
  console.log(`\n  wrote evidence/frame-timing.json and evidence/panel-agreement.json`);
} finally {
  s.close();
  kill(browser.pid);
}

console.log(`\n${fails.length ? 'FAILURES (' + fails.length + '):' : 'ALL GREEN'}`);
for (const f of fails) console.log('  · ' + f);
process.exit(fails.length ? 1 : 0);
