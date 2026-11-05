/* THE WHEEL RENDERER — a tilted, extruded, image-bearing prize wheel on one 2D canvas, no libraries.
 *
 * ── WHY CANVAS 2D AND NOT WEBGL ──────────────────────────────────────────────────────────────────────
 * A tilted disc is one linear transform, so a 3D engine buys nothing here and costs a vendored megabyte
 * that has to be served from the app (there is no CDN by design — a blocked CDN host HANGS rather than
 * failing, and the page then paints nothing). The 3D reading comes from four cheap things: a perspective
 * tilt, a visible extruded rim whose colours move with the wheel, directional shading, and a contact
 * shadow. To measure frame timing: node tools/assert_panel_agrees.mjs (writes evidence/frame-timing.json).
 *
 * ── HOW IT STAYS SMOOTH: ONE drawImage PER FRAME ─────────────────────────────────────────────────────
 * The wheel FACE is rendered once into an offscreen canvas in flat wheel space (12 o'clock = wheel angle
 * 0) whenever the prize list or the size changes — segments, medallions, names and all. Per frame the only
 * work is:
 *     translate(cx,cy) -> scale(1, cos(tilt)) -> rotate(theta) -> drawImage(face)
 * because screen = translate ∘ scaleY ∘ rotate ∘ wheelspace decomposes exactly. Everything else per frame
 * (rim slices, gloss, hub, pointer, highlight) is a handful of paths. Nothing touches layout: no `left`,
 * no `top`, no `width` — transforms and alpha only, which is what keeps a spin off the layout path.
 *
 * ── THE ENLARGED PANEL IS DERIVED FROM THE ROTATION ──────────────────────────────────────────────────
 * `segmentAt(theta)` below is a direct mirror of `segment_at()` in app/lib/model.py, over the segment
 * geometry THE SERVER COMPUTED. The panel reads that, every frame, from the same `theta` the renderer just
 * drew with. It is not driven by a timer, not by a precomputed schedule, and above all not by the known
 * winner — so the panel showing one prize while the result announces another is not a state this can
 * reach. `tools/assert_panel_agrees.mjs` checks it over many spins in a real browser anyway.
 */
'use strict';

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;
const FONT = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

/* Presentation constants. Each is a deliberate visual decision, not a magic number. */
const TILT_DEG = 20;            // how far the disc leans away from the viewer
const DEPTH_FRACTION = 0.085;   // rim thickness as a fraction of the wheel radius
const MEDALLION_R = 0.63;       // where a prize's picture sits, as a fraction of the radius
const SUPERSAMPLE = 1.18;       // the face is rendered this much larger than it is drawn, so rotating it
                                // resamples from more pixels than it needs and stays crisp

export function shade(hex, amount) {
  /* Lighten (amount > 0) or darken (amount < 0) a #rrggbb by a fraction, staying in gamut. */
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
  const n = m ? parseInt(m[1], 16) : 0x666666;
  const out = [16, 8, 0].map((sh) => {
    const c = (n >> sh) & 255;
    const v = amount >= 0 ? c + (255 - c) * amount : c * (1 + amount);
    return Math.max(0, Math.min(255, Math.round(v)));
  });
  return `rgb(${out[0]},${out[1]},${out[2]})`;
}

