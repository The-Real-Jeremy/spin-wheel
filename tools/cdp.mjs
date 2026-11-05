/* A zero-dependency CDP driver for a HEADED Chrome we launch ourselves.
 *
 * WHY IT EXISTS: browser checks need a Chrome we control end to end: our own headed Chrome on a
 * throwaway profile and a port we verified free, driven over CDP, killing only the process we started. Node 25 has a global
 * WebSocket, so no package is needed.
 *
 * ⛔ HEADED ONLY. Never `--headless`: a hidden window is not where browser work belongs, and a headless
 *    instance is easy to leave running unnoticed.
 *
 * ── FIVE TRAPS THIS FILE EXISTS TO NOT REPEAT, each of which produced a plausible measurement of the
 *    wrong thing on this project before:
 *  1. `Network.enable` MUST come before `setExtraHTTPHeaders`, or the header is silently dropped and the
 *     auth proxy serves a LOGIN PAGE that measures perfectly well.
 *  2. ONE long-lived connection. The header reverts when the Network domain detaches, so a
 *     connect-per-command pattern leaves the page's own fetches unauthenticated.
 *  3. ASSERT PAGE IDENTITY after every navigation (`assertIdentity`). A click that missed, or a redirect
 *     to a login wall, otherwise yields thirty screenshots of one stuck view under thirty different names.
 *  4. A non-2xx body is NOT data. `fetchJson` returns the status and refuses to parse a failure as content.
 *  5. Filter `chrome-extension://` out of console checks — a fresh profile can still inherit
 *     managed browser extensions, which throw their own errors.
 */
import { spawn } from 'node:child_process';
import { mkdirSync, rmSync, existsSync } from 'node:fs';

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

export async function launch({ port, profile, width = 1680, height = 1050 }) {
  if (existsSync(`${profile}/SingletonLock`)) {
    try { rmSync(`${profile}/SingletonLock`); } catch { /* not fatal */ }
  }
  mkdirSync(profile, { recursive: true });
  const child = spawn(CHROME, [
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
    `--window-size=${width},${height}`,
    'about:blank',
  ], { detached: true, stdio: 'ignore' });
  child.unref();

  // Wait for the debug endpoint, and confirm the process answering it is OURS (matching --user-data-dir).
  const deadline = Date.now() + 25000;
  let version = null;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) { version = await r.json(); break; }
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  if (!version) throw new Error(`Chrome did not open a debug port on ${port}`);
  console.log(`[cdp] HEADED Chrome pid=${child.pid} port=${port} profile=${profile}`);
  console.log(`[cdp] ${version.Browser}`);
  return { pid: child.pid, port, profile };
}

