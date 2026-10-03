// Canvas/SVG charts: sparkline, price chart (+EMA/RSI/analysis markers), indexed comparison chart,
// allocation donut. Colours come from CSS variables (theme aware), nothing is global except the
// module-level registries that let one theme change / one ResizeObserver redraw everything alive.
import { el, svg, clear, fmtEur, fmtNum, fmtPct, fmtDate, fmtDateTime24, isNum, clamp, debounce } from './util.js';
import { renderState } from './ui.js';
import { subscribe } from './store.js';

const LOCALE = 'de-CH';
const RESIZE_MS = 120;
const INTRO_MS = 700;
const DAY = 86400;

// ------------------------------------------------------------------ colours

const COLOR_VARS = {
  line: ['--chart-line', '#22c55e'],
  grid: ['--chart-grid', 'rgba(138,158,176,0.14)'],
  axis: ['--chart-axis', '#8a9eb0'],
  ink: ['--ink', '#e8f1f7'],
  ink2: ['--ink-2', '#a8bccc'],
  flat: ['--ink-3', '#8a9eb0'],
  label: ['--label', '#c2d2de'],
  border: ['--line', '#22333f'],
  panel: ['--panel', '#101a24'],
  up: ['--up', '#22c55e'],
  down: ['--down', '#f0605d'],
  ema20: ['--chart-ema20', '#f5a524'],
  ema50: ['--chart-ema50', '#a78bfa'],
  rsi: ['--chart-rsi', '#38bdf8'],
  benchmark: ['--chart-benchmark', '#8a9eb0'],
  buy: ['--chart-buy', '#22c55e'],
  sell: ['--chart-sell', '#f0605d'],
  hold: ['--chart-hold', '#f5a524'],
  font: ['--font-mono', 'ui-monospace, Menlo, Consolas, monospace'],
};
const ALLOC_VARS = ['--alloc-1', '--alloc-2', '--alloc-3', '--alloc-4', '--alloc-5', '--alloc-6'];
const ALLOC_FALLBACK = ['#38bdf8', '#22c55e', '#f5a524', '#a78bfa', '#f472b6', '#2dd4bf'];

let colorCache = null;
const alphaCache = new Map();

/** Resolved chart colours (cached until the theme changes). Treat as read-only. */
export function chartColors() {
  if (colorCache) return colorCache;
  const cs = typeof getComputedStyle === 'function' && typeof document !== 'undefined'
    ? getComputedStyle(document.documentElement) : null;
  const read = (name, fb) => (cs && cs.getPropertyValue(name).trim()) || fb;
  const out = {};
  for (const [k, [name, fb]] of Object.entries(COLOR_VARS)) out[k] = read(name, fb);
  out.alloc = Object.freeze(ALLOC_VARS.map((n, i) => read(n, ALLOC_FALLBACK[i])));
  colorCache = Object.freeze(out);
  return colorCache;
}

function invalidateColors() {
  colorCache = null;
  alphaCache.clear();
}