function initials(name) {
  const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

function upright(angle) {
  /* Keep a rotated label or picture within 90 degrees of upright, by flipping it half a turn when it
   * would otherwise be mirrored. This is what real prize wheels do: the labels on the far side read from
   * the outside in rather than upside down. Without it the whole bottom half of the wheel is inverted —
   * which is exactly how the first render of this came out. */
  const n = Math.atan2(Math.sin(angle), Math.cos(angle));   // normalise to (-PI, PI]
  return Math.abs(n) > Math.PI / 2 ? angle + Math.PI : angle;
}

function fitText(ctx, text, maxWidth) {
  /* Truncate to fit, with an ellipsis. Returns '' when even one character will not fit, because a single
   * clipped glyph reads as a rendering fault. */
  if (ctx.measureText(text).width <= maxWidth) return text;
  for (let i = text.length - 1; i > 0; i--) {
    const t = text.slice(0, i) + '…';
    if (ctx.measureText(t).width <= maxWidth) return t;
  }
  return '';
}

/* ================================================================== image loading */
export function loadImages(prizes) {
  /* Every prize picture, decoded up front. An image that arrives mid-spin is a frame spent on the network,
   * and a decode mid-spin is a frame spent on the CPU — so both happen before the wheel is drawable.
   * A failure resolves to null and the prize renders its monogram plate: a missing picture must look
   * deliberate, never broken. */
  const jobs = prizes.filter((p) => p.image).map((p) => new Promise((resolve) => {
    const img = new Image();
    img.decoding = 'sync';
    img.onload = () => resolve([p.image, img]);
    img.onerror = () => resolve([p.image, null]);
    img.src = `/api/image/${encodeURIComponent(p.image)}?v=${encodeURIComponent(p.image_md5 || '')}`;
  }));
  return Promise.all(jobs).then((pairs) => {
    const map = new Map();
    for (const [k, v] of pairs) if (v) map.set(k, v);
    return map;
  });
}

/* ================================================================== the wheel */
export class Wheel {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: true });
    this.segments = [];
    this.prizesById = new Map();
    this.imagesByName = new Map();
    this.theta = 0;              // degrees clockwise; the single source of truth for what is where
    this.omega = 0;              // degrees per second, from the last frame — drives the panel's blur
    this.tilt = (opts.tiltDeg ?? TILT_DEG) * DEG;
    this.face = null;            // offscreen canvas, flat wheel space
    this.faceR = 0;
    this.dpr = 1;
    this.flap = 0;               // pointer flick, 0..1, decays each frame
    this.lastSegIndex = -1;
    this.onBoundary = null;      // (segIndex, omega) => void
    this.reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  setData(segments, prizes, images) {
    this.segments = segments || [];
    this.prizesById = new Map((prizes || []).map((p) => [p.id, p]));
    this.imagesByName = images || new Map();
    this.face = null;            // force a re-render of the face
  }

  /* ---------------------------------------------------------------- layout */
  resize() {
    const rect = this.canvas.getBoundingClientRect();
    /* Cap the device-pixel ratio: a 3x display would otherwise quadruple the fill cost for a difference
     * nobody can see at stage distance, and fill rate is exactly what a 60fps budget is spent on. */
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(320, Math.floor(rect.width));
    const h = Math.max(320, Math.floor(rect.height));
    if (this.canvas.width !== w * this.dpr || this.canvas.height !== h * this.dpr) {
      this.canvas.width = w * this.dpr;
      this.canvas.height = h * this.dpr;
      this.face = null;
    }
    this.cssW = w;
    this.cssH = h;
    /* The disc is squashed vertically by cos(tilt) and then needs room for the rim and the shadow. */
    const k = Math.cos(this.tilt);
    this.R = Math.min(w / 2, (h - 24) / (2 * k + 2 * DEPTH_FRACTION + 0.18)) * 0.97;
    this.cx = w / 2;
    this.cy = h / 2 - this.R * DEPTH_FRACTION * 0.5;
    return this;
  }

  /* ---------------------------------------------------------------- the pointer readout (mirrors model.py) */
  pointerWheelAngle(theta = this.theta) {
    return ((-theta) % 360 + 360) % 360;
  }

  segmentAt(theta = this.theta) {
    /* Mirror of model.segment_at(). Over the SERVER's segment list, so there is one geometry and this is
     * only a lookup in it. */
    const segs = this.segments;
    if (!segs.length) return -1;
    const a = this.pointerWheelAngle(theta);
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      if (a >= s.start && a < s.start + s.size) return s.index;
    }
    return segs[segs.length - 1].index;
  }

  thetaForSegment(index, turns) {
    /* Mirror of model.theta_for_segment(): land segment `index`'s centre under the 12 o'clock pointer. */
    const seg = this.segments[index];
    if (!seg) return this.theta;
    const targetMod = ((-seg.centre) % 360 + 360) % 360;
    const delta = ((targetMod - this.theta) % 360 + 360) % 360;
    return this.theta + 360 * turns + delta;
  }

  /* ---------------------------------------------------------------- the offscreen face */
  buildFace() {
    const R = Math.round(this.R * this.dpr * SUPERSAMPLE);
    const size = R * 2;
    const c = document.createElement('canvas');
    c.width = size;
    c.height = size;
    const x = c.getContext('2d');
    x.translate(R, R);
    this.faceR = R;

    const segs = this.segments;
    if (!segs.length) {
      /* An empty wheel is a designed state, not a blank canvas. */
      x.beginPath();
      x.arc(0, 0, R * 0.98, 0, TAU);
      x.fillStyle = 'rgba(255,255,255,0.045)';
      x.fill();
      x.strokeStyle = 'rgba(255,255,255,0.18)';
      x.lineWidth = Math.max(2, R * 0.006);
      x.stroke();
      this.face = c;
      return;
    }

    for (const s of segs) {
      const a0 = (s.start - 90) * DEG;
      const a1 = (s.start + s.size - 90) * DEG;
      const mid = (s.start + s.size / 2 - 90) * DEG;

      /* The wedge. Alternating brightness by slot parity is load-bearing, not decoration: when a prize is
       * split, two of its segments can end up adjacent (unavoidably, if it holds more than half the
       * wheel), and without this they merge into one block — exactly what splitting is meant to prevent. */
      x.beginPath();
      x.moveTo(0, 0);
      x.arc(0, 0, R * 0.985, a0, a1);
      x.closePath();
      const tint = s.shade ? 0.10 : -0.04;
      const g = x.createRadialGradient(0, 0, R * 0.08, 0, 0, R);
      g.addColorStop(0, shade(s.colour, tint + 0.16));
      g.addColorStop(0.55, shade(s.colour, tint));
      g.addColorStop(1, shade(s.colour, tint - 0.16));
      x.fillStyle = g;
      x.fill();

      /* A divider on every boundary, so a boundary is always visible even between two same-coloured
       * segments of one prize. */
      x.save();
      x.beginPath();
      x.moveTo(0, 0);
      x.lineTo(Math.cos(a0) * R * 0.985, Math.sin(a0) * R * 0.985);
      x.strokeStyle = 'rgba(0,0,0,0.30)';
      x.lineWidth = Math.max(1.2, R * 0.0045);
      x.stroke();
      x.restore();

      /* The medallion: the prize picture clipped to a circle, with a ring and a soft shadow. A circle
       * normalises wildly different aspect ratios into one shape, which is what stops a wheel of mixed
       * photos looking like a collage. */
      const medR = Math.min(R * 0.155, R * MEDALLION_R * Math.sin(Math.max(2, s.size) * DEG / 2) * 0.92);
      if (medR > R * 0.028) {
        const mx = Math.cos(mid) * R * MEDALLION_R;
        const my = Math.sin(mid) * R * MEDALLION_R;
        const prize = this.prizesById.get(s.prize_id) || {};
        const img = s.image ? this.imagesByName.get(s.image) : null;

        x.save();
        x.translate(mx, my);
        x.rotate(upright(mid + Math.PI / 2));   // never upside down — see `upright`
        x.beginPath();
        x.arc(0, 0, medR, 0, TAU);
        x.save();
        x.shadowColor = 'rgba(0,0,0,0.45)';
        x.shadowBlur = medR * 0.5;
        x.shadowOffsetY = medR * 0.12;
        x.fillStyle = shade(s.colour, -0.55);
        x.fill();
        x.restore();

        x.save();
        x.clip();
        if (img) {
          /* Cover the circle, centred, preserving aspect ratio. */
          const scale = Math.max((medR * 2) / img.naturalWidth, (medR * 2) / img.naturalHeight);
          const dw = img.naturalWidth * scale;
          const dh = img.naturalHeight * scale;
          x.drawImage(img, -dw / 2, -dh / 2, dw, dh);
        } else {
          /* NO PICTURE YET — a deliberate monogram plate. */
          const pg = x.createLinearGradient(0, -medR, 0, medR);
          pg.addColorStop(0, shade(s.colour, 0.34));
          pg.addColorStop(1, shade(s.colour, -0.28));
          x.fillStyle = pg;
          x.fillRect(-medR, -medR, medR * 2, medR * 2);
          x.fillStyle = 'rgba(255,255,255,0.92)';
          x.font = `700 ${Math.round(medR * 0.95)}px ${FONT}`;
          x.textAlign = 'center';
          x.textBaseline = 'middle';
          x.fillText(initials(prize.name || s.name), 0, medR * 0.04);
        }
        x.restore();

        x.beginPath();
        x.arc(0, 0, medR, 0, TAU);
        x.strokeStyle = 'rgba(255,255,255,0.55)';
        x.lineWidth = Math.max(1.5, medR * 0.055);
        x.stroke();
        x.restore();
      }

      /* The name, set along the arc between the medallion and the rim.
       * ⛔ THE WIDTH BUDGET IS THE REAL CHORD AT THE TEXT'S OWN RADIUS, not a multiple of it. The first
       *    version allowed 1.7x the chord, and an 18 degree "GRAND PRIZE" wedge duly painted its label
       *    straight across its neighbour — which reads as a rendering fault rather than a long name. */
      const textR = R * 0.875;
      const chord = 2 * textR * Math.sin(s.size * DEG / 2) * 0.92;   // 8% breathing room at the edges
      const fontPx = Math.max(R * 0.030, Math.min(R * 0.058, chord * 0.42));
      if (s.size > 4 && fontPx > R * 0.029) {
        x.save();
        x.translate(Math.cos(mid) * textR, Math.sin(mid) * textR);
        x.rotate(upright(mid + Math.PI / 2));
        x.font = `700 ${Math.round(fontPx)}px ${FONT}`;
        x.textAlign = 'center';
        x.textBaseline = 'middle';
        const full = String(s.name || '').toUpperCase();
        let label = fitText(x, full, chord);
        /* A label cut to almost nothing is worse than no label: the medallion and the enlarged panel both
         * name the prize, so an honest blank beats "G…". */
        if (label.length <= 2 && full.length > 2) label = '';
        if (label) {
          x.lineWidth = Math.max(2, fontPx * 0.18);
          x.strokeStyle = 'rgba(0,0,0,0.42)';
          x.lineJoin = 'round';
          x.strokeText(label, 0, 0);
          x.fillStyle = '#fff';
          x.fillText(label, 0, 0);
        }
        x.restore();
      }
    }

    /* Inner and outer bands, drawn last so they sit over every wedge. */
    x.beginPath();
    x.arc(0, 0, R * 0.985, 0, TAU);
    x.strokeStyle = 'rgba(255,255,255,0.30)';
    x.lineWidth = Math.max(2, R * 0.010);
    x.stroke();

    this.face = c;
  }

  /* ---------------------------------------------------------------- per-frame draw */
  draw(dtMs) {
    const ctx = this.ctx;
    if (!this.face) this.buildFace();

    /* Angular speed, from the actual rotation change — this is what the panel's blur and the pointer flap
     * are driven by, so both describe the wheel rather than the animation's intent. */
    if (dtMs > 0 && this._lastTheta !== undefined) {
      const inst = Math.abs(this.theta - this._lastTheta) / (dtMs / 1000);
      this.omega = this.omega * 0.7 + inst * 0.3;      // light smoothing, or it jitters with frame noise
    }
    this._lastTheta = this.theta;

    const k = Math.cos(this.tilt);
    const R = this.R;
    const depth = R * DEPTH_FRACTION;
    const dpr = this.dpr;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.cssW, this.cssH);

    /* Contact shadow on the floor. */
    ctx.save();
    ctx.translate(this.cx, this.cy + R * k + depth * 0.9);
    ctx.scale(1, 0.18);
    const sg = ctx.createRadialGradient(0, 0, 0, 0, 0, R);
    sg.addColorStop(0, 'rgba(0,0,0,0.45)');
    sg.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = sg;
    ctx.beginPath();
    ctx.arc(0, 0, R, 0, TAU);
    ctx.fill();
    ctx.restore();

    /* ── the extruded rim ────────────────────────────────────────────────────────────────────────
     * Drawn as the band between the face ellipse and the same ellipse pushed down by `depth`, then
     * overpainted per segment on the FRONT arc so the rim's colours travel with the wheel. That moving
     * colour is most of what makes the disc read as a solid object rather than a picture of one. */
    ctx.save();
    ctx.translate(this.cx, this.cy);
    ctx.beginPath();
    ctx.ellipse(0, depth, R, R * k, 0, 0, Math.PI);
    ctx.lineTo(R, 0);
    ctx.ellipse(0, 0, R, R * k, 0, 0, Math.PI, true);
    ctx.closePath();
    const rg = ctx.createLinearGradient(0, 0, 0, depth);
    rg.addColorStop(0, 'rgba(10,12,18,0.55)');
    rg.addColorStop(1, 'rgba(4,5,8,0.95)');
    ctx.fillStyle = rg;
    ctx.fill();

    if (this.segments.length) {
      ctx.save();
      ctx.beginPath();                           // confine the slices to the rim band
      ctx.ellipse(0, depth, R, R * k, 0, 0, Math.PI);
      ctx.lineTo(R, 0);
      ctx.ellipse(0, 0, R, R * k, 0, 0, Math.PI, true);
      ctx.closePath();
      ctx.clip();
      for (const s of this.segments) {
        /* Screen angle of this segment, clockwise from 12 o'clock; the front of the disc is 90..270. */
        let a0 = (s.start + this.theta) % 360;
        let a1 = a0 + s.size;
        const lo = Math.max(a0, 90);
        const hi = Math.min(a1, 270);
        const segs2 = [];
        if (hi > lo) segs2.push([lo, hi]);
        /* The same segment can also be visible one turn over, so check the wrapped copy too. */
        const lo2 = Math.max(a0 - 360, 90);
        const hi2 = Math.min(a1 - 360, 270);
        if (hi2 > lo2) segs2.push([lo2, hi2]);
        const lo3 = Math.max(a0 + 360, 90);
        const hi3 = Math.min(a1 + 360, 270);
        if (hi3 > lo3) segs2.push([lo3, hi3]);
        for (const [b0, b1] of segs2) {
          const c0 = (b0 - 90) * DEG;
          const c1 = (b1 - 90) * DEG;
          ctx.beginPath();
          ctx.ellipse(0, 0, R, R * k, 0, c0, c1);
          ctx.ellipse(0, depth, R, R * k, 0, c1, c0, true);
          ctx.closePath();
          const cg = ctx.createLinearGradient(0, 0, 0, depth);
          cg.addColorStop(0, shade(s.colour, -0.30));
          cg.addColorStop(1, shade(s.colour, -0.68));
          ctx.fillStyle = cg;
          ctx.fill();
        }
      }
      ctx.restore();
    }
    ctx.restore();

    /* ── the face: ONE rotated drawImage ─────────────────────────────────────────────────────────── */
    ctx.save();
    ctx.translate(this.cx, this.cy);
    ctx.scale(1, k);
    ctx.rotate(this.theta * DEG);
    const drawR = R;
    ctx.drawImage(this.face, -drawR, -drawR, drawR * 2, drawR * 2);
    ctx.restore();

    /* ── the current segment's highlight, in the same tilted space ────────────────────────────────
     * This is the relationship the spec is about, made visible: the wedge under the pointer is lit,
     * and it is chosen by segmentAt(theta) — the same function the enlarged panel reads. If the highlight
     * and the panel ever disagreed, that would be visible on screen instantly. */
    const cur = this.segmentAt();
    if (cur >= 0 && this.segments.length) {
      const s = this.segments[cur];
      ctx.save();
      ctx.translate(this.cx, this.cy);
      ctx.scale(1, k);
      ctx.rotate(this.theta * DEG);
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, R * 0.985, (s.start - 90) * DEG, (s.start + s.size - 90) * DEG);
      ctx.closePath();
      ctx.fillStyle = 'rgba(255,255,255,0.13)';
      ctx.fill();
      ctx.lineWidth = Math.max(2, R * 0.009);
      ctx.strokeStyle = 'rgba(255,255,255,0.85)';
      ctx.stroke();
      ctx.restore();
    }

    /* ── gloss and vignette, in screen space so the light stays put while the wheel turns ─────────── */
    ctx.save();
    ctx.translate(this.cx, this.cy);
    ctx.scale(1, k);
    ctx.beginPath();
    ctx.arc(0, 0, R * 0.985, 0, TAU);
    const gl = ctx.createLinearGradient(-R * 0.6, -R, R * 0.4, R);
    gl.addColorStop(0, 'rgba(255,255,255,0.20)');
    gl.addColorStop(0.42, 'rgba(255,255,255,0.03)');
    gl.addColorStop(0.62, 'rgba(0,0,0,0.05)');
    gl.addColorStop(1, 'rgba(0,0,0,0.30)');
    ctx.fillStyle = gl;
    ctx.fill();
    ctx.restore();

    /* ── hub ──────────────────────────────────────────────────────────────────────────────────────── */
    ctx.save();
    ctx.translate(this.cx, this.cy);
    ctx.scale(1, k);
    const hubR = R * 0.115;
    ctx.beginPath();
    ctx.arc(0, 0, hubR, 0, TAU);
    const hg = ctx.createRadialGradient(-hubR * 0.3, -hubR * 0.4, hubR * 0.1, 0, 0, hubR);
    hg.addColorStop(0, '#f6f7fb');
    hg.addColorStop(0.55, '#c9ced9');
    hg.addColorStop(1, '#6d7486');
    ctx.fillStyle = hg;
    ctx.fill();
    ctx.lineWidth = Math.max(1.5, R * 0.006);
    ctx.strokeStyle = 'rgba(0,0,0,0.35)';
    ctx.stroke();
    ctx.restore();

    /* ── the pointer, in screen space, with a flick as each boundary passes ────────────────────────── */
    if (dtMs > 0) this.flap = Math.max(0, this.flap - dtMs / 260);
    if (cur !== this.lastSegIndex) {
      if (this.lastSegIndex !== -1) {
        this.flap = Math.min(1, 0.35 + this.omega / 900);
        if (this.onBoundary) this.onBoundary(cur, this.omega);
      }
      this.lastSegIndex = cur;
    }
    const topY = this.cy - R * k;
    ctx.save();
    ctx.translate(this.cx, topY - R * 0.055);
    ctx.rotate(-this.flap * 13 * DEG);
    ctx.beginPath();
    ctx.moveTo(0, R * 0.105);
    ctx.lineTo(-R * 0.048, -R * 0.055);
    ctx.quadraticCurveTo(0, -R * 0.088, R * 0.048, -R * 0.055);
    ctx.closePath();
    const pg = ctx.createLinearGradient(0, -R * 0.08, 0, R * 0.1);
    pg.addColorStop(0, '#fffdf4');
    pg.addColorStop(0.5, '#ffd34d');
    pg.addColorStop(1, '#f08a1c');
    ctx.fillStyle = pg;
    ctx.shadowColor = 'rgba(255,190,70,0.75)';
    ctx.shadowBlur = R * 0.09;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.lineWidth = Math.max(1.2, R * 0.005);
    ctx.strokeStyle = 'rgba(80,40,0,0.5)';
    ctx.stroke();
    ctx.restore();
  }
}