export class Session {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    this.consoleMessages = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`${msg.error.message} (${JSON.stringify(msg.error.data || '')})`));
        else resolve(msg.result);
      } else if (msg.method) {
        this.events.push(msg);
        if (msg.method === 'Runtime.consoleAPICalled' || msg.method === 'Log.entryAdded') {
          this.consoleMessages.push(msg);
        }
      }
    });
  }

  static async open(port) {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    let target = list.find((t) => t.type === 'page');
    if (!target) {
      await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' });
      const again = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      target = again.find((t) => t.type === 'page');
    }
    if (!target) throw new Error('no page target');
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    const s = new Session(ws);
    await s.send('Page.enable');
    await s.send('Runtime.enable');
    await s.send('Log.enable');
    return s;
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }
      }, 120000);
    });
  }

  /* ⛔ ORDER IS LOAD-BEARING: enable, THEN set the header. Reversed, the header is dropped in silence. */
  async auth(token) {
    await this.send('Network.enable');
    await this.send('Network.setExtraHTTPHeaders', { headers: { Authorization: `Bearer ${token}` } });
  }

  async goto(url, { waitMs = 700 } = {}) {
    const res = await this.send('Page.navigate', { url });
    if (res.errorText) throw new Error(`navigation failed: ${res.errorText}`);
    await this.send('Runtime.evaluate', {
      expression: 'new Promise(r => (document.readyState === "complete") ? r(1) '
        + ': window.addEventListener("load", () => r(1), {once:true}))',
      awaitPromise: true,
    });
    await new Promise((r) => setTimeout(r, waitMs));
    return res;
  }

  async eval(expression, { awaitPromise = false } = {}) {
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(`page threw: ${r.exceptionDetails.text} ${
        r.exceptionDetails.exception ? r.exceptionDetails.exception.description : ''}`);
    }
    return r.result.value;
  }

  /* Assert we are looking at the page we think we are, BEFORE believing any measurement taken from it. */
  async assertIdentity({ selector, contains, urlContains }) {
    const info = await this.eval(`(() => ({
      url: location.href,
      title: document.title,
      text: (document.querySelector(${JSON.stringify(selector)}) || {}).textContent || null,
      bodyBytes: document.documentElement.outerHTML.length,
    }))()`);
    if (urlContains && !info.url.includes(urlContains)) {
      throw new Error(`WRONG PAGE: url is ${info.url}, expected to contain ${urlContains}`);
    }
    if (info.text === null) {
      throw new Error(`WRONG PAGE: ${selector} is not present. title="${info.title}" `
        + `url=${info.url} html=${info.bodyBytes}B`
        + (info.bodyBytes > 20000 ? ' — that size suggests an auth/login wall, not this app' : ''));
    }
    if (contains && !String(info.text).includes(contains)) {
      throw new Error(`WRONG PAGE: ${selector} says "${info.text}", expected "${contains}"`);
    }
    return info;
  }

  async screenshot(path, { fullPage = false } = {}) {
    const r = await this.send('Page.captureScreenshot',
      { format: 'png', captureBeyondViewport: fullPage });
    const { writeFileSync } = await import('node:fs');
    writeFileSync(path, Buffer.from(r.data, 'base64'));
    return path;
  }

  async click(selector) {
    /* Click via the page, not a cached rect: a rect captured before an animation finished misses
     * invisibly, and nothing reports that the click went nowhere. */
    const hit = await this.eval(`(() => {
      const n = document.querySelector(${JSON.stringify(selector)});
      if (!n) return 'missing';
      if (n.disabled) return 'disabled';
      const r = n.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return 'zero-size';
      n.click();
      return 'ok';
    })()`);
    if (hit !== 'ok') throw new Error(`click(${selector}) -> ${hit}`);
  }

  /* Page errors only, with extension noise removed — a fresh profile can inherit managed extensions. */
  pageConsoleErrors() {
    return this.consoleMessages.filter((m) => {
      const entry = m.params.entry || m.params;
      const level = entry.level || entry.type;
      if (level !== 'error' && level !== 'warning') return false;
      const src = JSON.stringify(entry);
      return !src.includes('chrome-extension://');
    }).map((m) => {
      const e = m.params.entry || m.params;
      return (e.text || (e.args || []).map((a) => a.value ?? a.description).join(' ') || '').slice(0, 300);
    });
  }

  async fetchJson(url) {
    /* STATUS FIRST. A non-2xx body is not data — an OAuth wall's `{}` parses fine and reads as "nothing
     * is configured", which has produced a false red on this rig three times. */
    return this.eval(`(async () => {
      const r = await fetch(${JSON.stringify(url)});
      const text = await r.text();
      let body = null; try { body = JSON.parse(text); } catch (e) { body = null; }
      return { status: r.status, ok: r.ok, bytes: text.length, body,
               contentType: r.headers.get('content-type') };
    })()`, { awaitPromise: true });
  }

  close() { try { this.ws.close(); } catch { /* already gone */ } }
}

export function kill(pid) {
  /* Only the process we started. Never a sweep — another Chrome may hold the user's logged-in session. */
  try { process.kill(pid, 'SIGTERM'); console.log(`[cdp] killed our own Chrome pid=${pid}`); }
  catch { /* already exited */ }
}
