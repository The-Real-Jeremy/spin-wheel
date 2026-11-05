/* THE SETTINGS PAGE — add and remove prizes, set each one's chance, its picture, and how many segments it
 * is split into.
 *
 * ⛔ THERE IS NO PASSWORD ON THIS PAGE, by the owner's instruction (2026-09-23): the app runs on the hosts'
 *    own laptops and contestants only press the stop button. The gate that used to be here was removed entirely rather than disabled, so there is no
 *    dormant auth path to come back as a bug.
 *
 * ⭐ THE PERCENTAGE RULE IS VISIBLE, NOT IMPLIED. The running total is shown as you type and the server
 *    REFUSES a save that does not come to 100%, naming the actual total. Nothing is ever normalised behind
 *    the host's back: "Balance to 100%" rewrites the numbers ON SCREEN first, so the odds they can see are
 *    always the odds that will be in force.
 *
 * ⭐ THE PREVIEW IS THE SERVER'S GEOMETRY, not a second implementation. Every edit POSTs the working prize
 *    list to /api/preview and draws what comes back, including the measured arrangement (each prize's real
 *    angular gaps against its ideal). So what the host sees while editing is what the wheel will be, and
 *    "as evenly spaced as possible" is something they can look at rather than take on trust.
 *
 * ⭐ PICTURES ARE RE-ENCODED IN THE BROWSER before upload: drawn onto a canvas, capped on the long edge and
 *    exported with toBlob. That strips EXIF (including GPS), drops any trailing payload and turns a 12 MB
 *    phone photo into ~100 KB. The server then admits or refuses on the MAGIC BYTES — see app/lib/images.py
 *    for exactly where the trust boundary is, because it is easy to overclaim this.
 */
'use strict';

import { Wheel, loadImages } from './wheel.js';

const MAX_EDGE = 900;          // the long edge we re-encode to; app/lib/images.py allows up to 1400
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const uid = () => 'p_' + Math.random().toString(36).slice(2, 10);

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  /* STATUS FIRST, ALWAYS. A non-2xx body is not data — reading one as content is how an auth wall becomes
   * "nothing is configured". Callers get the status and decide. */
  return { status: r.status, ok: r.ok, body };
}

