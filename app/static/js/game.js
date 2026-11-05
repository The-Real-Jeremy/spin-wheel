/* THE GAME PAGE — the wheel, the enlarged prize panel, and the operator-stopped spin.
 *
 * ── THE INTERACTION MODEL (owner, 2026-09-23) ───────────────────────────────────────────────────────
 * "contestants will just come up to our laptop to press the stop button so the wheel stops accelerating
 * and lands on their prize". So this is NOT a fixed-duration spin:
 *
 *     idle --SPIN--> spinning up --(1.15s)--> cruising (STOP armed) --STOP--> stopping --> settled
 *
 * ⭐ THE WINNER IS DRAWN AT THE MOMENT STOP IS PRESSED, not when the wheel starts. That makes the
 *    contestant's press the thing that produces the outcome, and it means no winner exists anywhere the
 *    client could inspect while the wheel is still turning — which matters when people are standing at the
 *    laptop. The distribution is identical either way (see app/lib/model.py).
 *
 * ⭐ THE WHEEL CRUISES, IT DOES NOT KEEP ACCELERATING. Taken literally, "stops accelerating" would mean an
 *    ever-faster wheel, which becomes a strobing blur that reads as broken. It eases up to a cruising speed
 *    and holds there: visibly alive and fast, with the panel switching pictures the whole time.
 *
 * ⭐ STOP IS IDEMPOTENT THREE TIMES OVER, because it is a real button pressed by an excited stranger:
 *    the state machine refuses a second press, the draw request carries a token the server answers
 *    identically on a replay, and a `keydown` repeat is ignored. A double-press cannot draw twice, jump the
 *    wheel, or change the result.
 *
 * ⭐ AND A PRESS BEFORE THE WHEEL IS UP TO SPEED IS QUEUED, NOT IGNORED. The deceleration's duration is
 *    derived from the speed at the press (see solveSpindown), so stopping at a crawl would take a minute.
 *    Rather than silently dropping the press — which at an event looks like a broken button — it is
 *    remembered and honoured the instant the wheel reaches cruise.
 *
 * ⭐ THE ENLARGED PANEL IS A FUNCTION OF THE WHEEL'S ROTATION AND NOTHING ELSE.
 *    Every frame: `idx = wheel.segmentAt(wheel.theta)` — the same theta the renderer just drew with, looked
 *    up in the same server-computed geometry. There is no timer driving it, no precomputed schedule of
 *    which prize to show when, and it never reads the drawn winner. So "the panel says one prize while the
 *    result announces another" is not a reachable state, and the assertion in tools/assert_panel_agrees.mjs
 *    checks it in a real browser over many spins regardless.
 *
 * ⭐ WHAT HAPPENS AT SPEED, decided deliberately rather than left to chance.
 *    At high angular speed segment boundaries pass every few milliseconds, and a cross-fade per boundary
 *    would strobe. So the panel has two regimes, chosen from the MEASURED angular speed:
 *      · above FAST_DEG_S — switch instantly, with a motion blur that scales with speed. Reads as a reel.
 *      · below it         — cross-fade, with a floor so no fade is ever shorter than the eye can follow,
 *                           and a ceiling of the segment's own dwell time so a fade never outlives the
 *                           segment that started it.
 */
'use strict';

import { Wheel, loadImages, shade, solveSpindown, spindownOffset, spinupSpeed } from './wheel.js';

const FAST_DEG_S = 300;        // above this, switch instantly and blur instead of cross-fading
const FADE_FLOOR_MS = 90;
const FADE_CEIL_MS = 260;

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* ================================================================== sound (synthesised, no assets)
 * Default MUTED. An event host turns it on deliberately; nobody wants a wheel that starts making noise in
 * a quiet room. The preference is remembered per browser, and every storage access is wrapped because it
 * throws in a private window rather than returning null. */
class Sound {
  constructor() {
    this.on = false;
    try { this.on = localStorage.getItem('sw_sound') === '1'; } catch (e) { /* storage blocked */ }
    this.ctx = null;
  }

  _ensure() {
    if (!this.on) return null;
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      this.ctx = new AC();
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  }

  toggle() {
    this.on = !this.on;
    try { localStorage.setItem('sw_sound', this.on ? '1' : '0'); } catch (e) { /* ignore */ }
    if (this.on) this._ensure();
    return this.on;
  }

