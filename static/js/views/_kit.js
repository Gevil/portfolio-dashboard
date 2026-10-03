/* Shared plumbing for the secondary views (digest / market / aiops).
   Owned by FrontendViews. Everything here codes to the core contract:
   ctx = {store, api, util, ui, markdown}. No innerHTML anywhere. */

const SVG_NS = 'http://www.w3.org/2000/svg';

/* ---------- value helpers ---------- */

export function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
export function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
export function str(v, fallback = '') {
  return v === null || v === undefined ? fallback : String(v);
}
/** epoch seconds | epoch ms | ISO string | Date -> epoch seconds (or null). */
export function toSec(v) {
  if (v === null || v === undefined || v === '') return null;
  if (v instanceof Date) return v.getTime() / 1000;
  if (typeof v === 'number' || /^\d+(\.\d+)?$/.test(String(v))) {
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return null;
    return n > 1e12 ? n / 1000 : n;
  }
  const p = Date.parse(String(v));
  return Number.isFinite(p) ? p / 1000 : null;
}
export function ageSec(v) {
  const s = toSec(v);
  return s === null ? null : Math.max(0, Date.now() / 1000 - s);
}
/** "12 s" / "5 min" / "3.2 h" / "2 d" */
export function fmtSpan(secs) {
  const s = Math.max(0, Math.round(Number(secs)));
  if (!Number.isFinite(s)) return '–';
  if (s < 90) return s + ' s';
  if (s < 5400) return Math.round(s / 60) + ' min';
  if (s < 172800) return (s / 3600).toFixed(1) + ' h';
  return Math.round(s / 86400) + ' d';
}
export function fmtAgo(v) {
  const a = ageSec(v);
  return a === null ? 'never' : fmtSpan(a) + ' ago';
}
export function fmtIn(v) {
  const s = toSec(v);
  if (s === null) return '–';
  const d = s - Date.now() / 1000;
  return d <= 0 ? 'due now' : 'in ' + fmtSpan(d);
}
const TIME_FMT = new Intl.DateTimeFormat('de-CH', { hour: '2-digit', minute: '2-digit', hour12: false });
const DATE_FMT = new Intl.DateTimeFormat('de-CH', { day: '2-digit', month: '2-digit' });
export function fmtClock(v) {
  const s = toSec(v);
  return s === null ? '–' : TIME_FMT.format(new Date(s * 1000));
}
export function fmtDay(v) {
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v)) return v.slice(8, 10) + '.' + v.slice(5, 7) + '.';
  const s = toSec(v);
  return s === null ? '–' : DATE_FMT.format(new Date(s * 1000)) + '.';
}
export function fmtWhen(v) {
  const s = toSec(v);
  return s === null ? '–' : fmtDay(v) + ' ' + fmtClock(v);
}
export function fmtNum(v, dec = 0) {
  const n = num(v);
  return n === null ? '–' : n.toLocaleString('de-CH', { minimumFractionDigits: dec, maximumFractionDigits: dec }).replace(/’/g, "'");
}
export function fmtPct(v, dec = 1, sign = false) {
  const n = num(v);
  if (n === null) return '–';
  return (sign && n > 0 ? '+' : '') + n.toFixed(dec) + '%';
}
export function tone(v) {
  const n = num(v);
  return n === null || n === 0 ? 'flat' : n > 0 ? 'up' : 'down';
}
/** Short readable message for any thrown / returned error shape. */
export function errText(e) {
  if (!e) return 'unknown error';
  if (typeof e === 'string') return e;
  if (e.message) return String(e.message);
  if (e.detail) return String(e.detail);
  if (e.error) return String(e.error);
  return String(e);
}

/* ---------- DOM helpers (thin layer over ctx.util.el) ---------- */

export function makeDom(ctx) {
  const el = ctx.util.el;
  const h = (tag, attrs, ...kids) => el(tag, attrs || {}, ...kids.flat().filter(k => k !== null && k !== undefined && k !== false).map(k => (typeof k === 'number' ? String(k) : k)));
  const chip = (text, kind, title) => h('span', { class: 'v-chip' + (kind ? ' v-chip--' + kind : ''), title: title || null }, text);
  const empty = (text) => h('p', { class: 'v-muted' }, text);
  const svg = (tag, attrs, ...kids) => {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, String(v));
    kids.forEach(c => c && n.appendChild(c));
    return n;
  };
  /** table with semantic head: cols = [{label, num?, cls?}] */
  const table = (caption, cols, rows, cls) => {
    const thead = h('thead', {}, h('tr', {}, ...cols.map(c => h('th', { scope: 'col', class: c.num ? 'v-num' : null }, c.label))));
    const tbody = h('tbody', {}, ...rows);
    return h('div', { class: 'v-tablewrap' },
      h('table', { class: 'v-table' + (cls ? ' ' + cls : '') }, caption ? h('caption', { class: 'v-sr' }, caption) : null, thead, tbody));
  };
  return { h, chip, empty, svg, table };
}

/* ---------- modal dialog: delegates to the shell's ui.openDialog (focus trap, Escape, focus restore) ---------- */

export function openDialog(ctx, title, buildBody) {
  const body = document.createElement('div');
  body.className = 'v-dialog__body';
  const dlg = ctx.ui.openDialog({ title, body, kind: 'modal', size: 'lg' });
  buildBody(body, dlg);
  return () => dlg.close();
}

