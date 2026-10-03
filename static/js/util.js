// Small pure helpers shared by every module. No DOM state, no network.

const LOCALE = 'de-CH';      // 24-hour clock and dd.mm. dates
const NUM_LOCALE = 'en-GB';  // 1,867.31 - plain, unambiguous grouping for money

const nfCache = new Map();
function nf(dec) {
  let f = nfCache.get(dec);
  if (!f) {
    f = new Intl.NumberFormat(NUM_LOCALE, { minimumFractionDigits: dec, maximumFractionDigits: dec });
    nfCache.set(dec, f);
  }
  return f;
}

export function isNum(n) {
  return typeof n === 'number' && Number.isFinite(n);
}

/** '+' / '-' / '' prefix for a number (the minus is a real U+2212). */
function signOf(n, show) {
  if (n < 0) return '\u2212';
  return show && n > 0 ? '+' : '';
}

/** EUR amount: "€1’867.31". opts: {sign:boolean, dec:number}. null/NaN -> '–'. */
export function fmtEur(n, { sign = false, dec } = {}) {
  if (!isNum(n)) return '\u2013';
  const d = dec != null ? dec : (Math.abs(n) >= 10000 ? 0 : 2);
  return signOf(n, sign) + '\u20ac' + nf(d).format(Math.abs(n));
}

/** Percent: "+1.23%". opts: {sign:boolean=true, dec:number=2}. null/NaN -> '–'. */
export function fmtPct(n, { sign = true, dec = 2 } = {}) {
  if (!isNum(n)) return '\u2013';
  return signOf(n, sign) + nf(dec).format(Math.abs(n)) + '%';
}

/** Plain number with fixed decimals. */
export function fmtNum(n, dec = 2) {
  return isNum(n) ? signOf(n, false) + nf(dec).format(Math.abs(n)) : '\u2013';
}

/** Accepts unix seconds, unix ms, Date or ISO string -> Date (or null). */
export function toDate(ts) {
  if (ts == null || ts === '') return null;
  if (ts instanceof Date) return Number.isNaN(ts.getTime()) ? null : ts;
  if (typeof ts === 'string' && !/^\d+(\.\d+)?$/.test(ts)) {
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return null;
  return new Date(n < 1e11 ? n * 1000 : n);
}

const timeFmt = new Intl.DateTimeFormat(LOCALE, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const dateTimeFmt = new Intl.DateTimeFormat(LOCALE, {
  day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const dateFmt = new Intl.DateTimeFormat(LOCALE, { day: '2-digit', month: '2-digit', year: 'numeric' });

/** "14:05" (24 h). */
export function fmtTime24(ts) {
  const d = toDate(ts);
  return d ? timeFmt.format(d) : '\u2013';
}
/** "02.10. 14:05". */
export function fmtDateTime24(ts) {
  const d = toDate(ts);
  return d ? dateTimeFmt.format(d) : '\u2013';
}
/** "02.10.2026". */
export function fmtDate(ts) {
  const d = toDate(ts);
  return d ? dateFmt.format(d) : '\u2013';
}

/** Seconds since ts (ts: seconds/ms/Date/ISO). null when unknown. */
export function ageSeconds(ts) {
  const d = toDate(ts);
  return d ? Math.max(0, (Date.now() - d.getTime()) / 1000) : null;
}

/** "12 s ago", "3 min ago", "2 h ago", "4 d ago"; '' when unknown. */
export function ago(ts) {
  const s = ageSeconds(ts);
  if (s == null) return '';
  if (s < 5) return 'just now';
  if (s < 90) return Math.round(s) + ' s ago';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 129600) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' d ago';
}

/** 'up' | 'down' | 'flat' for colouring a signed number. */
export function signCls(n) {
  if (!isNum(n) || n === 0) return 'flat';
  return n > 0 ? 'up' : 'down';
}

export function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

export function debounce(fn, ms) {
  let t = null;
  const d = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, ms);
  };
  d.cancel = () => { clearTimeout(t); t = null; };
  return d;
}

/**
 * Safe JSON.parse: returns `fallback` (default null) instead of throwing.
 */
export function safeJson(text, fallback = null) {
  try { return JSON.parse(text); } catch (_) { return fallback; }
}

/** Only http(s) / mailto URLs may become an href. Returns '' otherwise. */
export function safeUrl(u) {
  try {
    const url = new URL(String(u || ''), location.href);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : '';
  } catch (_) { return ''; }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/**
 * el(tag, attrs, ...children) -> HTMLElement. Never parses HTML.
 *  attrs: class | text | on<event> (function) | style (object, CSSOM) |
 *         dataset (object) | value/checked/disabled/selected/indeterminate (properties) |
 *         anything else via setAttribute (true -> '', false/null/undefined skipped).
 *  children: strings/numbers -> text nodes; Nodes appended; arrays flattened;
 *            null/undefined/false skipped.
 */
const PROPS = new Set(['value', 'checked', 'disabled', 'selected', 'indeterminate', 'readOnly']);
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs && typeof attrs === 'object' && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = String(v);
      else if (k === 'text') node.textContent = String(v);
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k === 'dataset' && typeof v === 'object') Object.assign(node.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else if (PROPS.has(k)) node[k] = v;
      else node.setAttribute(k, v === true ? '' : String(v));
    }
  } else if (attrs != null && attrs !== false) {
    children.unshift(attrs);
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) appendChildren(node, c);
    else node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** SVG element factory (same conventions as el, attributes via setAttribute). */
export function svg(tag, attrs, ...children) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'text') node.textContent = String(v);
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, String(v));
    }
  }
  appendChildren(node, children);
  return node;
}