/* ================================================================== picture re-encoding */
async function reencode(file) {
  if (!/^image\//.test(file.type)) throw new Error('That is not an image file.');
  if (file.size > 25 * 1024 * 1024) throw new Error('That file is over 25 MB — pick a smaller one.');
  const bitmap = await createImageBitmap(file).catch(() => {
    throw new Error('That image could not be opened. Try a PNG, JPEG or WebP.');
  });
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const w = Math.max(16, Math.round(bitmap.width * scale));
  const h = Math.max(16, Math.round(bitmap.height * scale));
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  const x = c.getContext('2d');
  x.imageSmoothingQuality = 'high';
  x.drawImage(bitmap, 0, 0, w, h);
  bitmap.close?.();
  /* WebP first (smaller at the same quality, and it keeps transparency); PNG if the browser will not. */
  let blob = await new Promise((r) => c.toBlob(r, 'image/webp', 0.92));
  if (!blob) blob = await new Promise((r) => c.toBlob(r, 'image/png'));
  if (!blob) throw new Error('This browser could not re-encode that image.');
  return { blob, w, h };
}

/* ================================================================== the page */
class Settings {
  constructor() {
    this.prizes = [];
    this.limits = null;
    this.previewWheel = new Wheel($('#preview'), { tiltDeg: 16 });
    this.previewTimer = null;
    this.dirty = false;
  }

  async init() {
    $('#add').addEventListener('click', () => this.addPrize());
    $('#save').addEventListener('click', () => this.save());
    $('#balance').addEventListener('click', () => this.balance());
    $('#selftest').addEventListener('click', () => this.selftest());
    $('#clear-log').addEventListener('click', () => this.clearLog());
    window.addEventListener('resize', () => this.drawPreview());
    window.addEventListener('beforeunload', (e) => {
      if (this.dirty) { e.preventDefault(); e.returnValue = ''; }
    });
    window.__spinSettings = this;                  // the test harness drives the real page through this
    await this.load();
  }

  async load() {
    /* ⛔ NO GATE. the owner removed the settings password on 2026-09-23 ("this will just be used by us on our
     *    laptops"), so this page is open and nothing here pretends otherwise. */
    const r = await api('/api/settings');
    if (!r.ok) {
      this.banner('bad', 'The settings could not be loaded.', `HTTP ${r.status}`);
      return;
    }
    this.adopt(r.body);
  }

  adopt(data) {
    this.data = data;
    this.limits = data.limits;
    this.prizes = data.prizes.map((p) => ({ ...p }));
    if (!this.prizes.length) this.prizes = [];
    this.renderBanners();
    this.renderStorage();
    this.render();
    this.loadLog();
  }

  banner(kind, title, body) {
    const n = el('div', `banner banner--${kind}`);
    n.appendChild(el('strong', null, title));
    if (body) n.appendChild(el('span', null, body));
    $('#banners').appendChild(n);
  }

  renderBanners() {
    const d = this.data;
    $('#banners').innerHTML = '';
    if (!d) return;
    if (!d.store.durable) {
      this.banner('bad', 'Nothing saved here will survive a restart.',
        `This container is storing settings at ${d.store.base} on its own disk, which is emptied whenever `
        + 'the app restarts. Set the app up so it can write to the workspace before you rely on it for an '
        + 'event.');
    }
    if (d.store.state === 'denied' || d.store.state === 'error') {
      this.banner('bad', 'The stored prize list could not be read.', d.store.detail || '');
    }
    if (d.source === 'default') {
      this.banner('info', 'These are the built-in example prizes.',
        'Nothing has been saved yet — edit them and press Save, or start again with your own.');
    }
    if (d.updated && d.updated.at) {
      this.banner('info', 'Last saved', `${d.updated.at} by ${d.updated.by || 'unknown'}`);
    }
  }

  renderStorage() {
    const d = this.data;
    const dl = $('#storage');
    dl.innerHTML = '';
    const rows = [
      ['Mode', d.store.durable ? 'Workspace (survives a restart)' : 'Container disk (EPHEMERAL)'],
      ['Location', d.store.base || '—'],
      ['State', d.store.state],
    ];
    if (d.store.detail) rows.push(['Detail', d.store.detail]);
    for (const [k, v] of rows) {
      dl.appendChild(el('dt', null, k));
      dl.appendChild(el('dd', null, String(v)));
    }
  }

  /* ---------------------------------------------------------------- the prize rows */
  render() {
    const list = $('#prizes');
    list.innerHTML = '';
    this.prizes.forEach((p, i) => list.appendChild(this.row(p, i)));
    this.renderTotal();
    this.schedulePreview();
  }

  row(p, i) {
    const li = el('li', 'prize');
    li.dataset.index = String(i);

    const thumb = el('button', 'prize__thumb');
    thumb.type = 'button';
    thumb.title = 'Choose a picture for this prize';
    if (p.image) {
      const img = el('img');
      img.src = `/api/image/${encodeURIComponent(p.image)}?v=${encodeURIComponent(p.image_md5 || '')}`;
      img.alt = '';
      thumb.appendChild(img);
    } else {
      /* NO PICTURE is a designed state, not an empty box. */
      thumb.classList.add('prize__thumb--empty');
      thumb.style.background = p.colour;
      thumb.appendChild(el('span', null, (p.name || '?').trim().slice(0, 2).toUpperCase() || '?'));
    }
    const file = el('input', 'visually-hidden');
    file.type = 'file';
    file.accept = 'image/png,image/jpeg,image/webp,image/gif';
    thumb.addEventListener('click', () => file.click());
    file.addEventListener('change', () => this.upload(i, file));
    li.appendChild(thumb);
    li.appendChild(file);

    const fields = el('div', 'prize__fields');

    const name = el('input', 'prize__name');
    name.type = 'text';
    name.value = p.name || '';
    name.placeholder = 'Prize name';
    name.maxLength = 48;
    name.setAttribute('aria-label', 'Prize name');
    name.addEventListener('input', () => { p.name = name.value; this.touch(); this.schedulePreview(); });
    fields.appendChild(name);

    const nums = el('div', 'prize__nums');

    const chanceWrap = el('label', 'mini');
    chanceWrap.appendChild(el('span', null, 'Chance'));
    const chance = el('input');
    chance.type = 'number';
    chance.min = '0';
    chance.max = '100';
    chance.step = '0.01';
    chance.value = String(p.chance);
    chance.addEventListener('input', () => {
      p.chance = Number(chance.value) || 0;
      this.touch();
      this.renderTotal();
      this.schedulePreview();
    });
    chanceWrap.appendChild(chance);
    chanceWrap.appendChild(el('em', null, '%'));
    nums.appendChild(chanceWrap);

    const splitWrap = el('label', 'mini');
    splitWrap.appendChild(el('span', null, 'Segments'));
    const splits = el('input');
    splits.type = 'number';
    splits.min = '1';
    splits.max = String((this.limits && this.limits.max_splits) || 12);
    splits.step = '1';
    splits.value = String(p.splits);
    splits.addEventListener('input', () => {
      p.splits = Math.max(1, Math.round(Number(splits.value) || 1));
      this.touch();
      this.schedulePreview();
    });
    splitWrap.appendChild(splits);
    const each = el('em', 'prize__each');
    each.textContent = p.splits > 1 ? `${(p.chance / p.splits).toFixed(2)}% each` : '';
    splitWrap.appendChild(each);
    nums.appendChild(splitWrap);

    const colourWrap = el('label', 'mini');
    colourWrap.appendChild(el('span', null, 'Colour'));
    const colour = el('input');
    colour.type = 'color';
    colour.value = /^#[0-9a-f]{6}$/i.test(p.colour) ? p.colour : '#ff3621';
    colour.addEventListener('input', () => {
      p.colour = colour.value;
      this.touch();
      if (!p.image) { thumb.style.background = p.colour; }
      this.schedulePreview();
    });
    colourWrap.appendChild(colour);
    nums.appendChild(colourWrap);

    fields.appendChild(nums);
    li.appendChild(fields);

    const tools = el('div', 'prize__tools');
    if (p.image) {
      const drop = el('button', 'btn btn--tiny', 'Remove picture');
      drop.type = 'button';
      drop.addEventListener('click', () => {
        delete p.image; delete p.image_mime; delete p.image_md5;
        delete p.image_w; delete p.image_h;
        this.touch();
        this.render();
      });
      tools.appendChild(drop);
    }
    const up = el('button', 'btn btn--tiny', '↑');
    up.type = 'button';
    up.title = 'Move up';
    up.disabled = i === 0;
    up.addEventListener('click', () => this.move(i, -1));
    const down = el('button', 'btn btn--tiny', '↓');
    down.type = 'button';
    down.title = 'Move down';
    down.disabled = i === this.prizes.length - 1;
    down.addEventListener('click', () => this.move(i, 1));
    const del = el('button', 'btn btn--tiny btn--danger', 'Remove');
    del.type = 'button';
    del.addEventListener('click', () => {
      if (!confirm(`Remove “${p.name || 'this prize'}” from the wheel?`)) return;
      this.prizes.splice(i, 1);
      this.touch();
      this.render();
    });
    tools.append(up, down, del);
    li.appendChild(tools);
    return li;
  }

  touch() { this.dirty = true; $('#save-status').textContent = 'Unsaved changes.'; }

  move(i, d) {
    const j = i + d;
    if (j < 0 || j >= this.prizes.length) return;
    [this.prizes[i], this.prizes[j]] = [this.prizes[j], this.prizes[i]];
    this.touch();
    this.render();
  }

  addPrize() {
    const palette = (this.limits && this.limits.palette) || ['#ff3621'];
    this.prizes.push({
      id: uid(),
      name: '',
      chance: 0,
      splits: 1,
      colour: palette[this.prizes.length % palette.length],
    });
    this.touch();
    this.render();
    const rows = document.querySelectorAll('.prize__name');
    if (rows.length) rows[rows.length - 1].focus();
  }

  renderTotal() {
    const total = this.prizes.reduce((a, p) => a + (Number(p.chance) || 0), 0);
    const rounded = Math.round(total * 100) / 100;
    const box = $('#total-box');
    $('#total-value').textContent = `${rounded}%`;
    const good = Math.abs(rounded - 100) <= 0.01;
    box.classList.toggle('total--ok', good);
    box.classList.toggle('total--bad', !good);
    $('#total-value').title = good ? 'These are the odds that will be in force.'
      : `${rounded > 100 ? 'Remove' : 'Add'} ${Math.round(Math.abs(rounded - 100) * 100) / 100}% to save.`;
    $('#balance').hidden = good;
  }

  balance() {
    /* Rewrites the numbers ON SCREEN so the host sees the odds before they are saved. The largest share
     * absorbs the rounding remainder, so the visible numbers add to exactly 100 rather than to 99.99. */
    const total = this.prizes.reduce((a, p) => a + (Number(p.chance) || 0), 0);
    if (!this.prizes.length) return;
    if (total <= 0) {
      const even = Math.floor((100 / this.prizes.length) * 100) / 100;
      this.prizes.forEach((p) => { p.chance = even; });
    } else {
      this.prizes.forEach((p) => { p.chance = Math.round((p.chance / total) * 10000) / 100; });
    }
    const after = this.prizes.reduce((a, p) => a + p.chance, 0);
    const biggest = this.prizes.reduce((b, p) => (p.chance > b.chance ? p : b), this.prizes[0]);
    biggest.chance = Math.round((biggest.chance + (100 - after)) * 100) / 100;
    this.touch();
    this.render();
  }

  /* ---------------------------------------------------------------- pictures */
  async upload(i, fileInput) {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!file) return;
    const p = this.prizes[i];
    $('#save-status').textContent = `Preparing “${file.name}”…`;
    let prepared;
    try {
      prepared = await reencode(file);
    } catch (err) {
      $('#save-status').textContent = `That picture was not used: ${err.message}`;
      return;
    }
    const r = await api('/api/settings/image', {
      method: 'POST',
      headers: { 'Content-Type': prepared.blob.type },
      body: prepared.blob,
    });
    if (!r.ok) {
      $('#save-status').textContent = `That picture was NOT stored: ${(r.body && r.body.error) || r.status}`;
      return;
    }
    p.image = r.body.image;
    p.image_mime = r.body.image_mime;
    p.image_md5 = r.body.image_md5;
    p.image_w = r.body.image_w;
    p.image_h = r.body.image_h;
    this.touch();
    $('#save-status').textContent = `Picture stored (${r.body.image_w}x${r.body.image_h}, `
      + `${Math.round(r.body.bytes / 1024)} KB, ${r.body.image_mime}). Press Save to attach it.`;
    this.render();
  }

  /* ---------------------------------------------------------------- the preview */
  schedulePreview() {
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => this.refreshPreview(), 220);
  }

  async refreshPreview() {
    const r = await api('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prizes: this.prizes }),
    });
    if (!r.ok) {
      $('#arrangement').textContent = `Preview unavailable (HTTP ${r.status}).`;
      return;
    }
    this.preview = r.body;
    const images = await loadImages(r.body.prizes);
    this.previewWheel.setData(r.body.segments, r.body.prizes, images);
    this.drawPreview();
    this.renderArrangement(r.body);
    this.renderProblems(r.body.errors, r.body.warnings);
  }

  drawPreview() {
    if (!this.preview) return;
    this.previewWheel.resize();
    this.previewWheel.draw(0);
  }

  renderArrangement(body) {
    const box = $('#arrangement');
    box.innerHTML = '';
    const a = body.meta && body.meta.arrangement;
    if (!a) {
      box.appendChild(el('p', 'arrangement__note', 'Add a prize to see the arrangement.'));
      return;
    }
    /* ⭐ THE ARRANGEMENT IS SHOWN AS MEASUREMENTS, not as a reassurance. Each prize's real gaps against its
     *    ideal 360/k, and — when a prize holds more than half the wheel — the fact that some of its
     *    segments MUST touch, why, and what to change. A constraint that cannot be met is reported. */
    const kind = a.impossible_to_separate ? 'warn'
      : (a.adjacent_same_prize_pairs > 0 ? 'bad' : 'ok');
    const head = el('p', `arrangement__summary arrangement__summary--${kind}`, a.summary);
    box.appendChild(head);

    const table = el('table', 'gaps');
    const thead = el('thead');
    const hr = el('tr');
    ['Prize', 'Segments', 'Each', 'Ideal gap', 'Actual gaps'].forEach((h) => hr.appendChild(el('th', null, h)));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const row of a.prizes) {
      const tr = el('tr');
      tr.appendChild(el('td', null, row.name || '—'));
      tr.appendChild(el('td', null, String(row.splits)));
      tr.appendChild(el('td', null, `${row.segment_share_pct}%`));
      if (row.splits >= 2) {
        tr.appendChild(el('td', null, `${row.ideal_gap}°`));
        const cell = el('td');
        cell.appendChild(el('span', null, row.gaps.map((g) => `${g}°`).join('  ')));
        const off = el('em', row.even ? 'ok' : 'off',
          ` ${row.even ? 'even' : `${Math.round(row.worst_relative_deviation * 100)}% off`}`);
        cell.appendChild(off);
        if (row.touching) cell.appendChild(el('em', 'off', ' · two of these touch'));
        tr.appendChild(cell);
      } else {
        tr.appendChild(el('td', 'dim', '—'));
        tr.appendChild(el('td', 'dim', 'one segment'));
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    box.appendChild(table);
  }

  renderProblems(errors, warnings) {
    const ul = $('#problems');
    ul.innerHTML = '';
    for (const e of errors || []) ul.appendChild(el('li', 'problem problem--bad', e));
    for (const w of warnings || []) ul.appendChild(el('li', 'problem problem--warn', w));
  }

  /* ---------------------------------------------------------------- saving */
  async save() {
    $('#save-status').textContent = 'Saving…';
    const r = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prizes: this.prizes }),
    });
    if (!r.ok) {
      /* A refusal is the percentage rule working. Show every reason, keep the edits. */
      this.renderProblems(r.body && r.body.errors, r.body && r.body.warnings);
      $('#save-status').textContent = 'NOT saved — see the problems below. Your edits are still here.';
      return;
    }
    this.dirty = false;
    this.prizes = r.body.prizes.map((p) => ({ ...p }));
    const extra = (r.body.orphans_removed || []).length
      ? ` ${r.body.orphans_removed.length} unused picture(s) tidied up.` : '';
    const failed = (r.body.orphans_failed || []).length
      ? ` Could not tidy: ${r.body.orphans_failed.join(', ')}.` : '';
    $('#save-status').textContent = `Saved at ${r.body.updated.at}.${extra}${failed}`;
    this.renderProblems([], r.body.warnings);
    this.render();
  }

  /* ---------------------------------------------------------------- storage self-test & log */
  async selftest() {
    const out = $('#selftest-out');
    out.hidden = false;
    out.textContent = 'Writing and reading a real object…';
    const r = await api('/api/settings/selftest');
    if (!r.ok) { out.textContent = `HTTP ${r.status}`; return; }
    const b = r.body;
    out.textContent = (b.ok
      ? `OK — wrote a binary object to ${b.base} and read it back BYTE-IDENTICAL.`
      : `PROBLEM — ${b.detail || 'the round-trip did not come back identical'}`)
      + `\n\n${JSON.stringify(b, null, 1)}`;
  }

  async loadLog() {
    const r = await api('/api/history?limit=40');
    if (!r.ok) return;
    $('#log-count').textContent = r.body.count
      ? `${r.body.count} spin(s) recorded; the most recent ${Math.min(40, r.body.count)} are shown.`
      : 'No spins recorded yet.';
    const ol = $('#log');
    ol.innerHTML = '';
    for (const row of r.body.rows) {
      const li = el('li');
      li.appendChild(el('strong', null, row.prize_name));
      li.appendChild(el('span', 'dim', ` ${row.chance}% · ${new Date(row.at).toLocaleString()}`));
      ol.appendChild(li);
    }
  }

  async clearLog() {
    if (!confirm('Clear the whole spin log? This cannot be undone.')) return;
    const r = await api('/api/history/clear', { method: 'POST' });
    $('#save-status').textContent = r.ok ? 'Spin log cleared.' : `Could not clear the log (${r.status}).`;
    this.loadLog();
  }
}

new Settings().init();