/* ================================================================== the deceleration, SOLVED
 *
 * The spin is OPERATOR-STOPPED: the wheel accelerates to a cruising speed and holds there until somebody
 * presses STOP. Only then is the winner drawn, and only then does this run. So the deceleration cannot be a
 * fixed animation — it has to start from wherever the wheel IS and however fast it is GOING, and still land
 * exactly on the drawn segment's centre.
 *
 * ── THE THREE THINGS IT HAS TO GET RIGHT, AND HOW EACH IS GUARANTEED ─────────────────────────────────
 *
 * (a) IT LANDS EXACTLY. `delta` is chosen from the congruence class that puts the target centre under the
 *     pointer — `delta ≡ (-centre - theta0) (mod 360)` — so the landing is exact by construction rather
 *     than by a tolerance. The draw decided the winner; the animation must not make it approximate.
 *
 * (b) NO ABRUPT SPEED CHANGE AT THE PRESS. With `e(u) = 1 - (1-u)^n`, the offset's initial angular speed is
 *     `(delta + overshoot) * n / (0.9 * T)`. Rather than pick a duration and accept a velocity jump, this
 *     SOLVES FOR THE DURATION: `T = (delta + overshoot) * n / (0.9 * omega0)`. The first frame of the
 *     deceleration therefore continues at exactly the speed the wheel was already turning.
 *
 * (c) IT NEVER REVERSES AND ALWAYS TAKES AT LEAST `minTurns`. `delta` is positive and at least
 *     `minTurns * 360`, and `e` is monotonic over the main phase, so theta only ever increases there. The
 *     only backwards motion in the whole spin is the few degrees of settle in (d), which is deliberate.
 *
 * (d) IT SETTLES RATHER THAN STOPPING DEAD. The last tenth is a damped cosine that is EXACTLY zero at the
 *     end, because cos(1.5π) = 0 — so the settle cannot leave the wheel a fraction of a degree off target.
 *     The overshoot is capped at a QUARTER of the target segment's own width, so the wheel can never
 *     overshoot far enough to show the next prize in the enlarged panel before coming back.
 *
 * Duration is a consequence of (b), so `targetSeconds` steers it only by choosing WHICH legal `delta` to
 * use: the one closest to what would take about that long.
 */