  tick(strength) {
    const c = this._ensure();
    if (!c) return;
    const t = c.currentTime;
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = 'square';
    o.frequency.setValueAtTime(880 + 420 * Math.min(1, strength), t);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.05 + 0.05 * Math.min(1, strength), t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.055);
    o.connect(g).connect(c.destination);
    o.start(t);
    o.stop(t + 0.07);
  }

  fanfare() {
    const c = this._ensure();
    if (!c) return;
    const t0 = c.currentTime;
    [0, 4, 7, 12, 16].forEach((semi, i) => {
      const o = c.createOscillator();
      const g = c.createGain();
      o.type = 'triangle';
      o.frequency.value = 440 * Math.pow(2, semi / 12);
      const t = t0 + i * 0.085;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.16, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.5);
      o.connect(g).connect(c.destination);
      o.start(t);
      o.stop(t + 0.55);
    });
  }
}

/* ================================================================== the enlarged prize panel */
class Panel {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.current = -1;         // segment index currently shown
    this.from = -1;            // segment index being faded out of
    this.fadeStart = 0;
    this.fadeMs = 0;
    this.segments = [];
    this.images = new Map();
    this.prizesById = new Map();
  }

  setData(segments, prizes, images) {
    this.segments = segments || [];
    this.prizesById = new Map((prizes || []).map((p) => [p.id, p]));
    this.images = images || new Map();
    this.current = -1;
    this.from = -1;
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const s = Math.max(200, Math.floor(Math.min(rect.width, rect.height)));
    this.canvas.width = s * dpr;
    this.canvas.height = s * dpr;
    this.size = s;
    this.dpr = dpr;
  }

  /* `omega` is the wheel's measured angular speed; it decides the regime. */
  show(index, omega, now) {
    if (index === this.current) return;
    const seg = this.segments[index];
    if (omega > FAST_DEG_S) {
      /* Instant switch: a cross-fade here would be a strobe. */
      this.current = index;
      this.from = -1;
      this.fadeMs = 0;
      return;
    }
    const dwell = seg && omega > 1 ? (seg.size / omega) * 1000 : FADE_CEIL_MS;
    const wanted = Math.max(FADE_FLOOR_MS, Math.min(FADE_CEIL_MS, omega > 1 ? 30000 / omega : FADE_CEIL_MS));
    this.from = this.current;
    this.current = index;
    this.fadeStart = now;
    /* The fade never outlives the segment that started it: a fade still running when the next boundary
     * arrives is what makes fast switching look like mush rather than like a reel. */
    this.fadeMs = Math.min(wanted, Math.max(40, dwell * 0.7));
  }

  draw(now, omega) {
    const ctx = this.ctx;
    const s = this.size;
    const dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, s, s);
    if (this.current < 0) return;

    let alpha = 1;
    if (this.from >= 0 && this.fadeMs > 0) {
      alpha = Math.min(1, (now - this.fadeStart) / this.fadeMs);
      if (alpha >= 1) this.from = -1;
    }
    /* Motion blur scaled by speed. Under the fast regime this is the whole effect; it decays to 0 as the
     * wheel settles, so the final image is always sharp. */
    const blur = Math.min(11, Math.max(0, (omega - 60) / 90));
    ctx.filter = blur > 0.25 ? `blur(${blur.toFixed(2)}px)` : 'none';

    if (this.from >= 0 && alpha < 1) {
      ctx.globalAlpha = 1 - alpha;
      this._paint(this.from, s);
      ctx.globalAlpha = alpha;
      this._paint(this.current, s);
      ctx.globalAlpha = 1;
    } else {
      this._paint(this.current, s);
    }
    ctx.filter = 'none';
  }

  _paint(index, s) {
    const ctx = this.ctx;
    const seg = this.segments[index];
    if (!seg) return;
    const prize = this.prizesById.get(seg.prize_id) || {};
    const img = seg.image ? this.images.get(seg.image) : null;
    const cx = s / 2;
    const cy = s / 2;
    const r = s * 0.45;

    /* A coloured halo tying the panel to the segment it is showing. */
    ctx.save();
    const halo = ctx.createRadialGradient(cx, cy, r * 0.75, cx, cy, r * 1.12);
    halo.addColorStop(0, shade(seg.colour, -0.1).replace('rgb', 'rgba').replace(')', ',0.55)'));
    halo.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, s, s);
    ctx.restore();

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.55)';
    ctx.shadowBlur = r * 0.22;
    ctx.shadowOffsetY = r * 0.05;
    ctx.fillStyle = shade(seg.colour, -0.55);
    ctx.fill();
    ctx.restore();
    ctx.clip();
    if (img) {
      const scale = Math.max((r * 2) / img.naturalWidth, (r * 2) / img.naturalHeight);
      const dw = img.naturalWidth * scale;
      const dh = img.naturalHeight * scale;
      ctx.drawImage(img, cx - dw / 2, cy - dh / 2, dw, dh);
    } else {
      const g = ctx.createLinearGradient(0, cy - r, 0, cy + r);
      g.addColorStop(0, shade(seg.colour, 0.3));
      g.addColorStop(1, shade(seg.colour, -0.3));
      ctx.fillStyle = g;
      ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
      ctx.fillStyle = 'rgba(255,255,255,0.93)';
      ctx.font = `700 ${Math.round(r * 0.7)}px ui-sans-serif, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const name = String(prize.name || seg.name || '?').trim().split(/\s+/);
      const mono = name.length > 1 ? (name[0][0] + name[1][0]).toUpperCase()
                                   : name[0].slice(0, 2).toUpperCase();
      ctx.fillText(mono, cx, cy + r * 0.03);
    }
    ctx.restore();

    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.lineWidth = Math.max(2, r * 0.035);
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.stroke();
  }
}

/* ================================================================== confetti (its own canvas) */
class Confetti {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.bits = [];
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.floor(r.width * dpr);
    this.canvas.height = Math.floor(r.height * dpr);
    this.w = r.width;
    this.h = r.height;
    this.dpr = dpr;
  }

  burst(colours) {
    const n = 150;
    for (let i = 0; i < n; i++) {
      this.bits.push({
        x: this.w * (0.25 + Math.random() * 0.2),
        y: this.h * (0.35 + Math.random() * 0.2),
        vx: (Math.random() - 0.5) * 620,
        vy: -180 - Math.random() * 520,
        rot: Math.random() * Math.PI,
        vr: (Math.random() - 0.5) * 14,
        w: 6 + Math.random() * 7,
        h: 3 + Math.random() * 5,
        c: colours[i % colours.length],
        life: 1,
      });
    }
  }

  get active() { return this.bits.length > 0; }

  draw(dt) {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    const s = Math.min(dt, 34) / 1000;
    const keep = [];
    for (const b of this.bits) {
      b.vy += 1150 * s;
      b.vx *= 0.995;
      b.x += b.vx * s;
      b.y += b.vy * s;
      b.rot += b.vr * s;
      b.life -= s * 0.32;
      if (b.life > 0 && b.y < this.h + 40) keep.push(b);
      ctx.save();
      ctx.translate(b.x, b.y);
      ctx.rotate(b.rot);
      ctx.globalAlpha = Math.max(0, Math.min(1, b.life));
      ctx.fillStyle = b.c;
      ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
      ctx.restore();
    }
    ctx.globalAlpha = 1;
    this.bits = keep;
  }
}

/* ================================================================== the page */
class Game {
  constructor() {
    this.wheel = new Wheel($('#wheel'));
    this.panel = new Panel($('#panel'));
    this.confetti = new Confetti($('#confetti'));
    this.sound = new Sound();
    this.state = 'idle';
    this.plan = null;              // the solved deceleration; null while the draw is in flight
    this.drawn = null;
    this.queuedStop = false;       // a STOP pressed before the wheel was up to speed
    this.stopInFlight = false;
    this.armed = false;
    this.playable = false;         // false until a wheel with segments is loaded
    this.spinToken = null;
    this.data = null;
    this.dirty = true;
    /* Frame timing for the real page, read by tools/frame_timing.mjs. A claim that an animation is smooth
     * needs the worst frame, not the average — one 200 ms hitch is what an audience notices. */
    this.perf = null;
    window.__spinWheel = this;                 // the test harness drives the real page through this
  }

  async init() {
    window.addEventListener('resize', () => this.layout());
    $('#primary').addEventListener('click', () => this.press());
    $('#reset').addEventListener('click', () => this.reset());
    $('#sound').addEventListener('click', (e) => {
      const on = this.sound.toggle();
      e.currentTarget.setAttribute('aria-pressed', String(on));
      e.currentTarget.querySelector('.label').textContent = on ? 'Sound on' : 'Sound off';
      if (on) this.sound.tick(0.4);
    });
    $('#fullscreen').addEventListener('click', () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen().catch(() => {});
    });
    document.addEventListener('keydown', (e) => {
      /* guard 3 of 3: a HELD space bar fires keydown over and over. `e.repeat` drops every repeat, so
       * leaning on the key cannot press STOP twice. */
      if (e.repeat) return;
      if ((e.code === 'Space' || e.code === 'Enter') && e.target === document.body) {
        e.preventDefault();
        this.press();
      }
    });
    this.wheel.onBoundary = (idx, omega) => this.sound.tick(Math.min(1, omega / 900));

    await this.load();
    this.loop();
  }

  async load() {
    let payload;
    try {
      const r = await fetch('/api/wheel', { headers: { Accept: 'application/json' } });
      /* ⛔ STATUS BEFORE BODY. A non-2xx body is not data — reading one as configuration is how an auth
       *    wall's JSON becomes "nothing is configured". */
      if (!r.ok) throw new Error(`the wheel could not be loaded (HTTP ${r.status})`);
      payload = await r.json();
    } catch (err) {
      this.fatal(String(err.message || err));
      return;
    }
    this.data = payload;
    document.title = `${payload.config.event_name} — spin to win`;
    $('#event-name').textContent = payload.config.event_name;

    const images = await loadImages(payload.prizes);
    this.wheel.setData(payload.segments, payload.prizes, images);
    this.panel.setData(payload.segments, payload.prizes, images);
    this.layout();
    this.renderBanners();
    this.renderOdds();
    await this.loadHistory();

    this.playable = payload.segments.length > 0;
    this.setState('idle');
    this.dirty = true;
  }

  fatal(message) {
    const box = $('#banners');
    box.innerHTML = '';
    box.appendChild(this.banner('bad', 'The wheel could not start', message));
    this.playable = false;
    $('#primary').disabled = true;
  }

  banner(kind, title, body) {
    const n = el('div', `banner banner--${kind}`);
    n.appendChild(el('strong', null, title));
    if (body) n.appendChild(el('span', null, body));
    return n;
  }

  renderBanners() {
    const d = this.data;
    const box = $('#banners');
    box.innerHTML = '';

    /* ⭐ "COULD NOT READ" IS ITS OWN STATE and must never render as "nothing configured": one of those is
     *    a wheel nobody set up, the other is a wheel whose setup is sitting safely in the workspace while
     *    this container cannot see it. Showing placeholders for the second would put the wrong prizes on a
     *    stage with nothing on screen saying so. */
    if (d.source === 'unreadable') {
      box.appendChild(this.banner('bad', 'The prize list could not be read.',
        `${d.store.detail || ''} Nothing has been lost — but this page is NOT showing your prizes. `
        + 'Fix the access problem and reload; do not start the event on this screen.'));
    } else if (d.source === 'default') {
      box.appendChild(this.banner('warn', 'These are the built-in example prizes.',
        'Nothing has been configured yet. Open Settings to set your own prizes, odds and pictures.'));
    }
    if (!d.store.durable) {
      box.appendChild(this.banner('warn', 'This deployment cannot save anything permanently.',
        'Prizes and pictures are being kept in the container, which is emptied whenever the app '
        + 'restarts. Anything set up here can vanish before the event.'));
    }
    if (d.meta && d.meta.scaled) {
      box.appendChild(this.banner('warn',
        `The configured chances add up to ${d.meta.declared_total}%, not 100%.`,
        'The wheel has been drawn by scaling them to fit the circle, so the odds in force are NOT the '
        + 'numbers that were typed. Open Settings and fix the total.'));
    }
    const arr = d.meta && d.meta.arrangement;
    if (arr && arr.impossible_to_separate) {
      box.appendChild(this.banner('info', 'Some segments of one prize sit next to each other.',
        arr.summary));
    }
  }

  renderOdds() {
    const box = $('#odds');
    box.innerHTML = '';
    for (const p of this.data.prizes) {
      const row = el('li', 'odds__row');
      const sw = el('span', 'odds__chip');
      sw.style.background = p.colour;
      row.appendChild(sw);
      row.appendChild(el('span', 'odds__name', p.name));
      row.appendChild(el('span', 'odds__pct', `${p.chance}%`));
      const seg = el('span', 'odds__split', p.splits > 1 ? `${p.splits} segments` : '1 segment');
      row.appendChild(seg);
      box.appendChild(row);
    }
  }

  async loadHistory() {
    try {
      const r = await fetch('/api/history?limit=12');
      if (!r.ok) return;
      const j = await r.json();
      const box = $('#recent');
      box.innerHTML = '';
      if (!j.rows.length) {
        box.appendChild(el('li', 'recent__empty', 'No spins yet.'));
        return;
      }
      for (const row of j.rows) {
        const li = el('li', 'recent__item');
        const p = this.data.prizes.find((x) => x.id === row.prize_id);
        const chip = el('span', 'recent__chip');
        chip.style.background = p ? p.colour : '#888';
        li.appendChild(chip);
        li.appendChild(el('span', 'recent__name', row.prize_name));
        li.appendChild(el('time', 'recent__at', new Date(row.at).toLocaleTimeString()));
        box.appendChild(li);
      }
    } catch (e) { /* the strip is a nicety; its failure must not touch the wheel */ }
  }

  layout() {
    this.wheel.resize();
    this.panel.resize();
    this.confetti.resize();
    this.dirty = true;
  }

  /* ---------------------------------------------------------------- the spin state machine
   * states: idle | spinup | cruise | stopping | settled
   * `stopping` is entered the instant STOP is pressed, BEFORE the draw comes back — so the operator sees
   * the press register immediately, and a second press is refused by the state rather than by a timer. The
   * wheel keeps cruising during that request; `plan` being null is what "drawing" means.
   */
  setState(next) {
    this.state = next;
    const c = this.data ? this.data.config : {};
    const armed = next === 'cruise' || (next === 'spinup' && this.queuedStop);
    const pill = $('#state');
    const primary = $('#primary');
    const map = {
      idle: ['Idle', 'Spin', false, 'Press Spin, then press Stop when the contestant is ready.'],
      spinup: ['Spinning up', 'Stop', !this.armed, this.queuedStop
        ? 'Stop is queued — it will take effect the moment the wheel is up to speed.'
        : 'Getting up to speed…'],
      cruise: ['Spinning', 'Stop', false, 'Press Stop to land the wheel.'],
      stopping: ['Stopping', 'Stopping…', true, 'Slowing down…'],
      settled: ['Settled', 'Spin again', false, 'Press Spin (or the space bar) for the next contestant.'],
    };
    const [label, btn, disabled, hint] = map[next] || map.idle;
    pill.textContent = label;
    pill.dataset.state = next;
    primary.textContent = btn;
    primary.disabled = !!disabled || !this.playable;
    primary.dataset.action = (next === 'cruise' || next === 'spinup') ? 'stop' : 'spin';
    $('#spin-hint').textContent = this.playable ? hint : 'Add some prizes on the settings page first.';
    $('#reset').disabled = next === 'spinup' || next === 'cruise' || next === 'stopping';
  }

  press() {
    /* ONE entry point for the button, the space bar and the harness, so every route through the machine is
     * the same route. */
    if (!this.playable) return;
    if (this.state === 'idle' || this.state === 'settled') return this.start();
    if (this.state === 'cruise') return this.requestStop();
    if (this.state === 'spinup') {
      /* Queued rather than dropped — see the header. */
      this.queuedStop = true;
      this.setState('spinup');
      return;
    }
    /* stopping: deliberately nothing. A press while it is slowing down must not draw again. */
  }

  start() {
    if (this.state === 'spinup' || this.state === 'cruise' || this.state === 'stopping') return;
    this.spinToken = (crypto.randomUUID ? crypto.randomUUID()
      : String(Date.now()) + Math.random().toString(36).slice(2));
    this.queuedStop = false;
    this.armed = false;
    this.plan = null;
    this.drawn = null;
    this.spinStartedAt = performance.now();
    this.perf = { frames: [], spindownFrames: [], startedAt: this.spinStartedAt };
    $('#result').classList.remove('is-shown');
    this.setState('spinup');
    this.dirty = true;
  }

  async requestStop() {
    if (this.state !== 'cruise' || this.stopInFlight) return;      // guard 1 of 3: the state machine
    this.stopInFlight = true;
    this.setState('stopping');
    this.entrySpeed = this.wheel.omega;
    let drawn;
    try {
      const r = await fetch('/api/draw', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        /* guard 2 of 3: the same token always returns the same result, so a retry cannot draw twice. */
        body: JSON.stringify({ token: this.spinToken }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      drawn = j;
    } catch (err) {
      /* The wheel is still cruising, so go back to a state the operator can act on and say why. */
      this.stopInFlight = false;
      this.setState('cruise');
      $('#spin-hint').textContent = `Nothing was drawn (${err.message || err}) — press Stop again.`;
      return;
    }
    this.drawn = drawn;
    const seg = this.data.segments[drawn.segment_index];
    const c = this.data.config;
    /* Solve the deceleration from where the wheel IS and how fast it is going RIGHT NOW. */
    this.plan = solveSpindown({
      theta0: this.wheel.theta,
      omega0: this.wheel.omega,
      targetCentre: seg.centre,
      minTurns: this.wheel.reducedMotion ? 1 : c.spindown_min_turns,
      exponent: c.spindown_exponent,
      targetSeconds: this.wheel.reducedMotion ? 1.2 : 5.0,
      segmentSize: seg.size,
    });
    this.planTheta0 = this.wheel.theta;
    this.planStartedAt = performance.now();
    this.stopInFlight = false;
    if (this.plan.clamped) {
      /* Should be unreachable: stop only arms at cruise. Recorded rather than ignored. */
      console.warn('spindown duration was clamped — stop fired below cruise speed', this.plan);
    }
    this.dirty = true;
  }

  reset() {
    if (this.state === 'spinup' || this.state === 'cruise' || this.state === 'stopping') return;
    this.plan = null;
    this.drawn = null;
    $('#result').classList.remove('is-shown');
    this.setState('idle');
    this.dirty = true;
  }

  /* Called once per frame from the loop; advances whichever phase is live. */
  advance(now, dt) {
    const c = this.data ? this.data.config : null;
    if (!c) return;
    const secs = Math.min(dt, 50) / 1000;        // a long frame must not teleport the wheel

    if (this.state === 'spinup') {
      const elapsed = (now - this.spinStartedAt) / 1000;
      const speed = spinupSpeed(elapsed, c.cruise_deg_s, c.spinup_seconds);
      this.wheel.theta += speed * secs;
      this.dirty = true;
      if (elapsed >= c.spinup_seconds) {
        this.armed = true;
        this.setState('cruise');
        if (this.queuedStop) { this.queuedStop = false; this.requestStop(); }
      }
      return;
    }
    if (this.state === 'cruise') {
      this.wheel.theta += c.cruise_deg_s * secs;
      this.dirty = true;
      return;
    }
    if (this.state === 'stopping') {
      if (!this.plan) {
        /* The draw is in flight: keep cruising so the wheel does not hitch while we wait. */
        this.wheel.theta += c.cruise_deg_s * secs;
        this.dirty = true;
        return;
      }
      const t = Math.min(1, (now - this.planStartedAt) / (this.plan.duration * 1000));
      this.wheel.theta = this.planTheta0 + spindownOffset(t, this.plan);
      this.dirty = true;
      if (t >= 1) {
        /* EXACTLY on the target: the draw decided this, so the animation must not leave it approximate. */
        this.wheel.theta = this.planTheta0 + this.plan.delta;
        this.wheel.omega = 0;
        this.setState('settled');
        this.finishPerf();
        this.announce(this.drawn);
        this.loadHistory();
      }
    }
  }

  finishPerf() {
    const f = this.perf && this.perf.frames;
    if (!f || f.length < 5) return;
    const summarise = (arr) => {
      if (!arr.length) return null;
      const sorted = [...arr].sort((a, b) => a - b);
      const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
      const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
      return {
        frames: arr.length,
        mean_ms: +mean.toFixed(2),
        median_ms: +at(0.5).toFixed(2),
        p95_ms: +at(0.95).toFixed(2),
        p99_ms: +at(0.99).toFixed(2),
        worst_ms: +sorted[sorted.length - 1].toFixed(2),
        over_20ms: arr.filter((x) => x > 20).length,
        over_34ms: arr.filter((x) => x > 34).length,
        over_50ms: arr.filter((x) => x > 50).length,
        fps_mean: +(1000 / mean).toFixed(1),
      };
    };
    this.perf.summary = summarise(f);
    /* The deceleration is reported SEPARATELY, because it is the part the room is watching and the part
     * where a hitch would be unmistakable. An average over the whole spin would hide it. */
    this.perf.spindown = summarise(this.perf.spindownFrames);
    window.__spinPerf = { whole_spin: this.perf.summary, spindown: this.perf.spindown };
  }

  announce(drawn) {
    /* ⭐ THE ASSERTION, IN THE PRODUCT ITSELF. The panel is driven by the rotation; the result comes from
     *    the server's draw. At rest they must name the same prize. If they ever do not, the page SAYS SO
     *    rather than quietly announcing one thing while showing another — a visible disagreement is
     *    recoverable at an event, a silent one is not. */
    if (!drawn) return;
    const shown = this.wheel.segmentAt();
    const shownSeg = this.data.segments[shown];
    const box = $('#result');
    box.innerHTML = '';
    const agrees = shownSeg && shownSeg.prize_id === drawn.prize_id;
    if (!agrees) {
      box.appendChild(el('div', 'result__mismatch',
        `Display error: the wheel is showing “${shownSeg ? shownSeg.name : 'nothing'}” but the draw was `
        + `“${drawn.prize_name}”. The recorded result is “${drawn.prize_name}”. Please reload.`));
      box.classList.add('is-shown');
      console.error('panel/result disagreement', { shown: shownSeg, drawn });
      return;
    }
    box.appendChild(el('div', 'result__kicker', 'Winner'));
    box.appendChild(el('div', 'result__prize', drawn.prize_name));
    if (drawn.logged === false) {
      box.appendChild(el('div', 'result__note',
        'This result was NOT written to the spin log — the prize still stands.'));
    }
    box.classList.add('is-shown');
    if (!this.wheel.reducedMotion) {
      const seg = this.data.segments[drawn.segment_index];
      this.confetti.burst([seg.colour, shade(seg.colour, 0.35), '#ffd34d', '#ffffff']);
    }
    this.sound.fanfare();
  }

  /* ---------------------------------------------------------------- the frame loop */
  loop() {
    let last = performance.now();
    const frame = (now) => {
      const dt = now - last;
      last = now;
      const moving = this.state === 'spinup' || this.state === 'cruise' || this.state === 'stopping';
      if (this.perf && moving) {
        this.perf.frames.push(dt);
        if (this.state === 'stopping' && this.plan) this.perf.spindownFrames.push(dt);
      }
      this.advance(now, dt);

      if (this.dirty || this.confetti.active || this.wheel.flap > 0) {
        this.wheel.draw(dt);
        /* THE PANEL IS DERIVED HERE, from the theta that was just drawn. */
        const idx = this.wheel.segmentAt();
        if (idx >= 0) this.panel.show(idx, this.wheel.omega, now);
        this.panel.draw(now, this.wheel.omega);
        this.updateReadout(idx);
        if (this.confetti.active) this.confetti.draw(dt);
        this.dirty = moving || this.confetti.active || this.wheel.flap > 0;
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  updateReadout(idx) {
    const seg = this.data && this.data.segments[idx];
    if (!seg) return;
    if (this._readoutFor === seg.prize_id && this._readoutIdx === idx) return;
    this._readoutFor = seg.prize_id;
    this._readoutIdx = idx;
    const prize = this.data.prizes.find((p) => p.id === seg.prize_id) || {};
    $('#panel-name').textContent = seg.name;
    $('#panel-odds').textContent = `${prize.chance}% chance`
      + (prize.splits > 1 ? ` · segment ${seg.nth + 1} of ${prize.splits}` : '');
    /* A blur on the text at speed, matching the panel's image. Transform/filter only — no layout. */
    const blur = Math.min(6, Math.max(0, (this.wheel.omega - 80) / 140));
    const box = $('#panel-meta');
    box.style.filter = blur > 0.2 ? `blur(${blur.toFixed(2)}px)` : '';
    box.style.opacity = blur > 4 ? '0.75' : '1';
  }
}

new Game().init();