/** Colour string with alpha applied (hex / rgb() parsed; anything else via color-mix). */
function withAlpha(color, a) {
  const key = color + '|' + a;
  let v = alphaCache.get(key);
  if (v) return v;
  let m = /^#([a-f\d]{3}|[a-f\d]{6})$/i.exec(color);
  if (m) {
    let h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    v = `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
  } else if ((m = /^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/i.exec(color))) {
    v = `rgba(${m[1]},${m[2]},${m[3]},${a})`;
  } else {
    v = `color-mix(in srgb, ${color} ${Math.round(a * 100)}%, transparent)`;
  }
  alphaCache.set(key, v);
  return v;
}

function reducedMotion() {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// ------------------------------------------------------------------ pure helpers

/** Index of the point whose .t is closest to t (data ascending). -1 when empty. */
export function nearestIndexByTime(data, t) {
  if (!data || !data.length) return -1;
  let lo = 0;
  let hi = data.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (data[mid].t < t) lo = mid + 1; else hi = mid;
  }
  if (lo > 0 && Math.abs(data[lo - 1].t - t) < Math.abs(data[lo].t - t)) lo--;
  return lo;
}

/**
 * Split the first `count` points into [startIdx, endIdx] runs; a new run starts whenever two
 * neighbours are further apart than gapSec (market closes: no line across the gap).
 */
export function segmentRanges(data, gapSec = Infinity, count = data ? data.length : 0) {
  const out = [];
  const n = Math.min(count, data ? data.length : 0);
  if (n < 1) return out;
  let start = 0;
  for (let i = 1; i < n; i++) {
    if (data[i].t - data[i - 1].t > gapSec) { out.push([start, i - 1]); start = i; }
  }
  out.push([start, n - 1]);
  return out;
}

/** Gap threshold (s) for the time-based 1D axis: 4x the typical bar step, at least 30 min. */
export function gapThreshold(data) {
  if (!data || data.length < 3) return Infinity;
  const steps = [];
  const stride = Math.max(1, Math.floor((data.length - 1) / 60));
  for (let i = stride; i < data.length; i += stride) steps.push(data[i].t - data[i - stride].t);
  steps.sort((a, b) => a - b);
  const med = steps[steps.length >> 1] / stride;
  return Math.max(1800, med * 4);
}

/** "Nice" tick values inside [lo, hi]: {ticks, step}. */
export function niceTicks(lo, hi, count = 4) {
  const span = hi - lo;
  if (!(span > 0) || !Number.isFinite(span)) return { ticks: Number.isFinite(lo) ? [lo] : [], step: 1 };
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
  return { ticks, step };
}

const fmtMonth = new Intl.DateTimeFormat(LOCALE, { month: 'short' });
const fmtMonthYear = new Intl.DateTimeFormat(LOCALE, { month: 'short', year: '2-digit' });
const fmtDayMonth = new Intl.DateTimeFormat(LOCALE, { day: '2-digit', month: '2-digit' });
const fmtWeekday = new Intl.DateTimeFormat(LOCALE, { weekday: 'short', day: 'numeric' });

function pad2(n) { return String(n).padStart(2, '0'); }
function dayKey(d) { return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate(); }
function weekKey(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7)).getTime(); }
function monthKey(d) { return d.getFullYear() * 12 + d.getMonth(); }

/**
 * X-axis ticks for the price chart. 1D is time-based (hourly); longer ranges are bar-index
 * based, ticks sit on the first bar of each day / week / month. Thinned to >= minGap pixels.
 * data: [{t}], pr: {x, w}. Returns [{x, label}].
 */
export function timeTicks(data, range, pr, minGap = 56) {
  const ticks = [];
  const n = data ? data.length : 0;
  if (n < 2) return ticks;
  const t0 = data[0].t;
  const t1 = data[n - 1].t;
  const span = t1 - t0 || 1;
  const xForIdx = i => pr.x + (i / (n - 1)) * pr.w;
  const xForT = t => pr.x + ((t - t0) / span) * pr.w;
  const firstOfEach = (keyFn, labelFn) => {
    let last = null;
    for (let i = 0; i < n; i++) {
      const d = new Date(data[i].t * 1000);
      const k = keyFn(d);
      if (k === last) continue;
      last = k;
      ticks.push({ x: xForIdx(i), label: labelFn(d) });
    }
  };
  if (range === '1D' && span <= 30 * 3600) {
    for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) {
      ticks.push({ x: xForT(t), label: pad2(new Date(t * 1000).getHours()) + ':00' });
    }
  } else if (range === '1D') {
    // multi-day intraday window: time-based, one tick per local day
    let last = null;
    for (const p of data) {
      const d = new Date(p.t * 1000);
      const k = dayKey(d);
      if (k !== last) { last = k; ticks.push({ x: xForT(p.t), label: fmtDayMonth.format(d) }); }
    }
  } else if (range === '1W') {
    firstOfEach(dayKey, d => fmtWeekday.format(d));
  } else if (range === '1M') {
    firstOfEach(weekKey, d => fmtDayMonth.format(d));
  } else {
    firstOfEach(monthKey, d => (d.getMonth() === 0 ? fmtMonthYear : fmtMonth).format(d));
  }
  const kept = [];
  for (const t of ticks) {
    if (kept.length && t.x - kept[kept.length - 1].x < minGap) continue;
    kept.push(t);
  }
  return kept;
}

/**
 * Calendar-aligned date ticks for a time axis [t0, t1] (unix s) with at most maxCount ticks.
 * Returns [{t, label}] (t in unix s).
 */
export function dateTicks(t0, t1, maxCount) {
  const out = [];
  if (!(t1 > t0)) return out;
  const start = new Date(t0 * 1000);
  const steps = [['d', 1], ['d', 2], ['d', 7], ['d', 14], ['m', 1], ['m', 2], ['m', 3], ['m', 6], ['m', 12]];
  let ticks = [];
  for (const [unit, k] of steps) {
    ticks = [];
    if (unit === 'd') {
      let d = new Date(start.getFullYear(), start.getMonth(), start.getDate());
      if (k >= 7) while (d.getDay() !== 1) d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1);
      for (; d.getTime() / 1000 <= t1; d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + k)) {
        if (d.getTime() / 1000 >= t0) ticks.push({ t: d.getTime() / 1000, label: fmtDayMonth.format(d) });
      }
    } else {
      let d = new Date(start.getFullYear(), start.getMonth(), 1);
      for (; d.getTime() / 1000 <= t1; d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) {
        if (monthKey(d) % k !== 0 || d.getTime() / 1000 < t0) continue;
        ticks.push({ t: d.getTime() / 1000, label: (d.getMonth() === 0 || !ticks.length ? fmtMonthYear : fmtMonth).format(d) });
      }
    }
    if (ticks.length <= maxCount) return ticks;
  }
  return ticks.slice(0, Math.max(1, maxCount));
}

/** Map analysis decision/action text to 'buy' | 'sell' | 'hold'. */
export function markerKind(m) {
  const rx = (s, re) => re.test(String(s || '').toUpperCase());
  const BUY = /BUY|OVERWEIGHT|ACCUMULATE|\bADD\b|LONG/;
  const SELL = /SELL|UNDERWEIGHT|REDUCE|TRIM|EXIT|SHORT/;
  const HOLD = /HOLD|NEUTRAL|WAIT|MAINTAIN/;
  const dec = m && m.decision;
  if (rx(dec, BUY)) return 'buy';
  if (rx(dec, SELL)) return 'sell';
  if (rx(dec, HOLD)) return 'hold';
  const act = m && m.action;
  if (rx(act, BUY)) return 'buy';
  if (rx(act, SELL)) return 'sell';
  return 'hold';
}

/**
 * Align two {t, pct} series on the union of their timestamps (step carry-forward inside each
 * series' own range, null outside). Returns [{t, p, b}] ascending.
 */
export function mergeIndexed(portfolio, benchmark) {
  const P = (portfolio || []).filter(r => r && isNum(r.t) && isNum(r.pct));
  const B = (benchmark || []).filter(r => r && isNum(r.t) && isNum(r.pct));
  const ts = [...new Set([...P.map(r => r.t), ...B.map(r => r.t)])].sort((a, b) => a - b);
  const walk = (S) => {
    let j = 0;
    return (t) => {
      if (!S.length || t < S[0].t || t > S[S.length - 1].t) return null;
      while (j + 1 < S.length && S[j + 1].t <= t) j++;
      return S[j].pct;
    };
  };
  const pAt = walk(P);
  const bAt = walk(B);
  return ts.map(t => ({ t, p: pAt(t), b: bAt(t) }));
}

export const chartMath = { nearestIndexByTime, segmentRanges, gapThreshold, niceTicks, timeTicks, dateTicks, markerKind, mergeIndexed, withAlpha };

// ------------------------------------------------------------------ canvas plumbing

/** Size a canvas to its CSS box x DPR, reset the context, clear. null when it has no size yet. */
function prep(canvas, minSize = 1) {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w < minSize || h < minSize) return null;
  const dpr = window.devicePixelRatio || 1;
  const pw = Math.round(w * dpr);
  const ph = Math.round(h * dpr);
  if (canvas.width !== pw) canvas.width = pw;
  if (canvas.height !== ph) canvas.height = ph;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.setLineDash([]);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function fmtPrice(v, currency, dec) {
  if (!currency || currency === 'EUR') return fmtEur(v, dec == null ? {} : { dec });
  return fmtNum(v, dec == null ? 2 : dec) + '\u00a0' + currency;
}

function fmtSigned(n, dec = 1, suffix = '') {
  if (!isNum(n)) return '\u2013';
  return (n > 0 ? '+' : n < 0 ? '\u2212' : '') + fmtNum(Math.abs(n), dec) + suffix;
}

function dir(n) { return n > 0 ? 'up' : n < 0 ? 'down' : 'flat'; }

// ------------------------------------------------------------------ live registries + theme

const liveCharts = new Set();   // {redraw}
const sparkSet = new Set();     // canvases
const sparkState = new WeakMap();
let sparkRO = null;

function redrawEverything() {
  for (const c of [...liveCharts]) {
    try { c.redraw(); } catch (e) { console.error('chart redraw failed', e); }
  }
  redrawSparks();
}

function onThemeChanged() {
  invalidateColors();
  redrawEverything();
}

subscribe('theme', onThemeChanged);
if (typeof matchMedia === 'function') {
  // The store 'theme' key is owned by main.js; the OS scheme flips CSS variables even before
  // it republishes, so repaint once the new variables are in effect.
  const mq = matchMedia('(prefers-color-scheme: dark)');
  const onScheme = () => {
    const run = () => { invalidateColors(); redrawEverything(); };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run); else run();
  };
  if (mq.addEventListener) mq.addEventListener('change', onScheme);
  else if (mq.addListener) mq.addListener(onScheme);
}

// ------------------------------------------------------------------ sparkline

function redrawSparks() {
  for (const canvas of [...sparkSet]) {
    if (!canvas.isConnected) {
      sparkSet.delete(canvas);
      if (sparkRO) sparkRO.unobserve(canvas);
      continue;
    }
    paintSpark(canvas);
  }
}

function registerSpark(canvas) {
  if (sparkSet.has(canvas)) return;
  sparkSet.add(canvas);
  if (!sparkRO && typeof ResizeObserver === 'function') {
    sparkRO = new ResizeObserver(debounce(redrawSparks, RESIZE_MS));
  }
  if (sparkRO) sparkRO.observe(canvas);
  // drop canvases of removed rows once the registry gets big
  if (sparkSet.size > 64 && sparkSet.size % 32 === 0) {
    for (const c of [...sparkSet]) {
      if (!c.isConnected) { sparkSet.delete(c); if (sparkRO) sparkRO.unobserve(c); }
    }
  }
}

function paintSpark(canvas) {
  const st = sparkState.get(canvas);
  if (!st) return;
  const c = prep(canvas, 4);
  if (!c) return;
  const { ctx, w, h } = c;
  const v = st.values;
  let n = 0;
  let min = Infinity;
  let max = -Infinity;
  for (let i = 0; i < v.length; i++) {
    const x = v[i];
    if (!isNum(x)) continue;
    n++;
    if (x < min) min = x;
    if (x > max) max = x;
  }
  if (n < 2) return;
  const col = chartColors();
  const color = st.up === true ? col.up : st.up === false ? col.down : col.flat;
  const range = max - min || 1;
  const pts = [];
  const len = v.length;
  for (let i = 0; i < len; i++) {
    if (!isNum(v[i])) continue;
    pts.push((i / (len - 1)) * w, h - ((v[i] - min) / range) * (h - 4) - 2);
  }
  if (st.fill) {
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, withAlpha(color, 0.18));
    grad.addColorStop(1, withAlpha(color, 0));
    ctx.beginPath();
    ctx.moveTo(pts[0], pts[1]);
    for (let i = 2; i < pts.length; i += 2) ctx.lineTo(pts[i], pts[i + 1]);
    ctx.lineTo(pts[pts.length - 2], h);
    ctx.lineTo(pts[0], h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
  }
  ctx.beginPath();
  ctx.moveTo(pts[0], pts[1]);
  for (let i = 2; i < pts.length; i += 2) ctx.lineTo(pts[i], pts[i + 1]);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.stroke();
}

/** Draw (and keep redrawing on resize/theme) a sparkline. up: true green, false red, null neutral. */
export function drawSparkline(canvas, values, { up = null, fill = false } = {}) {
  if (!canvas) return;
  sparkState.set(canvas, { values: Array.isArray(values) ? values : [], up, fill });
  registerSpark(canvas);
  paintSpark(canvas);
}

// ------------------------------------------------------------------ chart shell (shared by price + indexed)

/**
 * Shared DOM + interaction: focusable role=img stage, crosshair overlay canvas, tooltip,
 * state overlay, legend list, live region, debounced ResizeObserver, hover (pointer, touch,
 * keyboard), intro animation loop.
 * hooks: {count(), indexAt(x), draw(progress), drawCross(ctx,w,h,idx), renderTip(node,idx),
 *         xOf(idx), announce(idx), onIndex(idx|null)}
 */
function createShell(host, { cls, ariaLabel, canvases, hooks }) {
  const cross = el('canvas', { class: 'ch-canvas ch-cross', 'aria-hidden': 'true' });
  const tip = el('div', { class: 'ch-tip', 'aria-hidden': 'true', hidden: true });
  const stage = el('div', { class: 'ch-stage', tabindex: '0', role: 'img', 'aria-label': ariaLabel }, ...canvases, cross, tip);
  const layer = el('div', { class: 'ch-state-layer', hidden: true });
  const box = el('div', { class: 'ch-box' }, stage, layer);
  const key = el('ul', { class: 'ch-key' });
  const sr = el('div', { class: 'visually-hidden', role: 'status', 'aria-live': 'polite' });
  const root = el('div', { class: 'ch ' + cls }, box, key, sr);
  host.append(root);

  let hover = null;
  let kbMode = false;
  let raf = 0;
  let anim = 0;
  let touchTimer = 0;
  let destroyed = false;
  const listeners = [];
  const handle = { redraw: () => redraw() };

  const on = (target, type, fn, opts) => {
    target.addEventListener(type, fn, opts);
    listeners.push(() => target.removeEventListener(type, fn, opts));
  };

  function paintCross() {
    const c = prep(cross);
    if (!c || hover == null) { tip.hidden = true; return; }
    hooks.drawCross(c.ctx, c.w, c.h, hover);
    clear(tip);
    hooks.renderTip(tip, hover);
    tip.hidden = false;
    const x = hooks.xOf(hover);
    const tw = tip.offsetWidth;
    const sw = stage.clientWidth;
    let left = x + 14;
    if (left + tw > sw - 4) left = x - 14 - tw;
    tip.style.left = clamp(left, 4, Math.max(4, sw - tw - 4)) + 'px';
  }

  function queueCross() {
    if (raf || destroyed) return;
    raf = requestAnimationFrame(() => { raf = 0; if (!destroyed) paintCross(); });
  }

  function emit(i) {
    if (!hooks.onIndex) return;
    try { hooks.onIndex(i); } catch (e) { console.error('chart onHover failed', e); }
  }

  function setHover(i, { kbd = false } = {}) {
    if (i == null || i === hover) return;
    hover = i;
    kbMode = kbd;
    queueCross();
    emit(i);
    if (kbd) sr.textContent = hooks.announce(i);
  }

  function clearHover() {
    if (hover == null) return;
    hover = null;
    kbMode = false;
    sr.textContent = '';
    queueCross();
    emit(null);
  }

  function cancelAnim() {
    if (anim) { cancelAnimationFrame(anim); anim = 0; }
  }

  function redraw() {
    if (destroyed) return;
    cancelAnim();
    hooks.draw(1);
    paintCross();
  }

  function animate() {
    cancelAnim();
    if (destroyed) return;
    const t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / INTRO_MS);
      hooks.draw(1 - Math.pow(1 - p, 3));
      anim = p < 1 ? requestAnimationFrame(step) : 0;
      if (p >= 1) paintCross();
    };
    anim = requestAnimationFrame(step);
  }

  function setState(s, hasData) {
    if (!s) {
      layer.hidden = true;
      clear(layer);
      layer.classList.remove('is-soft');
      stage.removeAttribute('aria-hidden');
      stage.tabIndex = 0;
      return;
    }
    const soft = !!s.loading && !s.error && !s.empty && hasData;
    layer.hidden = false;
    layer.classList.toggle('is-soft', soft);
    if (soft) {
      clear(layer);
      layer.setAttribute('aria-busy', 'true');
      layer.append(el('div', { class: 'ch-pill', role: 'status', text: 'Updating\u2026' }));
    } else {
      layer.removeAttribute('aria-busy');
      renderState(layer, { loading: s.loading, error: s.error, empty: s.empty });
      stage.setAttribute('aria-hidden', 'true');
      stage.tabIndex = -1;
      clearHover();
    }
    if (soft) { stage.removeAttribute('aria-hidden'); stage.tabIndex = 0; }
  }

  // --- interaction
  const onPointer = (e) => {
    if (e.pointerType === 'touch') clearTimeout(touchTimer);
    const r = stage.getBoundingClientRect();
    const i = hooks.indexAt(e.clientX - r.left);
    if (i != null) setHover(i);
  };
  const onLeave = (e) => { if (e.pointerType !== 'touch') clearHover(); };
  const onUp = (e) => {
    if (e.pointerType !== 'touch') return;
    clearTimeout(touchTimer);
    touchTimer = setTimeout(clearHover, 3000);
  };
  const onKey = (e) => {
    const n = hooks.count();
    if (!n) return;
    let i = hover;
    switch (e.key) {
      case 'ArrowLeft': i = hover == null ? n - 1 : hover - (e.shiftKey ? 10 : 1); break;
      case 'ArrowRight': i = hover == null ? n - 1 : hover + (e.shiftKey ? 10 : 1); break;
      case 'Home': i = 0; break;
      case 'End': i = n - 1; break;
      case 'Escape':
        if (hover != null) { clearHover(); e.preventDefault(); e.stopPropagation(); }
        return;
      default: return;
    }
    e.preventDefault();
    setHover(clamp(i, 0, n - 1), { kbd: true });
  };
  on(stage, 'pointermove', onPointer);
  on(stage, 'pointerdown', onPointer);
  on(stage, 'pointerleave', onLeave);
  on(stage, 'pointerup', onUp);
  on(stage, 'pointercancel', () => clearHover());
  on(stage, 'keydown', onKey);
  on(stage, 'blur', () => { if (kbMode) clearHover(); });

  // --- resize (unconditional: independent of prefers-reduced-motion)
  const onResize = debounce(() => redraw(), RESIZE_MS);
  let ro = null;
  if (typeof ResizeObserver === 'function') {
    ro = new ResizeObserver(() => onResize());
    ro.observe(stage);
  }
  liveCharts.add(handle);

  return {
    root, stage, key, tip, get hover() { return hover; },
    setAria: (s) => stage.setAttribute('aria-label', s),
    setState, redraw, animate, cancelAnim, resetHover: clearHover,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      cancelAnim();
      if (raf) cancelAnimationFrame(raf);
      clearTimeout(touchTimer);
      onResize.cancel();
      if (ro) ro.disconnect();
      for (const off of listeners) off();
      liveCharts.delete(handle);
      root.remove();
    },
  };
}

function keyItem(glyphCls, text, extra) {
  return el('li', { class: 'ch-key-item' }, el('span', { class: 'ch-glyph ' + glyphCls, 'aria-hidden': 'true' }), el('span', { text }), extra || null);
}

function tipRow(label, value, { cls = '', swatch = '' } = {}) {
  return el('div', { class: 'ch-tip-row' },
    el('span', { class: 'ch-tip-label' }, swatch ? el('span', { class: 'ch-glyph ' + swatch, 'aria-hidden': 'true' }) : null, label),
    el('span', { class: 'ch-tip-val num ' + cls, text: value }));
}

// ------------------------------------------------------------------ price chart

const RANGE_NAMES = { '1D': '1 day', '1W': '1 week', '1M': '1 month', '3M': '3 months', '1Y': '1 year' };
const PAD_L = 8;
const PAD_T = 10;
const PAD_B = 22;

function buildSeriesMap(t, series) {
  const m = new Map();
  if (!Array.isArray(t) || !Array.isArray(series)) return m;
  const n = Math.min(t.length, series.length);
  for (let i = 0; i < n; i++) if (isNum(series[i])) m.set(t[i], series[i]);
  return m;
}

function medianStep(points) {
  if (points.length < 2) return 0;
  const steps = [];
  const stride = Math.max(1, Math.floor((points.length - 1) / 40));
  for (let i = stride; i < points.length; i += stride) steps.push((points[i].t - points[i - stride].t) / stride);
  steps.sort((a, b) => a - b);
  return steps[steps.length >> 1];
}

export function createPriceChart(host, { onHover } = {}) {
  const priceCanvas = el('canvas', { class: 'ch-canvas ch-canvas-price', 'aria-hidden': 'true' });
  const rsiCanvas = el('canvas', { class: 'ch-canvas ch-canvas-rsi', 'aria-hidden': 'true', hidden: true });

  let data = null;          // {points, range, currency, label, key, daily}
  let overlays = { ema20: false, ema50: false, rsi14: false };
  let maps = { ema20: new Map(), ema50: new Map(), rsi14: new Map() };
  let markers = [];         // [{idx, kind, text}]
  let markersAt = new Map();
  let state = null;
  let view = null;          // geometry of the last price draw

  const rsiOn = () => !!(overlays.rsi14 && data && data.points.length >= 2 && maps.rsi14.size > 0);
  const hasPts = () => !!(data && data.points.length >= 2);

  const shell = createShell(host, {
    cls: 'ch-price',
    ariaLabel: 'Price chart',
    canvases: [priceCanvas, rsiCanvas],
    hooks: {
      count: () => (data ? data.points.length : 0),
      indexAt(x) {
        if (!view) return null;
        const f = clamp((x - view.pr.x) / view.pr.w, 0, 1);
        if (view.useIndex) return Math.round(f * (view.n - 1));
        return nearestIndexByTime(data.points, view.t0 + f * view.span);
      },
      xOf: (i) => (view ? view.xAt(i) : 0),
      draw,
      drawCross,
      renderTip,
      announce(i) {
        const p = data.points[i];
        return `${timeLabel(p.t)}, ${fmtPrice(p.c, data.currency)}`;
      },
      onIndex(i) {
        if (!onHover) return;
        onHover(i == null || !data ? null : { index: i, point: data.points[i] });
      },
    },
  });

  function timeLabel(t) {
    return data && data.daily ? fmtDate(t) : fmtDateTime24(t);
  }

  function draw(progress) {
    drawPrice(progress);
    drawRsi();
  }

  function drawPrice(progress) {
    const c = prep(priceCanvas);
    view = null;
    if (!c || !hasPts()) return;
    const { ctx, w, h } = c;
    if (w < 40 || h < 40) return;
    const pts = data.points;
    const n = pts.length;
    const col = chartColors();

    let min = Infinity;
    let max = -Infinity;
    for (let i = 0; i < n; i++) {
      const v = pts[i].c;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const pad = (max - min) * 0.08 || Math.abs(max) * 0.01 || 1;
    const lo = min - pad;
    const hi = max + pad;
    const { ticks, step } = niceTicks(lo, hi, 4);
    const dec = step >= 1 ? 0 : 2;

    ctx.font = `11px ${col.font}`;
    let lane = 36;
    for (const v of ticks) lane = Math.max(lane, ctx.measureText(fmtPrice(v, data.currency, dec)).width + 14);
    const padR = Math.min(88, Math.ceil(lane));
    const pr = { x: PAD_L, y: PAD_T, w: Math.max(10, w - PAD_L - padR), h: Math.max(10, h - PAD_T - PAD_B) };
    const yAt = (v) => pr.y + pr.h - ((v - lo) / (hi - lo)) * pr.h;
    const useIndex = data.range !== '1D';
    const t0 = pts[0].t;
    const span = (pts[n - 1].t - t0) || 1;
    const xAt = (i) => (useIndex ? pr.x + (i / (n - 1)) * pr.w : pr.x + ((pts[i].t - t0) / span) * pr.w);
    view = { w, h, pr, lo, hi, yAt, xAt, n, useIndex, t0, span, padR };

    // grid + price labels (right lane)
    ctx.lineWidth = 1;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    for (const v of ticks) {
      const y = Math.round(yAt(v)) + 0.5;
      ctx.strokeStyle = col.grid;
      ctx.beginPath();
      ctx.moveTo(pr.x, y);
      ctx.lineTo(pr.x + pr.w, y);
      ctx.stroke();
      ctx.fillStyle = col.label;
      ctx.fillText(fmtPrice(v, data.currency, dec), pr.x + pr.w + 8, y);
    }

    // baseline + time ticks
    ctx.strokeStyle = col.border;
    ctx.beginPath();
    ctx.moveTo(pr.x, pr.y + pr.h + 0.5);
    ctx.lineTo(pr.x + pr.w, pr.y + pr.h + 0.5);
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const t of timeTicks(pts, data.range, pr)) {
      if (t.x < pr.x - 1 || t.x > pr.x + pr.w + 1) continue;
      ctx.strokeStyle = col.grid;
      ctx.beginPath();
      ctx.moveTo(t.x, pr.y);
      ctx.lineTo(t.x, pr.y + pr.h);
      ctx.stroke();
      ctx.strokeStyle = col.border;
      ctx.beginPath();
      ctx.moveTo(t.x, pr.y + pr.h);
      ctx.lineTo(t.x, pr.y + pr.h + 4);
      ctx.stroke();
      ctx.fillStyle = col.label;
      ctx.fillText(t.label, clamp(t.x, pr.x + 14, pr.x + pr.w - 14), pr.y + pr.h + 8);
    }

    // area + line per gap segment (no edges across market closes)
    const count = Math.max(2, Math.floor(n * Math.min(1, progress)));
    const color = pts[n - 1].c >= pts[0].c ? col.up : col.down;
    const segs = segmentRanges(pts, useIndex ? Infinity : gapThreshold(pts), count);
    const grad = ctx.createLinearGradient(0, pr.y, 0, pr.y + pr.h);
    grad.addColorStop(0, withAlpha(color, 0.22));
    grad.addColorStop(1, withAlpha(color, 0));
    for (const [s, e] of segs) {
      if (e - s < 1) continue;
      ctx.beginPath();
      ctx.moveTo(xAt(s), yAt(pts[s].c));
      for (let i = s + 1; i <= e; i++) ctx.lineTo(xAt(i), yAt(pts[i].c));
      ctx.lineTo(xAt(e), pr.y + pr.h);
      ctx.lineTo(xAt(s), pr.y + pr.h);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    for (const [s, e] of segs) {
      if (e - s < 1) continue;
      ctx.beginPath();
      ctx.moveTo(xAt(s), yAt(pts[s].c));
      for (let i = s + 1; i <= e; i++) ctx.lineTo(xAt(i), yAt(pts[i].c));
      ctx.stroke();
    }

    // EMA overlays (clipped to the plot, matched by timestamp, null = break)
    for (const id of ['ema20', 'ema50']) {
      if (!overlays[id] || !maps[id].size) continue;
      ctx.save();
      ctx.beginPath();
      ctx.rect(pr.x, pr.y, pr.w, pr.h);
      ctx.clip();
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < count; i++) {
        const v = maps[id].get(pts[i].t);
        if (v == null) { started = false; continue; }
        if (started) ctx.lineTo(xAt(i), yAt(v)); else { ctx.moveTo(xAt(i), yAt(v)); started = true; }
      }
      ctx.strokeStyle = col[id];
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.restore();
    }

    // analysis markers (distinct shapes, not colour alone)
    for (const m of markers) {
      if (m.idx >= count) continue;
      drawMarker(ctx, col, m.kind, xAt(m.idx), yAt(pts[m.idx].c));
    }

    // live dot while the data is fresh
    if (count >= n) {
      const lx = xAt(n - 1);
      const ly = yAt(pts[n - 1].c);
      if (Date.now() / 1000 - pts[n - 1].t < 300) {
        ctx.beginPath();
        ctx.arc(lx, ly, 7, 0, Math.PI * 2);
        ctx.fillStyle = withAlpha(color, 0.2);
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(lx, ly, 3, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    }
  }

  function drawMarker(ctx, col, kind, x, y) {
    const color = col[kind];
    ctx.beginPath();
    ctx.arc(x, y, 9, 0, Math.PI * 2);
    ctx.fillStyle = withAlpha(color, 0.18);
    ctx.fill();
    ctx.beginPath();
    if (kind === 'buy') { ctx.moveTo(x, y - 5); ctx.lineTo(x + 5, y + 4); ctx.lineTo(x - 5, y + 4); }
    else if (kind === 'sell') { ctx.moveTo(x - 5, y - 4); ctx.lineTo(x + 5, y - 4); ctx.lineTo(x, y + 5); }
    else { ctx.moveTo(x, y - 5); ctx.lineTo(x + 5, y); ctx.lineTo(x, y + 5); ctx.lineTo(x - 5, y); }
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = col.panel;
    ctx.stroke();
  }

  function syncRsiVisibility() {
    rsiCanvas.hidden = !rsiOn();
  }

  function drawRsi() {
    syncRsiVisibility();
    if (rsiCanvas.hidden || !view) return;
    const c = prep(rsiCanvas);
    if (!c) return;
    const { ctx, w, h } = c;
    const col = chartColors();
    const pts = data.points;
    const pr = { x: view.pr.x, y: 6, w: view.pr.w, h: Math.max(10, h - 12) };
    const yAt = (v) => pr.y + pr.h - (clamp(v, 0, 100) / 100) * pr.h;
    ctx.font = `10px ${col.font}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    for (const lvl of [30, 50, 70]) {
      const y = Math.round(yAt(lvl)) + 0.5;
      ctx.setLineDash(lvl === 50 ? [] : [3, 3]);
      ctx.strokeStyle = lvl === 50 ? col.grid : withAlpha(col.hold, 0.35);
      ctx.beginPath();
      ctx.moveTo(pr.x, y);
      ctx.lineTo(pr.x + pr.w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col.label;
      ctx.fillText(String(lvl), pr.x + pr.w + 8, y);
    }
    ctx.textBaseline = 'top';
    ctx.fillStyle = col.label;
    ctx.fillText('RSI 14', pr.x + 2, 1);
    ctx.beginPath();
    let started = false;
    let last = null;
    for (let i = 0; i < pts.length; i++) {
      const v = maps.rsi14.get(pts[i].t);
      if (v == null) { started = false; continue; }
      const x = view.xAt(i);
      const y = yAt(v);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
      last = { x, y };
    }
    ctx.strokeStyle = col.rsi;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();
    if (last) {
      ctx.beginPath();
      ctx.arc(last.x, last.y, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = col.rsi;
      ctx.fill();
    }
  }

  function drawCross(ctx, w, h, i) {
    if (!view || !data || i >= data.points.length) return;
    const col = chartColors();
    const p = data.points[i];
    const { pr } = view;
    const x = view.xAt(i);
    const y = view.yAt(p.c);
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = withAlpha(col.ink2, 0.55);
    ctx.beginPath();
    ctx.moveTo(x + 0.5, pr.y);
    ctx.lineTo(x + 0.5, pr.y + pr.h);
    ctx.moveTo(pr.x, y + 0.5);
    ctx.lineTo(pr.x + pr.w, y + 0.5);
    if (!rsiCanvas.hidden) {
      ctx.moveTo(x + 0.5, rsiCanvas.offsetTop + 6);
      ctx.lineTo(x + 0.5, rsiCanvas.offsetTop + rsiCanvas.clientHeight - 6);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    // price bubble in the right lane
    const label = fmtPrice(p.c, data.currency);
    ctx.font = `11px ${col.font}`;
    const tw = ctx.measureText(label).width + 10;
    const bx = pr.x + pr.w + 4;
    const by = clamp(y - 8, pr.y, pr.y + pr.h - 16);
    ctx.fillStyle = col.panel;
    ctx.strokeStyle = col.border;
    ctx.beginPath();
    ctx.rect(bx, by, Math.min(tw, w - bx), 16);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = col.ink;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, bx + 5, by + 8);

    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fillStyle = col.ink;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = col.panel;
    ctx.stroke();
    if (!rsiCanvas.hidden) {
      const v = maps.rsi14.get(p.t);
      if (v != null) {
        const ry = rsiCanvas.offsetTop + 6 + (rsiCanvas.clientHeight - 12) * (1 - clamp(v, 0, 100) / 100);
        ctx.beginPath();
        ctx.arc(x, ry, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = col.rsi;
        ctx.fill();
        ctx.stroke();
      }
    }
  }

  function renderTip(node, i) {
    const pts = data.points;
    const p = pts[i];
    node.append(el('div', { class: 'ch-tip-time', text: timeLabel(p.t) }));
    node.append(el('div', { class: 'ch-tip-main num', text: fmtPrice(p.c, data.currency) }));
    const chg = pts[0].c ? ((p.c - pts[0].c) / pts[0].c) * 100 : null;
    node.append(tipRow('vs. range start', fmtPct(chg), { cls: dir(chg) }));
    for (const [id, name] of [['ema20', 'EMA 20'], ['ema50', 'EMA 50']]) {
      const v = overlays[id] ? maps[id].get(p.t) : null;
      if (v != null) node.append(tipRow(name, fmtPrice(v, data.currency), { swatch: 'ch-g-' + id }));
    }
    if (rsiOn()) {
      const v = maps.rsi14.get(p.t);
      if (v != null) node.append(tipRow('RSI 14', fmtNum(v, 1), { swatch: 'ch-g-rsi' }));
    }
    for (const m of markersAt.get(i) || []) {
      node.append(el('div', { class: 'ch-tip-marker' }, el('span', { class: 'ch-glyph ch-g-' + m.kind, 'aria-hidden': 'true' }), el('span', { text: m.text })));
    }
  }

  function renderLegend() {
    const key = shell.key;
    clear(key);
    if (!hasPts()) return;
    const kinds = new Set(markers.map(m => m.kind));
    const labels = { buy: 'Buy analysis', sell: 'Sell analysis', hold: 'Hold analysis' };
    for (const k of ['buy', 'sell', 'hold']) if (kinds.has(k)) key.append(keyItem('ch-g-' + k, labels[k]));
    if (overlays.ema20) key.append(keyItem('ch-g-ema20 ch-g-line', 'EMA 20', maps.ema20.size ? null : el('span', { class: 'muted', text: '(no data)' })));
    if (overlays.ema50) key.append(keyItem('ch-g-ema50 ch-g-line', 'EMA 50', maps.ema50.size ? null : el('span', { class: 'muted', text: '(no data)' })));
    if (overlays.rsi14) key.append(keyItem('ch-g-rsi ch-g-line', 'RSI 14', maps.rsi14.size ? null : el('span', { class: 'muted', text: '(no data)' })));
  }

  function updateAria() {
    if (!hasPts()) {
      shell.setAria(`${(data && data.label) || 'Price'} chart: no data`);
      return;
    }
    const pts = data.points;
    let hi = -Infinity;
    let lo = Infinity;
    for (const p of pts) { if (p.c > hi) hi = p.c; if (p.c < lo) lo = p.c; }
    const first = pts[0].c;
    const last = pts[pts.length - 1].c;
    const chg = first ? ((last - first) / first) * 100 : null;
    shell.setAria(`${data.label || 'Price'} price, ${RANGE_NAMES[data.range] || data.range}: last ${fmtPrice(last, data.currency)}, `
      + `high ${fmtPrice(hi, data.currency)}, low ${fmtPrice(lo, data.currency)}, change ${fmtPct(chg)}, ${pts.length} data points. `
      + 'Focus and use arrow keys to inspect values.');
  }

  function applyState() {
    const eff = state || (data && !hasPts() ? { empty: 'No chart data available right now.' } : null);
    shell.setState(eff, hasPts());
  }

  // The intro animation plays once per fresh dataset, as soon as nothing opaque covers the chart.
  let intro = false;

  function flush() {
    const blocked = state && (state.error || state.empty);
    if (intro && hasPts() && !blocked) {
      intro = false;
      if (!reducedMotion()) { shell.animate(); return; }
    }
    shell.redraw();
  }

  function paint(fresh) {
    if (fresh) intro = true;
    syncRsiVisibility();
    renderLegend();
    updateAria();
    applyState();
    flush();
  }

  return {
    setData(d) {
      const prev = data;
      const pts = ((d && d.points) || []).filter(p => p && isNum(p.t) && isNum(p.c));
      const range = (d && d.range) || '1M';
      const key = `${(d && d.label) || ''}|${range}`;
      const fresh = !prev || prev.key !== key || prev.points.length < 2;
      data = {
        points: pts, range, key,
        currency: (d && d.currency) || 'EUR',
        label: (d && d.label) || '',
        daily: medianStep(pts) >= 20 * 3600,
      };
      const ind = d && d.indicators && Array.isArray(d.indicators.t) ? d.indicators : null;
      maps = {
        ema20: ind ? buildSeriesMap(ind.t, ind.ema && ind.ema['20']) : new Map(),
        ema50: ind ? buildSeriesMap(ind.t, ind.ema && ind.ema['50']) : new Map(),
        rsi14: ind ? buildSeriesMap(ind.t, ind.rsi && ind.rsi['14']) : new Map(),
      };
      markers = [];
      markersAt = new Map();
      if (pts.length >= 2) {
        const t0 = pts[0].t - DAY;
        const t1 = pts[pts.length - 1].t + 3600;
        for (const m of (d && d.markers) || []) {
          const ts = Date.parse(String(m && m.date) + 'T00:00:00Z') / 1000;
          if (!isNum(ts) || ts < t0 || ts > t1) continue;
          const idx = nearestIndexByTime(pts, ts);
          const kind = markerKind(m);
          const word = String(m.decision || m.action || kind).toUpperCase();
          const entry = { idx, kind, text: `Analysis ${fmtDate(ts)}: ${word}${isNum(m.score) ? ` (score ${fmtNum(m.score, 1)})` : ''}` };
          markers.push(entry);
          if (!markersAt.has(idx)) markersAt.set(idx, []);
          markersAt.get(idx).push(entry);
        }
      }
      shell.resetHover();
      paint(fresh);
    },
    setOverlays(o) {
      overlays = { ema20: !!(o && o.ema20), ema50: !!(o && o.ema50), rsi14: !!(o && o.rsi14) };
      paint(false);
    },
    setState(s) {
      state = s || null;
      applyState();
      if (!state) flush();
    },
    redraw: () => shell.redraw(),
    destroy: () => shell.destroy(),
  };
}

// ------------------------------------------------------------------ indexed comparison chart

export function createIndexedChart(host) {
  const canvas = el('canvas', { class: 'ch-canvas ch-canvas-indexed', 'aria-hidden': 'true' });
  let d = null;       // {rows, t0, t1, pl, bl, hasB, lastP, lastB}
  let state = null;
  let view = null;

  const hasRows = () => !!(d && d.rows.length >= 2);

  const shell = createShell(host, {
    cls: 'ch-indexed',
    ariaLabel: 'Indexed performance chart',
    canvases: [canvas],
    hooks: {
      count: () => (d ? d.rows.length : 0),
      indexAt(x) {
        if (!view) return null;
        const f = clamp((x - view.pr.x) / view.pr.w, 0, 1);
        return nearestIndexByTime(d.rows, d.t0 + f * (d.t1 - d.t0));
      },
      xOf: (i) => (view ? view.xAt(i) : 0),
      draw,
      drawCross,
      renderTip,
      announce(i) {
        const r = d.rows[i];
        return `${fmtDate(r.t)}, ${d.pl} ${fmtPct(r.p, { dec: 1 })}, ${d.bl} ${fmtPct(r.b, { dec: 1 })}`;
      },
    },
  });

  function draw() {
    view = null;
    const c = prep(canvas);
    if (!c || !hasRows()) return;
    const { ctx, w, h } = c;
    if (w < 40 || h < 40) return;
    const col = chartColors();
    const rows = d.rows;
    let min = 0;
    let max = 0;
    for (const r of rows) {
      for (const v of [r.p, r.b]) {
        if (v == null) continue;
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
    const pad = (max - min) * 0.1 || 1;
    const lo = min - pad;
    const hi = max + pad;
    const { ticks } = niceTicks(lo, hi, 4);
    ctx.font = `11px ${col.font}`;
    let lane = 40;
    for (const v of ticks) lane = Math.max(lane, ctx.measureText(axisPct(v)).width + 14);
    const padR = Math.min(84, Math.ceil(lane));
    const pr = { x: PAD_L, y: PAD_T, w: Math.max(10, w - PAD_L - padR), h: Math.max(10, h - PAD_T - PAD_B) };
    const span = d.t1 - d.t0 || 1;
    const xAt = (i) => pr.x + ((rows[i].t - d.t0) / span) * pr.w;
    const xT = (t) => pr.x + ((t - d.t0) / span) * pr.w;
    const yAt = (v) => pr.y + pr.h - ((v - lo) / (hi - lo)) * pr.h;
    view = { pr, xAt, yAt, w, h };

    ctx.lineWidth = 1;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (const v of ticks) {
      const y = Math.round(yAt(v)) + 0.5;
      ctx.strokeStyle = col.grid;
      ctx.beginPath();
      ctx.moveTo(pr.x, y);
      ctx.lineTo(pr.x + pr.w, y);
      ctx.stroke();
      ctx.fillStyle = col.label;
      ctx.fillText(axisPct(v), pr.x + pr.w + 8, y);
    }
    // date axis
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const dt = dateTicks(d.t0, d.t1, Math.max(2, Math.floor(pr.w / 72)));
    for (const t of dt) {
      const x = xT(t.t);
      ctx.strokeStyle = col.grid;
      ctx.beginPath();
      ctx.moveTo(x, pr.y);
      ctx.lineTo(x, pr.y + pr.h);
      ctx.stroke();
      ctx.fillStyle = col.label;
      ctx.fillText(t.label, clamp(x, pr.x + 16, pr.x + pr.w - 16), pr.y + pr.h + 8);
    }
    // zero baseline
    const zy = Math.round(yAt(0)) + 0.5;
    ctx.strokeStyle = withAlpha(col.axis, 0.7);
    ctx.beginPath();
    ctx.moveTo(pr.x, zy);
    ctx.lineTo(pr.x + pr.w, zy);
    ctx.stroke();

    const line = (key, color, width, dash) => {
      ctx.beginPath();
      let started = false;
      let last = null;
      for (let i = 0; i < rows.length; i++) {
        const v = rows[i][key];
        if (v == null) { started = false; continue; }
        const x = xAt(i);
        const y = yAt(v);
        if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
        last = { x, y };
      }
      ctx.setLineDash(dash);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineJoin = 'round';
      ctx.stroke();
      ctx.setLineDash([]);
      if (last) {
        ctx.beginPath();
        ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      }
    };
    if (d.hasB) line('b', col.benchmark, 1.5, [5, 4]);
    line('p', col.line, 2, []);
  }

  function axisPct(v) {
    return v === 0 ? '0.0%' : fmtPct(v, { dec: 1 });
  }

  function drawCross(ctx, w, h, i) {
    if (!view || !d || i >= d.rows.length) return;
    const col = chartColors();
    const r = d.rows[i];
    const x = view.xAt(i);
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = withAlpha(col.ink2, 0.55);
    ctx.beginPath();
    ctx.moveTo(x + 0.5, view.pr.y);
    ctx.lineTo(x + 0.5, view.pr.y + view.pr.h);
    ctx.stroke();
    ctx.setLineDash([]);
    for (const [v, color] of [[r.b, col.benchmark], [r.p, col.line]]) {
      if (v == null) continue;
      ctx.beginPath();
      ctx.arc(x, view.yAt(v), 4, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = col.panel;
      ctx.stroke();
    }
  }

  function renderTip(node, i) {
    const r = d.rows[i];
    node.append(el('div', { class: 'ch-tip-time', text: fmtDate(r.t) }));
    node.append(tipRow(d.pl, fmtPct(r.p, { dec: 1 }), { cls: dir(r.p), swatch: 'ch-g-line ch-g-portfolio' }));
    if (d.hasB) node.append(tipRow(d.bl, fmtPct(r.b, { dec: 1 }), { cls: dir(r.b), swatch: 'ch-g-line ch-g-bench' }));
    if (r.p != null && r.b != null) {
      const diff = r.p - r.b;
      node.append(tipRow('Difference', fmtSigned(diff, 1, ' pp'), { cls: dir(diff) }));
    }
  }

  function renderLegend() {
    clear(shell.key);
    if (!hasRows()) return;
    const val = (v) => el('span', { class: 'ch-key-val num ' + dir(v), text: fmtPct(v, { dec: 1 }) });
    shell.key.append(keyItem('ch-g-line ch-g-portfolio', d.pl, val(d.lastP)));
    if (d.hasB) shell.key.append(keyItem('ch-g-line ch-g-bench', d.bl, val(d.lastB)));
  }

  function updateAria() {
    if (!hasRows()) { shell.setAria('Indexed performance chart: no data'); return; }
    let s = `${d.pl} versus ${d.bl}, indexed return from ${fmtDate(d.t0)} to ${fmtDate(d.t1)}: ${d.pl} ${fmtPct(d.lastP, { dec: 1 })}`;
    if (d.hasB) {
      s += `, ${d.bl} ${fmtPct(d.lastB, { dec: 1 })}`;
      if (isNum(d.lastP) && isNum(d.lastB)) s += `, difference ${fmtSigned(d.lastP - d.lastB, 1, ' percentage points')}`;
    }
    shell.setAria(s + '. Focus and use arrow keys to inspect values.');
  }

  function applyState() {
    const eff = state || (d && !hasRows() ? { empty: 'No comparison data available right now.' } : null);
    shell.setState(eff, hasRows());
  }

  return {
    setData(x) {
      const rows = mergeIndexed(x && x.portfolio, x && x.benchmark);
      const lastOf = (key) => {
        for (let i = rows.length - 1; i >= 0; i--) if (rows[i][key] != null) return rows[i][key];
        return null;
      };
      d = {
        rows,
        t0: rows.length ? rows[0].t : 0,
        t1: rows.length ? rows[rows.length - 1].t : 0,
        pl: (x && x.portfolioLabel) || 'Portfolio',
        bl: (x && x.benchmarkLabel) || 'S&P 500',
        hasB: rows.some(r => r.b != null),
        lastP: lastOf('p'),
        lastB: lastOf('b'),
      };
      shell.resetHover();
      renderLegend();
      updateAria();
      applyState();
      shell.redraw();
    },
    setState(s) {
      state = s || null;
      applyState();
      if (!state) shell.redraw();
    },
    redraw: () => shell.redraw(),
    destroy: () => shell.destroy(),
  };
}

// ------------------------------------------------------------------ allocation donut

export function createDonut(host, { onSelect } = {}) {
  const R = 15.9155;
  const slots = new Map();   // id -> colour slot
  const rows = new Map();    // id -> {seg, li, btn, name, pct, val}
  const track = svg('circle', { class: 'ch-donut-track', cx: 21, cy: 21, r: R });
  const ring = svg('g', { transform: 'rotate(-90 21 21)' }, track);
  const centerMain = svg('text', { class: 'ch-donut-main', x: 21, y: 21.2 });
  const centerSub = svg('text', { class: 'ch-donut-sub', x: 21, y: 26 });
  const graphic = svg('svg', { class: 'ch-donut-svg', viewBox: '0 0 42 42', role: 'img', 'aria-label': 'Allocation by weight' }, ring, centerMain, centerSub);
  const list = el('ul', { class: 'ch-alloc' });
  const root = el('div', { class: 'ch-donut' }, el('div', { class: 'ch-donut-fig' }, graphic), list);
  host.append(root);

  function slotFor(id) {
    let s = slots.get(id);
    if (s == null) {
      const used = new Set(slots.values());
      s = 0;
      while (used.has(s)) s++;
      slots.set(id, s);
    }
    return s;
  }

  function makeRow(id) {
    const seg = svg('circle', { class: 'ch-seg', cx: 21, cy: 21, r: R, pathLength: 100, 'data-id': id, onclick: () => select(id) });
    const name = el('span', { class: 'ch-alloc-name' });
    const pct = el('span', { class: 'ch-alloc-pct num' });
    const val = el('span', { class: 'ch-alloc-val num' });
    const sw = el('span', { class: 'ch-swatch', 'aria-hidden': 'true' });
    const btn = el('button', { type: 'button', class: 'ch-alloc-btn', 'data-id': id, onclick: () => select(id) }, sw, name, pct, val);
    const li = el('li', { class: 'ch-alloc-item', 'data-id': id }, btn);
    return { seg, li, btn, sw, name, pct, val };
  }

  function select(id) {
    if (onSelect) onSelect(id);
  }

  return {
    update(items, selectedId = null) {
      const listed = (items || []).filter(it => it && it.id != null && isNum(it.weightPct) && it.weightPct > 0)
        .sort((a, b) => b.weightPct - a.weightPct);
      const keep = new Set(listed.map(it => String(it.id)));
      for (const [id, r] of [...rows]) {
        if (keep.has(id)) continue;
        r.seg.remove();
        r.li.remove();
        rows.delete(id);
        slots.delete(id);
      }
      const total = listed.reduce((s, it) => s + it.weightPct, 0) || 100;
      const gap = listed.length > 1 ? 0.6 : 0;
      let acc = 0;
      let totalEur = 0;
      let selected = null;
      for (const it of listed) {
        const id = String(it.id);
        let r = rows.get(id);
        if (!r) { r = makeRow(id); rows.set(id, r); }
        const slot = (slotFor(id) % 6) + 1;
        const share = (it.weightPct / total) * 100;
        const isSel = selectedId != null && String(selectedId) === id;
        if (isSel) selected = it;
        if (isNum(it.valueEur)) totalEur += it.valueEur;
        r.seg.setAttribute('class', `ch-seg ch-a${slot}${isSel ? ' is-selected' : ''}`);
        r.seg.setAttribute('stroke-dasharray', `${Math.max(0.1, share - gap).toFixed(3)} ${(100 - Math.max(0.1, share - gap)).toFixed(3)}`);
        r.seg.setAttribute('stroke-dashoffset', ((((100 - acc - gap / 2) % 100) + 100) % 100).toFixed(3));
        acc += share;
        r.sw.className = `ch-swatch ch-a${slot}`;
        r.btn.setAttribute('aria-pressed', isSel ? 'true' : 'false');
        r.name.textContent = it.label || id;
        r.pct.textContent = fmtPct(it.weightPct, { sign: false, dec: 1 });
        r.val.textContent = fmtEur(it.valueEur);
        ring.append(r.seg);
        list.append(r.li);
      }
      root.classList.toggle('has-sel', !!selected);
      if (selected) {
        const l = String(selected.label || selected.id);
        centerMain.textContent = l.length > 9 ? l.slice(0, 8) + '\u2026' : l;
        centerSub.textContent = fmtPct(selected.weightPct, { sign: false, dec: 1 });
      } else if (listed.length) {
        centerMain.textContent = fmtEur(totalEur, { dec: 0 });
        centerSub.textContent = 'total';
      } else {
        centerMain.textContent = '';
        centerSub.textContent = '';
      }
      graphic.setAttribute('aria-label', listed.length
        ? 'Allocation by weight: ' + listed.map(it => `${it.label || it.id} ${fmtPct(it.weightPct, { sign: false, dec: 1 })}`).join(', ')
        : 'Allocation by weight: no data');
    },
    destroy() {
      root.remove();
      rows.clear();
      slots.clear();
    },
  };
}

export const charts = { chartColors, drawSparkline, createPriceChart, createDonut, createIndexedChart };