export function solveSpindown({ theta0, omega0, targetCentre, minTurns = 2, exponent = 3.4,
                                targetSeconds = 5.0, segmentSize = 12, overshootCap = 6,
                                maxSeconds = 12 }) {
  const n = Math.max(1.6, exponent);
  const omega = Math.max(1, omega0);                     // a stopped wheel would divide by zero
  const overshoot = Math.max(0.6, Math.min(overshootCap, segmentSize * 0.25));

  /* Every legal landing rotation is `base + 360k`. Pick the k that is closest to the rotation which would
   * take about `targetSeconds`, but never fewer than `minTurns` full turns. */
  const base = (((-targetCentre - theta0) % 360) + 360) % 360;
  const idealDelta = Math.max(0, (0.9 * omega * targetSeconds) / n - overshoot);
  const kMin = Math.ceil((minTurns * 360 - base) / 360);
  const kIdeal = Math.round((idealDelta - base) / 360);
  const k = Math.max(kMin, kIdeal);
  const delta = base + 360 * k;

  let duration = ((delta + overshoot) * n) / (0.9 * omega);

  /* ⚠️ THE LAST-RESORT CLAMP, and it is worth saying what it costs. Duration is DERIVED from the entry
   * speed, so a press at a crawl (say 30 deg/s) would take nearly two minutes to complete its two
   * mandatory turns. Capping the duration means the wheel speeds up at the press instead — a visible jolt,
   * which is precisely what (b) above exists to avoid. It is the lesser of two bad outcomes and it should
   * never fire: the page does not ARM stop until the wheel is at cruise, and a press during spin-up is
   * QUEUED until it is. `clamped` is returned so a test can assert that it stays false in the real flow. */
  const clamped = duration > maxSeconds;
  if (clamped) duration = maxSeconds;

  return { delta, overshoot, duration, exponent: n, turns: delta / 360, entrySpeed: omega, clamped };
}

/* The offset from theta0 at normalised time t (0..1) for a solved plan. */
export function spindownOffset(t, plan) {
  if (t >= 1) return plan.delta;
  if (t <= 0) return 0;
  if (t < 0.9) {
    const u = t / 0.9;
    return (plan.delta + plan.overshoot) * (1 - Math.pow(1 - u, plan.exponent));
  }
  const s = (t - 0.9) / 0.1;
  return plan.delta + plan.overshoot * Math.cos(s * 1.5 * Math.PI) * Math.exp(-4 * s);
}

/* The spin-up: ease into the cruising speed, then hold. Returns the angular speed at `elapsed` seconds. */
export function spinupSpeed(elapsed, cruise, spinupSeconds) {
  if (elapsed >= spinupSeconds) return cruise;
  const u = Math.max(0, elapsed / spinupSeconds);
  /* smootherstep: zero acceleration at both ends, so the start is not a jolt and reaching cruise is not a
   * corner. A linear ramp shows a visible kink at the top. */
  return cruise * (u * u * u * (u * (u * 6 - 15) + 10));
}