/* ---------- view kit: sections, polling, lifecycle ---------- */

export function createKit(ctx, root) {
  const dom = makeDom(ctx);
  const { h } = dom;
  const ac = new AbortController();
  const stops = [];
  const dialogs = new Set();
  let disposed = false;

  const memo = (fn, ttlMs = 1500) => {
    let at = 0, p = null;
    return () => {
      if (!p || Date.now() - at > ttlMs) { at = Date.now(); p = Promise.resolve(fn()); }
      return p;
    };
  };
  /** GET with the view's abort signal. Never throws. */
  const get = (url) => ctx.api.apiGet(url, { signal: ac.signal, timeoutMs: 15000 });

  /** A titled panel with its own loading/error/empty/stale handling. */
  function section({ title, hint, cls, id, actions, headingLevel = 2 }) {
    const hid = 'v-h-' + (id || Math.random().toString(36).slice(2, 8));
    const stampSlot = h('span', { class: 'v-section__stamp' });
    const actionsEl = h('div', { class: 'v-section__actions' }, ...(actions || []));
    const body = h('div', { class: 'v-section__body', 'aria-busy': 'true' });
    const head = h('header', { class: 'v-section__head' },
      h('h' + headingLevel, { class: 'v-section__title', id: hid }, title),
      hint ? h('span', { class: 'v-section__hint' }, hint) : null,
      actionsEl, stampSlot);
    const el = h('section', { class: 'v-section' + (cls ? ' ' + cls : ''), 'aria-labelledby': hid }, head, body);

    const sec = { el, body, head, actions: actionsEl, hasContent: false, seq: 0, busy: false, last: null };

    sec.stamp = (tsSec, stale) => {
      stampSlot.textContent = '';
      if (tsSec) stampSlot.appendChild(ctx.ui.stamp(tsSec, { stale: !!stale }));
    };
    sec.state = (opts) => { ctx.ui.renderState(body, opts); sec.hasContent = false; body.setAttribute('aria-busy', opts.loading ? 'true' : 'false'); };
    sec.setContent = (build, tsSec, stale) => {
      body.textContent = '';
      body.removeAttribute('aria-busy');
      build(body);
      sec.hasContent = true;
      sec.stamp(tsSec || Date.now() / 1000, stale);
    };

    /* load({source, render, isEmpty, emptyText}). `source` = url | async () => res.
       `render(body, data, res)`. Overlap-safe: stale responses are dropped; a
       failed refresh keeps the previous content and flags it stale. */
    sec.load = async (opts) => {
      sec.opts = opts;
      if (disposed || sec.busy) return;
      sec.busy = true;
      const mine = ++sec.seq;
      if (!sec.hasContent) sec.state({ loading: true });
      let res;
      try {
        res = typeof opts.source === 'string' ? await get(opts.source) : await opts.source();
      } catch (e) {
        res = { ok: false, error: errText(e), status: 0 };
      }
      sec.busy = false;
      if (disposed || mine !== sec.seq || ac.signal.aborted) return;
      if (!res || !res.ok) {
        const msg = errText(res && res.error) + (res && res.status ? ' (HTTP ' + res.status + ')' : '');
        if (sec.hasContent) {
          sec.stamp(sec.last, true);
          sec.body.classList.add('is-stale');
          sec.body.title = 'Refresh failed: ' + msg;
        } else {
          sec.state({ error: msg, retry: () => sec.reload() });
        }
        return;
      }
      sec.body.classList.remove('is-stale');
      sec.body.removeAttribute('title');
      let data = res.data;
      if (opts.map) data = opts.map(data);
      if (opts.isEmpty ? opts.isEmpty(data) : (data === null || data === undefined)) {
        sec.state({ empty: opts.emptyText || 'Nothing to show yet.' });
        sec.last = Date.now() / 1000;
        sec.stamp(sec.last, res.stale);
        return;
      }
      try {
        sec.setContent((b) => opts.render(b, data, res), null, res.stale);
        sec.last = Date.now() / 1000;
        sec.stamp(sec.last, res.stale);
      } catch (e) {
        sec.state({ error: 'Could not display this panel: ' + errText(e), retry: () => sec.reload() });
      }
    };
    sec.reload = () => (sec.opts ? sec.load(sec.opts) : Promise.resolve());
    return sec;
  }

  function poll(fn, ms) {
    const stop = ctx.ui.usePolling(fn, ms, { visibilityAware: true });
    if (typeof stop === 'function') stops.push(stop);
    return stop;
  }
  /** load once now, then keep refreshing on visibility-aware polling. */
  function autoLoad(sec, opts, ms) {
    sec.load(opts);
    if (ms) poll(() => sec.reload(), ms);
  }
  const dialog = (title, build) => {
    const close = openDialog(ctx, title, build);
    dialogs.add(close);
    return close;
  };
  function dispose() {
    disposed = true;
    ac.abort();
    stops.splice(0).forEach(s => { try { s(); } catch (_) { /* noop */ } });
    dialogs.forEach(c => { try { c(); } catch (_) { /* noop */ } });
    dialogs.clear();
    if (root) root.textContent = '';
  }
  return { ...dom, ctx, signal: ac.signal, get, memo, section, poll, autoLoad, dialog, dispose, isDisposed: () => disposed };
}
