// Holding detail pane: header/price, price chart, AI analysis, position, valuation + signals,
// news. Also owns the TradingView advanced-chart modal and the report viewer modal.
//
//   const pane = mountDetail(root, { onClose });   pane.show(id|null);   pane.destroy();
//   openReportModal(rep);   openAdvancedChart(id);   tvSymbolFor(entry);
//
// The pane is self-contained: it may be re-parented (dock <-> drawer) at any time. Every request
// of the previous id is aborted on show(otherId); responses that still slip through are dropped
// by comparing the per-id state object `S` captured before each await.
import {
  el, clear, fmtEur, fmtPct, fmtNum, fmtTime24, fmtDateTime24, signCls, isNum, safeUrl,
} from '../util.js';
import { apiGet, apiSend, latestRequest } from '../api.js';
import * as store from '../store.js';
import {
  openDialog, confirmDialog, toast, renderState, stamp, usePolling, setMarkdown,
} from '../ui.js';
import { entryOf, labelOf, positionOf, priceItemOf, refreshPortfolio } from '../data.js';
import { createPriceChart } from '../charts.js';

// ------------------------------------------------------------------ constants

const RANGES = ['1D', '1W', '1M', '3M', '1Y'];
const MODES = ['quick', 'standard', 'deep'];
const JOB_POLL_MS = 5000;
const JOB_POLL_MAX = 600; // 5 s x 600 = 50 min
const JOB_FAIL_MAX = 5;
const CHART_REFRESH_MS = 60000;
const NEWS_REFRESH_MS = 300000;
const HIST_VISIBLE = 6;
const TV_SCRIPT_URL = 'https://s3.tradingview.com/tv.js';
const TV_TIMEOUT_MS = 10000;

// ------------------------------------------------------------------ TradingView symbol map

// MIC -> TradingView exchange prefix.
const TV_EXCHANGE = {
  XAMS: 'EURONEXT', XPAR: 'EURONEXT', XBRU: 'EURONEXT', XLIS: 'EURONEXT',
  XETR: 'XETR', XMIL: 'MIL', XNAS: 'NASDAQ', XNYS: 'NYSE', XLON: 'LSE', XSWX: 'SIX', XMAD: 'BME',
};
const TV_US_VENUES = new Set(['XNAS', 'XNYS']);

/**
 * TradingView symbol for a watchlist entry ({id,symbol,kind,listing:{symbol,venue}}).
 *   ASML (XAMS, ASML.AS) -> EURONEXT:ASML     SXR8 (XETR, SXR8.DE) -> XETR:SXR8
 *   NVDA (XNAS)          -> NASDAQ:NVDA       GSPC / ^GSPC / index -> SP:SPX
 * Unknown venue -> the plain listing symbol. No entry -> ''.
 */
export function tvSymbolFor(entry) {
  if (!entry || typeof entry !== 'object') return '';
  const listing = entry.listing && typeof entry.listing === 'object' ? entry.listing : {};
  const raw = String(listing.symbol || entry.symbol || entry.id || '').trim();
  const bare = raw.replace(/^\^/, '').toUpperCase();
  const id = String(entry.id || '').toUpperCase();
  if (bare === 'GSPC' || bare === 'SPX' || id === 'GSPC') return 'SP:SPX';
  if (entry.kind === 'index') return raw.replace(/^\^/, '');
  const venue = String(listing.venue || '').toUpperCase();
  const exchange = TV_EXCHANGE[venue];
  if (!exchange) return raw;
  // Yahoo exchange suffix (.AS/.DE/.PA ...) is not part of the TradingView ticker.
  const sym = TV_US_VENUES.has(venue) ? raw : raw.replace(/\.[A-Za-z]{1,3}$/, '');
  return exchange + ':' + sym;
}

function tvUrlFor(symbol) {
  return 'https://www.tradingview.com/chart/?symbol=' + encodeURIComponent(symbol);
}

// ------------------------------------------------------------------ small helpers

const CUR_SYM = { EUR: '\u20ac', USD: '$', GBP: '\u00a3' };

function fmtMoney(n, cur) {
  if (!isNum(n)) return '\u2013';
  if (!cur || cur === 'EUR') return fmtEur(n);
  const sym = CUR_SYM[cur] || cur + '\u00a0';
  return (n < 0 ? '\u2212' : '') + sym + fmtNum(Math.abs(n));
}

function fmtShares(n) {
  if (!isNum(n)) return '\u2013';
  return Number.isInteger(n) ? fmtNum(n, 0) : fmtNum(n, 4).replace(/0+$/, '').replace(/\.$/, '');
}

function numOrNull(v) {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function cap(s) {
  s = String(s || '');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

function humanize(key) {
  return cap(String(key).replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim());
}

function setPressed(btn, on) {
  btn.setAttribute('aria-pressed', String(!!on));
}

function setContent(body, ...nodes) {
  clear(body);
  body.removeAttribute('aria-busy');
  body.append(...nodes.filter(Boolean));
}

function chip(text, cls = '', title = '') {
  return el('span', { class: 'chip' + (cls ? ' ' + cls : ''), text, title: title || null });
}

function decisionChip(decision) {
  const d = String(decision || '').toUpperCase();
  const cls = d === 'BUY' ? 'chip-ok' : d === 'SELL' ? 'chip-err' : d === 'HOLD' ? 'chip-warn' : '';
  return el('span', { class: 'chip dt-decision ' + cls, text: d || '\u2013' });
}

/** <dl class="kv"> from [[label, value(string|Node), cls?]]. */
function kvList(rows) {
  const dl = el('dl', { class: 'kv dt-kv' });
  for (const [label, value, cls] of rows) {
    dl.append(el('div', null, el('dt', { text: label }), el('dd', { class: cls || null }, value)));
  }
  return dl;
}

function laneText(src) {
  if (!src) return '';
  const lane = src.lane ? String(src.lane) : '';
  const model = src.model ? String(src.model) : '';
  if (!lane && !model) return '';
  const fell = src.fallback || (Array.isArray(src.lane_fallbacks) && src.lane_fallbacks.length);
  return [lane, model].filter(Boolean).join(' \u00b7 ') + (fell ? ' (fell back to another lane)' : '');
}

function reportUrl(id) {
  return '/api/report/' + encodeURIComponent(String(id));
}

// ------------------------------------------------------------------ report markdown extraction

function extractDecision(md) {
  const m = /\brating\b[\s:*_]*\b(BUY|SELL|HOLD)\b/i.exec(md || '');
  return m ? m[1].toUpperCase() : null;
}

function extractPriceTarget(md) {
  const m = /price\s*target[\s:*_]*[$\u20ac]?\s*([\d.,]+\d)/i.exec(md || '');
  return m ? m[1].replace(/,/g, '') : null;
}

function extractTimeHorizon(md) {
  const m = /time\s*horizon[\s:*_]*([^\n]+)/i.exec(md || '');
  return m ? m[1].replace(/[*_]+/g, '').trim() || null : null;
}

/** Body of "## heading" up to the next heading of the same or higher level. */
function extractSection(md, heading, max = 1200) {
  if (!md) return null;
  const lines = md.split('\n');
  const want = heading.toLowerCase();
  let start = -1;
  let level = 2;
  for (let i = 0; i < lines.length; i++) {
    const m = /^(#{1,4})\s+(.*?)\s*$/.exec(lines[i]);
    if (m && m[2].toLowerCase().startsWith(want)) { start = i; level = m[1].length; break; }
  }
  if (start < 0) return null;
  const out = [];
  for (let i = start + 1; i < lines.length; i++) {
    const m = /^(#{1,4})\s/.exec(lines[i]);
    if (m && m[1].length <= level) break;
    out.push(lines[i]);
  }
  let text = out.join('\n').trim();
  if (text.length > max) text = text.slice(0, max).replace(/\s+\S*$/, '') + '\n\n\u2026';
  return text || null;
}

function headingSet(md) {
  const set = new Set();
  for (const line of String(md || '').split('\n')) {
    const m = /^#{1,4}\s+(.*?)\s*$/.exec(line);
    if (m) set.add(m[1].toLowerCase());
  }
  return set;
}

// Keys of the /api/report payload that are metadata, not report sections.
const REPORT_META_KEYS = new Set([
  'content', 'error', 'id', 'date', 'meta', 'lane', 'model', 'mode', 'source', 'generated_at',
  'generatedAt', 'trade_date', 'company_of_interest', 'lane_fallbacks', 'lane_fallback',
  'price_at_analysis', 'decision', 'score', 'action', 'summary', 'ticker', 'sections', 'status',
]);

function valueToMarkdown(v, depth = 0) {
  if (v == null || v === '') return '';
  if (typeof v === 'string') return v.trim();
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (depth > 3) return '';
  if (Array.isArray(v)) {
    return v.map(x => {
      const t = valueToMarkdown(x, depth + 1);
      return t ? '- ' + t.replace(/\n/g, '\n  ') : '';
    }).filter(Boolean).join('\n');
  }
  if (typeof v === 'object') {
    return Object.entries(v).map(([k, x]) => {
      const t = valueToMarkdown(x, depth + 1);
      if (!t) return '';
      return t.includes('\n') ? `**${humanize(k)}**\n\n${t}` : `**${humanize(k)}:** ${t}`;
    }).filter(Boolean).join('\n\n');
  }
  return '';
}

/** Extra sections a report payload carries beyond `content` (debate, risk, ...). */
function payloadSections(p, md) {
  const seen = headingSet(md);
  const out = [];
  const add = (title, body) => {
    const t = valueToMarkdown(body);
    if (!title || !t || seen.has(String(title).toLowerCase())) return;
    out.push({ title: String(title), md: t });
  };
  if (Array.isArray(p.sections)) {
    for (const s of p.sections) {
      if (s && typeof s === 'object') add(s.title || s.heading || s.name, s.content ?? s.markdown ?? s.text ?? s.body);
    }
  }
  for (const [k, v] of Object.entries(p)) {
    if (!REPORT_META_KEYS.has(k)) add(humanize(k), v);
  }
  return out;
}

function reportMeta(p, rep) {
  const m = p && typeof p.meta === 'object' && p.meta ? p.meta : {};
  const pick = k => (p && p[k] != null ? p[k] : m[k] != null ? m[k] : (rep && rep[k] != null ? rep[k] : null));
  return {
    lane: pick('lane'),
    model: pick('model'),
    lane_fallbacks: pick('lane_fallbacks'),
    fallback: !!pick('lane_fallback'),
    mode: pick('mode'),
  };
}

// ------------------------------------------------------------------ report viewer modal

function reportHeadNodes(rep, payload) {
  const md = payload && typeof payload.content === 'string' ? payload.content : '';
  const decision = rep.decision || (payload && payload.decision) || extractDecision(md);
  const meta = reportMeta(payload, rep);
  const nodes = [];
  if (decision) nodes.push(decisionChip(decision));
  if (rep.date) nodes.push(chip(String(rep.date)));
  if (meta.mode) nodes.push(chip(cap(meta.mode)));
  if (isNum(rep.score)) nodes.push(chip(`score ${Math.round(rep.score)}/100`));
  if (rep.action) nodes.push(chip(String(rep.action)));
  const lt = laneText(meta);
  return { nodes, lane: lt };
}

function paintReportHead(head, rep, payload) {
  const { nodes, lane } = reportHeadNodes(rep, payload);
  clear(head);
  head.append(...nodes);
  if (lane) head.append(el('span', { class: 'dt-lane muted', text: 'Model: ' + lane }));
}

function paintReportBody(container, payload) {
  const md = typeof payload.content === 'string' ? payload.content : '';
  const extras = payloadSections(payload, md);
  if (!md.trim() && !extras.length) {
    renderState(container, { error: 'The report has no content.' });
    return;
  }
  clear(container);
  container.removeAttribute('aria-busy');
  if (md.trim()) {
    const box = el('article', { class: 'md dt-md' });
    setMarkdown(box, md);
    container.append(box);
  }
  for (const s of extras) {
    const box = el('div', { class: 'md dt-md' });
    setMarkdown(box, s.md);
    container.append(el('details', { class: 'dt-sec', open: true },
      el('summary', { text: s.title }), box));
  }
}

/** Open the report viewer for a ticker-reports row ({id,date,decision,mode,score,action,summary}). */
export function openReportModal(rep) {
  if (!rep || rep.id == null || rep.id === '') return null;
  const ctl = new AbortController();
  const head = el('div', { class: 'dt-report-head' });
  const content = el('div', { class: 'dt-report-body' });
  const body = el('div', { class: 'dt-report' }, head, content);
  const dlg = openDialog({
    title: 'Analysis report' + (rep.date ? ' \u00b7 ' + rep.date : ''),
    kind: 'modal', size: 'lg', body, onClose: () => ctl.abort(),
  });
  paintReportHead(head, rep, null);
  async function load() {
    renderState(content, { loading: 'Loading report\u2026' });
    const res = await apiGet(reportUrl(rep.id), { signal: ctl.signal });
    if (ctl.signal.aborted || res.aborted) return;
    const d = res.data;
    if (!res.ok || !d || typeof d !== 'object' || d.error) {
      renderState(content, { error: (d && d.error) || res.error || 'Report unavailable', retry: load });
      return;
    }
    paintReportHead(head, rep, d);
    paintReportBody(content, d);
  }
  load();
  return dlg;
}

// ------------------------------------------------------------------ news modal

function openNewsModal(item) {
  const box = el('div', { class: 'dt-news-modal' });
  if (item.body && item.body !== item.title) box.append(el('p', { class: 'dt-news-text', text: String(item.body) }));
  const bits = [String(item.kind || 'alert').toUpperCase()];
  bits.push('source: ' + (item.source || 'unknown'));
  if (item.ts) bits.push(fmtDateTime24(item.ts));
  if (item.ticker) bits.push(String(item.ticker));
  box.append(el('p', { class: 'dt-news-meta muted', text: bits.join(' \u00b7 ') }));
  const href = safeUrl(item.url);
  if (href && /^https?:/.test(href)) {
    box.append(el('a', { class: 'dt-ext-link', href, target: '_blank', rel: 'noopener noreferrer', text: 'Open source \u2197' }));
  } else if (item.url) {
    box.append(el('p', { class: 'dt-news-meta muted', text: String(item.url) }));
  }
  return openDialog({ title: item.title || cap(item.kind) || 'Alert', kind: 'modal', size: 'md', body: box });
}

// ------------------------------------------------------------------ TradingView modal

let tvScriptPromise = null;

function loadTvScript() {
  if (window.TradingView && window.TradingView.widget) return Promise.resolve();
  if (!tvScriptPromise) {
    tvScriptPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = TV_SCRIPT_URL;
      s.async = true;
      const fail = msg => { clearTimeout(timer); tvScriptPromise = null; s.remove(); reject(new Error(msg)); };
      const timer = setTimeout(() => fail('The TradingView script did not load in time.'), TV_TIMEOUT_MS);
      s.onload = () => { clearTimeout(timer); resolve(); };
      s.onerror = () => fail('The TradingView script could not be loaded (offline or blocked).');
      document.head.append(s);
    });
  }
  return tvScriptPromise;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Open the TradingView advanced-chart modal for a holding id. */
export function openAdvancedChart(id) {
  const entry = entryOf(id);
  const symbol = tvSymbolFor(entry) || String(id || '');
  const tvUrl = tvUrlFor(symbol);
  const holder = el('div', { class: 'dt-tv-holder', id: 'dt-tv-' + Math.random().toString(36).slice(2, 8) });
  const status = el('div', { class: 'dt-tv-status', hidden: true });
  const foot = el('div', { class: 'dt-tv-foot muted' },
    el('span', { text: symbol }),
    el('a', { class: 'dt-ext-link', href: tvUrl, target: '_blank', rel: 'noopener noreferrer', text: 'Open on tradingview.com \u2197' }));
  const body = el('div', { class: 'dt-tv' }, el('div', { class: 'dt-tv-stage' }, holder, status), foot);

  let gen = 0;
  let widget = null;
  let timer = null;
  let closed = false;

  function showStatus(opts) {
    if (!opts) { status.hidden = true; clear(status); return; }
    status.hidden = false;
    renderState(status, opts);
    if (opts.error) {
      status.append(el('div', { class: 'dt-tv-actions' },
        el('a', { class: 'btn btn-sm', href: tvUrl, target: '_blank', rel: 'noopener noreferrer', text: 'Open on tradingview.com' }),
        el('button', { class: 'btn btn-sm', type: 'button', text: 'Retry', onclick: () => build() })));
    }
  }

  function teardown() {
    clearTimeout(timer);
    timer = null;
    if (widget && typeof widget.remove === 'function') {
      try { widget.remove(); } catch (_) { /* already torn down */ }
    }
    widget = null;
    clear(holder);
  }

  async function build() {
    const my = ++gen;
    teardown();
    showStatus({ loading: 'Loading TradingView chart\u2026' });
    try {
      await loadTvScript();
    } catch (e) {
      if (my === gen && !closed) showStatus({ error: e.message });
      return;
    }
    if (my !== gen || closed) return;
    try {
      if (!window.TradingView || typeof window.TradingView.widget !== 'function') throw new Error('TradingView.widget is unavailable.');
      const dark = store.get('theme') !== 'light';
      const bg = cssVar('--panel');
      widget = new window.TradingView.widget({
        container_id: holder.id,
        symbol,
        // Free embed serves intraday only for US/index symbols; XETR/Euronext etc. are D/W/M only.
        interval: /^(NASDAQ|NYSE|SP):/.test(symbol) ? '60' : 'D',
        theme: dark ? 'dark' : 'light',
        style: '1',
        locale: 'en',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Etc/UTC',
        toolbar_bg: bg || undefined,
        enable_publishing: false,
        hide_side_toolbar: false,
        allow_symbol_change: true,
        save_image: false,
        autosize: true,
      });
    } catch (e) {
      teardown();
      showStatus({ error: e.message || 'The TradingView widget failed to start.' });
      return;
    }
    // The chart is "ready" via onChartReady; otherwise the iframe's own load event. If neither
    // happens in time show the error state - but keep the widget so a late load still wins.
    const ready = () => {
      if (my !== gen || closed) return;
      clearTimeout(timer);
      timer = null;
      showStatus(null);
    };
    timer = setTimeout(() => {
      if (my === gen && !closed) showStatus({ error: 'The chart did not respond within 10 seconds.' });
    }, TV_TIMEOUT_MS);
    if (typeof widget.onChartReady === 'function') {
      try { widget.onChartReady(ready); } catch (_) { /* fall through to timeout */ }
    } else {
      const watch = setInterval(() => {
        const f = holder.querySelector('iframe');
        if (my !== gen || closed) { clearInterval(watch); return; }
        if (f) { clearInterval(watch); f.addEventListener('load', ready, { once: true }); }
      }, 200);
    }
  }

  const unsubTheme = store.subscribe('theme', () => { if (!closed) build(); });
  openDialog({
    title: `Advanced chart: ${labelOf(id)} \u00b7 ${symbol}`,
    kind: 'modal', size: 'xl', body,
    onClose: () => { closed = true; gen++; unsubTheme(); teardown(); },
  });
  build();
}

// ------------------------------------------------------------------ the pane

let uidCounter = 0;

/** Mount the detail pane into `root`. Returns {show(id|null), destroy()}. */
export function mountDetail(root, { onClose } = {}) {
  const uid = 'dt' + (++uidCounter) + '-';
  const prefs = {
    range: '1M',
    overlays: { ema20: false, ema50: false, rsi14: false },
    mode: 'standard',
  };
  const lr = {
    hist: latestRequest(), markers: latestRequest(), ind: latestRequest(), ticker: latestRequest(),
    news: latestRequest(), reports: latestRequest(), jobs: latestRequest(), job: latestRequest(),
    result: latestRequest(),
  };
  const unsubs = [];
  let S = null;            // per-id state; replaced on every id change
  let destroyed = false;
  let chart = null;
  let stopChartPoll = null;
  let stopNewsPoll = null;
  let stopJobPoll = null;

  // ---- DOM ----------------------------------------------------------------

  function card(key, title, ...tools) {
    const titleId = `${uid}${key}-t`;
    const stampEl = stamp(null);
    const body = el('div', { class: 'dt-body' });
    const toolsEl = el('div', { class: 'card-tools' }, ...tools, stampEl);
    const node = el('section', { class: `card dt-card dt-${key}`, 'aria-labelledby': titleId },
      el('div', { class: 'card-head' }, el('h3', { class: 'card-title', id: titleId, text: title }), toolsEl),
      body);
    return { node, body, tools: toolsEl, stamp: stampEl };
  }

  const dom = {};

  // header
  dom.title = el('h2', { class: 'dt-title', tabindex: '-1' });
  dom.closeBtn = el('button', {
    class: 'icon-btn dt-close', type: 'button', 'aria-label': 'Close details', text: '\u00d7',
    onclick: () => { if (typeof onClose === 'function') onClose(); },
  });
  dom.priceBox = el('div', { class: 'dt-price-box' });
  dom.usLive = el('div', { class: 'dt-us muted', hidden: true });
  dom.headStamp = stamp(null);
  dom.head = el('header', { class: 'dt-head' },
    el('div', { class: 'dt-titlebar' }, dom.title, dom.closeBtn),
    dom.priceBox, dom.usLive, el('div', { class: 'dt-head-foot' }, dom.headStamp));

  // chart card
  dom.rangeSeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Chart range' },
    RANGES.map(r => el('button', {
      type: 'button', text: r, dataset: { range: r }, 'aria-pressed': String(r === prefs.range), onclick: () => setRange(r),
    })));
  const OVERLAYS = [['ema20', 'EMA20'], ['ema50', 'EMA50'], ['rsi14', 'RSI14']];
  dom.overlaySeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Chart overlays' },
    OVERLAYS.map(([k, label]) => el('button', {
      type: 'button', text: label, dataset: { overlay: k }, 'aria-pressed': 'false', onclick: () => toggleOverlay(k),
    })));
  dom.advBtn = el('button', { class: 'btn btn-sm', type: 'button', text: 'Advanced chart', onclick: () => S && openAdvancedChart(S.id) });
  dom.tvLink = el('a', { class: 'dt-ext-link', target: '_blank', rel: 'noopener noreferrer', text: 'Open on TradingView \u2197' });
  dom.chartRetry = el('button', { class: 'btn btn-sm', type: 'button', text: 'Retry', hidden: true, onclick: () => { loadHistory(); } });
  dom.chart = card('chart', 'Price', dom.chartRetry);
  dom.chartHost = el('div', { class: 'dt-chart-host' });
  dom.chartNote = el('div', { class: 'dt-note warn-ink', role: 'status', hidden: true });
  dom.chart.node.insertBefore(el('div', { class: 'dt-chart-tools' }, dom.rangeSeg, dom.overlaySeg, dom.advBtn, dom.tvLink), dom.chart.body);
  dom.chart.body.append(dom.chartHost, dom.chartNote);

  // AI card
  dom.ai = card('ai', 'AI analysis');
  dom.aiNote = el('p', { class: 'dt-ai-na muted', hidden: true });
  dom.modeSeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Analysis depth' },
    MODES.map(m => el('button', {
      type: 'button', text: cap(m), dataset: { mode: m }, 'aria-pressed': String(m === prefs.mode), onclick: () => setMode(m),
    })));
  dom.analyseBtn = el('button', { class: 'btn btn-primary', type: 'button', text: 'Analyse', onclick: () => triggerAnalysis() });
  dom.cancelBtn = el('button', { class: 'btn', type: 'button', text: 'Cancel', hidden: true, onclick: () => cancelJob() });
  dom.aiControls = el('div', { class: 'dt-ai-controls' }, dom.modeSeg, dom.analyseBtn, dom.cancelBtn);
  dom.aiProgress = el('div', { class: 'dt-ai-progress', role: 'status', 'aria-live': 'polite', hidden: true });
  dom.aiResult = el('div', { class: 'dt-ai-result', hidden: true });
  dom.aiHistHead = el('h4', { class: 'lbl dt-ai-hist-title', text: 'Analysis history' });
  dom.aiHist = el('div', { class: 'dt-ai-hist' });
  dom.aiLive = el('div', { class: 'dt-ai-live' }, dom.aiControls, dom.aiProgress, dom.aiResult, dom.aiHistHead, dom.aiHist);
  dom.ai.body.append(dom.aiNote, dom.aiLive);

  // position, valuation, news
  dom.pos = card('pos', 'Position');
  dom.val = card('val', 'Valuation & signals');
  dom.news = card('news', 'News & alerts');
  dom.grid = el('div', { class: 'dt-grid' }, dom.pos.node, dom.val.node);

  dom.content = el('div', { class: 'dt-content' }, dom.head, dom.chart.node, dom.ai.node, dom.grid, dom.news.node);
  dom.empty = el('div', { class: 'dt-empty state state-empty', text: 'Select a holding to see its details.' });

  root.classList.add('dt-pane');
  root.setAttribute('role', 'region');
  root.setAttribute('aria-label', 'Holding details');
  root.append(dom.content, dom.empty);
  dom.content.hidden = true;

  // chart instance (the host is part of the pane, so it survives re-parenting)
  try {
    chart = createPriceChart(dom.chartHost, {}); // the chart owns resize/theme redraws and its tooltip
  } catch (e) {
    console.error('createPriceChart failed', e);
    chart = null;
    renderState(dom.chartHost, { error: 'Chart unavailable: ' + (e && e.message ? e.message : e) });
  }
  function chartCall(name, ...args) {
    if (!chart || typeof chart[name] !== 'function') return;
    try { chart[name](...args); } catch (e) { console.error('chart.' + name + ' failed', e); }
  }
  chartCall('setOverlays', { ...prefs.overlays });

  // ---- helpers bound to S --------------------------------------------------

  const anyOverlay = () => prefs.overlays.ema20 || prefs.overlays.ema50 || prefs.overlays.rsi14;

  function newState(id) {
    return {
      id, points: null, pointsRange: null, histTs: null, histStale: false, currency: 'EUR', markers: [],
      ind: null, indError: null, job: null, jobAttempts: 0, jobFails: 0, reports: null, reportsError: null,
      reportsTs: null, showAllHist: false, lastLane: null, ticker: null, news: null,
    };
  }

  function stopPolls() {
    if (stopChartPoll) stopChartPoll();
    if (stopNewsPoll) stopNewsPoll();
    if (stopJobPoll) stopJobPoll();
    stopChartPoll = stopNewsPoll = stopJobPoll = null;
  }

  function abortAll() {
    for (const k of Object.keys(lr)) lr[k].abort();
  }

  // ---- header -------------------------------------------------------------

  function renderHeader() {
    if (!S) return;
    const id = S.id;
    const entry = entryOf(id);
    const pos = positionOf(id);
    const px = priceItemOf(id);
    const label = labelOf(id);
    dom.title.textContent = label;
    root.setAttribute('aria-label', label);

    const quoteCur = (entry && entry.quoteCurrency) || 'EUR';
    let price = null;
    if (pos && isNum(pos.priceEur)) {
      price = { v: pos.priceEur, cur: 'EUR', prev: pos.prevCloseEur, pct: pos.dayPct };
    } else if (px && isNum(px.price)) {
      price = { v: px.price, cur: quoteCur, prev: px.previousClose, pct: px.change24h };
    }
    const pSlot = store.slot('portfolio');
    const bench = pSlot.data && pSlot.data.benchmark && pSlot.data.benchmark.id === id ? pSlot.data.benchmark : null;

    const kindLabel = entry ? (entry.role === 'benchmark' ? 'Benchmark' : entry.kind === 'etf' ? 'ETF' : entry.kind === 'index' ? 'Index' : entry.kind ? 'Stock' : '') : '';
    const stale = !!((pos && pos.stale) || (px && px.stale === true));

    if (price) {
      const abs = isNum(price.prev) ? price.v - price.prev : null;
      const pct = isNum(price.pct) ? price.pct : (isNum(price.prev) && price.prev !== 0 ? (abs / price.prev) * 100 : null);
      const cls = signCls(isNum(pct) ? pct : abs);
      const dayBits = [];
      if (isNum(pct)) dayBits.push(fmtPct(pct));
      if (isNum(abs)) dayBits.push((abs > 0 ? '+' : '') + fmtMoney(abs, price.cur));
      setContent(dom.priceBox,
        el('span', { class: 'dt-price num', text: fmtMoney(price.v, price.cur) }),
        dayBits.length ? el('span', { class: `dt-day num ${cls}`, title: 'Change vs previous close', text: dayBits.join(' \u00b7 ') }) : null,
        kindLabel ? chip(kindLabel) : null,
        stale ? chip('last close', 'chip-warn', pos && pos.priceAsOf ? 'Price as of ' + fmtDateTime24(pos.priceAsOf) : 'Market closed or quote delayed') : null);
    } else if (bench && isNum(bench.dayPct)) {
      setContent(dom.priceBox,
        el('span', { class: `dt-day num ${signCls(bench.dayPct)}`, text: fmtPct(bench.dayPct) }),
        kindLabel ? chip(kindLabel) : null);
    } else if (pSlot.error && !pSlot.data) {
      renderState(dom.priceBox, { error: pSlot.error, retry: () => refreshPortfolio() });
    } else if (pSlot.loading || !pSlot.data) {
      renderState(dom.priceBox, { loading: 'Loading price\u2026' });
    } else {
      setContent(dom.priceBox, el('span', { class: 'dt-price num muted', text: '\u2013' }), kindLabel ? chip(kindLabel) : null,
        el('span', { class: 'muted', text: 'No price yet' }));
    }

    if (px && px.usLive && isNum(px.usLive.price)) {
      dom.usLive.hidden = false;
      dom.usLive.textContent = `US live reference: ${fmtMoney(px.usLive.price, px.usLive.currency || 'USD')} (as of ${fmtTime24(px.usLive.asOf)})`;
    } else {
      dom.usLive.hidden = true;
      dom.usLive.textContent = '';
    }

    const ts = (pos ? pSlot.ts : null) || store.slot('prices').ts || pSlot.ts;
    dom.headStamp.setTs(ts, !!(pSlot.error && pSlot.data) || stale);

    const sym = tvSymbolFor(entry) || id;
    dom.tvLink.href = tvUrlFor(sym);
  }

  // ---- chart --------------------------------------------------------------

  function pushChart() {
    if (!S || !S.points) return;
    const ind = anyOverlay() && S.ind && S.ind.range === S.pointsRange ? S.ind.data : null;
    chartCall('setState', null);
    chartCall('setData', {
      points: S.points, range: S.pointsRange, markers: S.markers || [], indicators: ind,
      currency: S.currency, label: labelOf(S.id),
    });
  }

  function setChartNote() {
    if (!S) return;
    const bits = [];
    if (S.histStale) bits.push('Showing earlier prices \u2013 the latest refresh failed.');
    if (S.indError && anyOverlay()) bits.push('Indicators unavailable: ' + S.indError);
    dom.chartNote.hidden = !bits.length;
    dom.chartNote.textContent = bits.join(' ');
  }

  async function loadHistory({ silent = false } = {}) {
    const s = S;
    if (!s) return;
    const range = prefs.range;
    const t = lr.hist.begin();
    dom.chartRetry.hidden = true;
    if (!silent || !s.points || s.pointsRange !== range) {
      s.points = null;
      chartCall('setState', { loading: true });
    }
    const res = await apiGet(`/api/history/${encodeURIComponent(s.id)}?range=${range}`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return;
    if (!res.ok) {
      if (s.points && s.pointsRange === range) {
        s.histStale = true;
        dom.chart.stamp.setTs(s.histTs, true);
      } else {
        chartCall('setState', { error: res.error || 'History unavailable' });
        dom.chartRetry.hidden = false;
        s.histFailed = true;
      }
      setChartNote();
      return;
    }
    const d = res.data;
    const arr = Array.isArray(d && d.data) ? d.data : (Array.isArray(d) ? d : []);
    const points = arr.filter(p => p && isNum(p.c));
    s.histStale = false;
    s.histFailed = false;
    s.histTs = Date.now();
    dom.chart.stamp.setTs(s.histTs, false);
    if (points.length < 2) {
      s.points = null;
      chartCall('setState', { empty: 'No price data for this range.' });
    } else {
      s.points = points;
      s.pointsRange = range;
      const cur = (d && d.currency) || (entryOf(s.id) && entryOf(s.id).listing && entryOf(s.id).listing.currency) || 'EUR';
      s.currency = cur;
      pushChart();
    }
    setChartNote();
  }

  async function loadMarkers() {
    const s = S;
    if (!s) return;
    const t = lr.markers.begin();
    const res = await apiGet(`/api/analysis-markers/${encodeURIComponent(s.id)}`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return;
    s.markers = res.ok && Array.isArray(res.data) ? res.data : [];
    pushChart();
  }

  async function loadIndicators() {
    const s = S;
    if (!s || !anyOverlay()) return;
    const range = prefs.range;
    const t = lr.ind.begin();
    const res = await apiGet(`/api/indicators/${encodeURIComponent(s.id)}?range=${range}&ema=20,50&rsi=14`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return;
    if (res.ok && res.data && typeof res.data === 'object') {
      s.ind = { range, data: res.data };
      s.indError = null;
    } else {
      s.indError = res.error || 'no data';
    }
    pushChart();
    setChartNote();
  }

  function setRange(r) {
    if (r === prefs.range) return;
    prefs.range = r;
    for (const b of dom.rangeSeg.children) setPressed(b, b.dataset.range === r);
    if (!S) return;
    S.points = null;
    S.ind = null;
    S.histStale = false;
    loadHistory();
    if (anyOverlay()) loadIndicators();
  }

  function paintOverlays() {
    for (const b of dom.overlaySeg.children) setPressed(b, prefs.overlays[b.dataset.overlay]);
    dom.chartHost.classList.toggle('has-rsi', prefs.overlays.rsi14);
  }

  function toggleOverlay(k) {
    prefs.overlays[k] = !prefs.overlays[k];
    paintOverlays();
    chartCall('setOverlays', { ...prefs.overlays });
    if (!S) return;
    if (anyOverlay() && (!S.ind || S.ind.range !== S.pointsRange)) loadIndicators();
    else { pushChart(); setChartNote(); }
  }

  // ---- AI analysis --------------------------------------------------------

  function aiAvailability(id) {
    const e = entryOf(id);
    if (e && (e.role === 'benchmark' || e.kind === 'index')) return 'hidden';
    if (e && e.kind === 'etf') return 'etf';
    if (e && e.analyzeable === false) return 'no';
    return 'ok';
  }

  function paintAiShell() {
    if (!S) return;
    const mode = aiAvailability(S.id);
    dom.ai.node.hidden = mode === 'hidden';
    const live = mode === 'ok';
    dom.aiLive.hidden = !live;
    dom.aiNote.hidden = live;
    if (mode === 'etf') dom.aiNote.textContent = 'Not analysed: ETF. AI analysis covers single stocks only.';
    else if (mode === 'no') dom.aiNote.textContent = 'Not analysed: this holding is excluded from AI analysis.';
    paintJob();
  }

  function setMode(m) {
    if (S && S.job) return;
    prefs.mode = m;
    for (const b of dom.modeSeg.children) setPressed(b, b.dataset.mode === m);
  }

  function paintJob() {
    const j = S && S.job;
    dom.analyseBtn.disabled = !!j;
    for (const b of dom.modeSeg.children) b.disabled = !!j;
    dom.cancelBtn.hidden = !j;
    if (j) {
      const queued = j.status === 'queued';
      dom.cancelBtn.disabled = !queued;
      dom.cancelBtn.title = queued ? 'Drop this run from the queue' : 'Already running \u2013 the lane stays busy until it finishes';
      const line = j.message || (queued ? 'queued \u2013 waiting for the analysis lane' : 'running');
      const lane = laneText(j);
      setContent(dom.aiProgress,
        el('div', { class: 'dt-ai-line' }, el('span', { class: 'dt-spin', 'aria-hidden': 'true' }),
          el('span', { text: `${cap(j.mode)} analysis: ${line}` })),
        el('div', { class: 'dt-bar', 'aria-hidden': 'true' }, el('span')),
        lane ? el('div', { class: 'dt-lane muted', text: 'Running on ' + lane }) : null);
      dom.aiProgress.hidden = false;
    } else {
      dom.aiProgress.hidden = true;
      clear(dom.aiProgress);
    }
  }

  function paintAiResultMessage(text, tone) {
    dom.aiResult.hidden = false;
    setContent(dom.aiResult, el('div', { class: 'state ' + (tone === 'error' ? 'state-error' : 'state-empty') + ' dt-ai-msg', role: tone === 'error' ? 'alert' : null },
      el('div', { class: 'state-text' }, el('div', { class: 'state-detail', text: text }))));
  }

  async function triggerAnalysis() {
    const s = S;
    if (!s || s.job || aiAvailability(s.id) !== 'ok') return;
    const lane = store.slot('lane').data;
    if (lane && lane.serving_model === false) {
      const go = await confirmDialog(
        'No analysis lane is serving a model right now. The run will wait in the queue until a lane comes up. Queue it anyway?',
        { title: 'Analysis lane down', confirmText: 'Queue anyway' });
      if (!go || s !== S || destroyed) return;
    }
    const mode = prefs.mode;
    dom.analyseBtn.disabled = true;
    dom.aiResult.hidden = true;
    const res = await apiSend('POST', `/api/analyse/${encodeURIComponent(s.id)}`, { mode });
    if (s !== S || destroyed) return;
    if (!res.ok || !res.data || !res.data.job_id) {
      dom.analyseBtn.disabled = false;
      const msg = res.error || 'the server did not return a job';
      paintAiResultMessage(`Analysis not started: ${msg}`, 'error');
      toast(msg, { type: 'error', title: `${s.id}: analysis not started` });
      return;
    }
    const d = res.data;
    if (d.deduped) toast(`${s.id} already has a ${d.mode || mode} analysis in the queue.`, { type: 'info' });
    s.job = { id: d.job_id, ticker: s.id, mode: d.mode || mode, status: d.status || 'queued', message: '' };
    s.lastLane = null;
    paintJob();
    startJobPoll(s);
  }

  function startJobPoll(s) {
    if (stopJobPoll) stopJobPoll();
    s.jobAttempts = 0;
    s.jobFails = 0;
    stopJobPoll = usePolling(() => pollJob(s), JOB_POLL_MS, { visibilityAware: true, immediate: true });
  }

  function endJobPoll() {
    if (stopJobPoll) stopJobPoll();
    stopJobPoll = null;
  }

  async function pollJob(s) {
    if (s !== S || destroyed || !s.job) { endJobPoll(); return; }
    const job = s.job;
    if (++s.jobAttempts > JOB_POLL_MAX) {
      endJobPoll();
      s.job = null;
      paintJob();
      paintAiResultMessage('The analysis did not finish in time. Check AI Ops for its status.', 'error');
      toast(`${s.id} did not finish in time.`, { type: 'warn', title: 'Analysis timed out' });
      return;
    }
    const t = lr.job.begin();
    const res = await apiGet(`/api/analysis/${encodeURIComponent(job.id)}`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted || s.job !== job) return;
    if (!res.ok) {
      if (res.status === 404 || ++s.jobFails >= JOB_FAIL_MAX) {
        endJobPoll();
        s.job = null;
        paintJob();
        paintAiResultMessage(res.status === 404 ? 'The job is no longer known (server restarted?).' : `Status unavailable: ${res.error}`, 'error');
        return;
      }
      job.message = `status unavailable, retrying (${res.error})`;
      paintJob();
      return;
    }
    s.jobFails = 0;
    const j = res.data || {};
    Object.assign(job, {
      status: j.status || job.status, mode: j.mode || job.mode, message: j.message || '',
      decision: j.decision || null, result_path: j.result_path || null,
      lane: j.lane || job.lane, model: j.model || job.model, lane_fallback: !!j.lane_fallback,
    });
    if (job.status === 'queued' || job.status === 'running') { paintJob(); return; }
    endJobPoll();
    s.job = null;
    paintJob();
    if (job.status === 'done') {
      await finishJob(s, job);
    } else if (/cancel/i.test(job.message || '')) {
      dom.aiResult.hidden = true;
      toast(`${s.id}: analysis cancelled`, { type: 'info' });
    } else {
      paintAiResultMessage(job.message || 'Analysis failed.', 'error');
      toast(job.message || 'no detail', { type: 'error', title: `${s.id}: analysis failed` });
    }
  }

  async function finishJob(s, job) {
    s.lastLane = { lane: job.lane, model: job.model, fallback: job.lane_fallback };
    const rows = await loadReports();
    if (s !== S || destroyed) return;
    loadMarkers();
    loadHistory({ silent: true });
    const rep = (rows || []).find(r => r.id === job.result_path) || (rows || [])[0] || null;
    if (!rep) {
      paintAiResultMessage('The run finished, but no report was found in the results directory.', 'error');
      return;
    }
    toast(rep.decision ? `Decision: ${rep.decision}` : 'Report written', { type: 'ok', title: `${s.id}: analysis ready` });
    showResult(s, rep);
  }

  async function cancelJob() {
    const s = S;
    if (!s || !s.job) return;
    dom.cancelBtn.disabled = true;
    const res = await apiSend('POST', `/api/cancel/${encodeURIComponent(s.job.id)}`, {});
    if (s !== S || destroyed) return;
    if (!res.ok) toast(res.error || 'refused', { type: 'warn', title: `${s.id}: cancel refused` });
    else toast('Run dropped from the queue.', { type: 'info', title: `${s.id}: cancelled` });
    if (s.job) pollJob(s);
  }

  async function adoptJob() {
    const s = S;
    if (!s || aiAvailability(s.id) !== 'ok') return;
    const t = lr.jobs.begin();
    const res = await apiGet('/api/jobs?limit=50', { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted || !res.ok || s.job) return;
    const rows = Array.isArray(res.data) ? res.data : (res.data && Array.isArray(res.data.jobs) ? res.data.jobs : []);
    const mine = rows.filter(j => j && j.ticker === s.id && (j.status === 'queued' || j.status === 'running'))
      .sort((a, b) => (a.status === 'running' ? 0 : 1) - (b.status === 'running' ? 0 : 1))[0];
    if (!mine) return;
    s.job = { id: mine.id, ticker: s.id, mode: mine.mode || 'standard', status: mine.status, message: mine.message || '', lane: mine.lane, model: mine.model };
    paintJob();
    startJobPoll(s);
  }

  // result summary of the newest finished run
  async function showResult(s, rep) {
    dom.aiResult.hidden = false;
    renderState(dom.aiResult, { loading: 'Loading analysis\u2026' });
    const t = lr.result.begin();
    const res = await apiGet(reportUrl(rep.id), { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return;
    const d = res.data;
    if (!res.ok || !d || typeof d !== 'object' || d.error) {
      renderState(dom.aiResult, { error: (d && d.error) || res.error || 'Report unavailable', retry: () => showResult(s, rep) });
      return;
    }
    const md = typeof d.content === 'string' ? d.content : '';
    const decision = rep.decision || extractDecision(md) || 'HOLD';
    const meta = reportMeta(d, rep);
    const lane = laneText(s.lastLane && (s.lastLane.lane || s.lastLane.model) ? s.lastLane : meta);
    const entry = entryOf(s.id);
    const target = extractPriceTarget(md);
    const horizon = extractTimeHorizon(md);
    const metaBits = [];
    if (rep.date) metaBits.push(String(rep.date));
    if (rep.mode) metaBits.push(cap(rep.mode));
    if (isNum(rep.score)) metaBits.push(`score ${Math.round(rep.score)}/100`);
    if (rep.action) metaBits.push(String(rep.action));
    if (target) metaBits.push('target ' + fmtMoney(Number(target), (entry && entry.quoteCurrency) || 'EUR'));
    if (horizon) metaBits.push('horizon ' + horizon);

    const nodes = [
      el('div', { class: 'dt-decision-card' },
        decisionChip(decision),
        el('span', { class: 'dt-decision-meta muted', text: metaBits.join(' \u00b7 ') })),
    ];
    if (lane) nodes.push(el('div', { class: 'dt-lane muted', text: 'Model: ' + lane }));
    const sections = [['Executive summary', 'Executive Summary'], ['Trader plan', 'Trader Plan'], ['Evidence gaps', 'Evidence Gaps']];
    let found = 0;
    for (const [title, heading] of sections) {
      const body = extractSection(md, heading, 1000);
      if (!body) continue;
      found++;
      const box = el('div', { class: 'md dt-md' });
      setMarkdown(box, body);
      nodes.push(el('div', { class: 'dt-sec-block' }, el('h4', { class: 'lbl', text: title }), box));
    }
    if (!found && rep.summary) nodes.push(el('p', { class: 'dt-ai-summary', text: String(rep.summary) }));
    nodes.push(el('div', { class: 'dt-result-actions' },
      el('button', { class: 'btn btn-sm', type: 'button', text: 'Full report', onclick: () => openReportModal(rep) }),
      el('button', { class: 'btn btn-sm btn-ghost', type: 'button', text: 'Dismiss', onclick: () => { dom.aiResult.hidden = true; clear(dom.aiResult); } })));
    setContent(dom.aiResult, ...nodes);
  }

  // history list
  async function loadReports() {
    const s = S;
    if (!s || aiAvailability(s.id) !== 'ok') return null;
    const t = lr.reports.begin();
    if (!s.reports) renderState(dom.aiHist, { loading: 'Loading history\u2026' });
    const res = await apiGet(`/api/ticker-reports/${encodeURIComponent(s.id)}`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return null;
    if (!res.ok || !Array.isArray(res.data)) {
      s.reportsError = res.ok ? 'Unexpected response' : res.error;
      if (!s.reports) renderState(dom.aiHist, { error: s.reportsError, retry: () => loadReports() });
      dom.ai.stamp.setTs(s.reportsTs, true);
      return s.reports;
    }
    s.reportsError = null;
    s.reports = res.data;
    s.reportsTs = Date.now();
    dom.ai.stamp.setTs(s.reportsTs, false);
    paintHistory();
    return s.reports;
  }

  function paintHistory() {
    const s = S;
    if (!s || !s.reports) return;
    const rows = s.reports;
    if (!rows.length) { renderState(dom.aiHist, { empty: 'No analyses yet.' }); return; }
    const shown = s.showAllHist ? rows : rows.slice(0, HIST_VISIBLE);
    const list = el('ul', { class: 'dt-hist' }, shown.map(rep => {
      const mode = String(rep.mode || '').trim().toLowerCase();
      const lane = laneText(rep);
      return el('li', { class: 'dt-hist-item' },
        el('button', {
          class: 'dt-hist-row', type: 'button', dataset: { reportId: rep.id },
          title: lane ? 'Model: ' + lane : null,
          'aria-label': `Open report of ${rep.date || 'unknown date'}${rep.decision ? ', ' + rep.decision : ''}`,
          onclick: () => openReportModal(rep),
        },
        el('span', { class: 'dt-hist-date num', text: rep.date || '\u2013' }),
        decisionChip(rep.decision),
        isNum(rep.score) ? el('span', { class: 'dt-hist-score num', text: `${Math.round(rep.score)}/100` }) : null,
        mode && mode !== 'standard' ? el('span', { class: 'dt-hist-mode', text: cap(mode) }) : null,
        el('span', { class: 'dt-hist-sum', text: String(rep.summary || '').trim() || 'Report available.' })));
    }));
    const nodes = [list];
    if (rows.length > HIST_VISIBLE) {
      nodes.push(el('button', {
        class: 'btn btn-sm btn-ghost dt-hist-more', type: 'button', 'aria-expanded': String(s.showAllHist),
        text: s.showAllHist ? 'Show fewer' : `Show all (${rows.length})`,
        onclick: () => { s.showAllHist = !s.showAllHist; paintHistory(); },
      }));
    }
    setContent(dom.aiHist, ...nodes);
  }

  // ---- position -----------------------------------------------------------

  function renderPosition() {
    if (!S) return;
    const id = S.id;
    const slot = store.slot('portfolio');
    const pos = positionOf(id);
    const body = dom.pos.body;
    if (!pos) {
      if (slot.error && !slot.data) renderState(body, { error: slot.error, retry: () => refreshPortfolio() });
      else if (!slot.data) renderState(body, { loading: 'Loading position\u2026' });
      else {
        const e = entryOf(id);
        renderState(body, { empty: e && e.role === 'benchmark' ? 'Benchmark only \u2013 not a holding.' : 'No position data for this ticker.' });
      }
      dom.pos.stamp.setTs(slot.ts, !!slot.error);
      return;
    }
    const costKnown = isNum(pos.investedEur);
    const unknown = () => chip('cost unknown', 'chip-warn', 'No cost basis entered for this holding');
    const avg = costKnown && isNum(pos.shares) && pos.shares > 0 ? pos.investedEur / pos.shares : null;
    const pnl = isNum(pos.pnlEur)
      ? el('span', { class: signCls(pos.pnlEur) }, fmtEur(pos.pnlEur, { sign: true }), isNum(pos.pnlPct) ? ` (${fmtPct(pos.pnlPct)})` : '')
      : unknown();
    const day = isNum(pos.dayPnlEur)
      ? el('span', { class: signCls(pos.dayPnlEur) }, fmtEur(pos.dayPnlEur, { sign: true }), isNum(pos.dayPct) ? ` (${fmtPct(pos.dayPct)})` : '')
      : '\u2013';
    setContent(body, kvList([
      ['Shares', fmtShares(pos.shares), 'num'],
      ['Value', fmtEur(pos.valueEur), 'num'],
      ['Cost basis', costKnown ? fmtEur(pos.investedEur) : unknown(), 'num'],
      ['Avg cost / share', avg != null ? fmtEur(avg) : (costKnown ? '\u2013' : unknown()), 'num'],
      ['P&L', pnl, 'num'],
      ['Weight', isNum(pos.weightPct) ? fmtPct(pos.weightPct, { sign: false, dec: 1 }) : '\u2013', 'num'],
      ['Day P&L', day, 'num'],
      ['Max drawdown', isNum(pos.mddPct) ? fmtPct(-Math.abs(pos.mddPct), { sign: false }) : '\u2013', 'num'],
    ]));
    dom.pos.stamp.setTs(slot.ts, !!slot.error || !!slot.stale || !!pos.stale);
  }

  // ---- valuation + signals --------------------------------------------------

  async function loadTicker() {
    const s = S;
    if (!s) return;
    const t = lr.ticker.begin();
    if (!s.ticker) renderState(dom.val.body, { loading: 'Loading valuation\u2026' });
    const res = await apiGet(`/api/ticker/${encodeURIComponent(s.id)}`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return;
    if (!res.ok || !res.data || typeof res.data !== 'object') {
      if (!s.ticker) { s.tickerFailed = true; renderState(dom.val.body, { error: res.error || 'Unexpected response', retry: () => loadTicker() }); }
      dom.val.stamp.setTs(s.tickerTs, true);
      return;
    }
    s.tickerFailed = false;
    s.ticker = res.data;
    s.tickerTs = Date.now();
    dom.val.stamp.setTs(s.tickerTs, false);
    paintTicker();
  }

  function paintTicker() {
    const s = S;
    if (!s || !s.ticker) return;
    const d = s.ticker;
    const body = dom.val.body;
    if (d.status === 'no_data') {
      renderState(body, { empty: d.message || 'No valuation data available right now.' });
      return;
    }
    const v = d.valuation || {};
    const c = d.context || {};
    const cur = (entryOf(s.id) && entryOf(s.id).quoteCurrency) || 'EUR';
    const rows = [];
    const addNum = (label, n, fmt) => { const x = numOrNull(n); if (x != null) rows.push([label, fmt(x), 'num']); };
    addNum('P/E', v.pe, x => fmtNum(x));
    addNum('Forward P/E', v.forwardPE, x => fmtNum(x));
    addNum('Dividend yield', v.dividendYield, x => fmtNum(x) + '%'); // backend sends percent
    addNum('ROE', v.roe, x => fmtPct(x * 100, { sign: false, dec: 1 })); // backend sends a fraction
    if (c.sector) rows.push(['Sector', String(c.sector)]);
    if (c.industry) rows.push(['Industry', String(c.industry)]);
    addNum('Short ratio', c.shortRatio, x => fmtNum(x));
    addNum('Analyst target', c.analystTarget, x => fmtMoney(x, cur));
    if (c.recommendation) rows.push(['Recommendation', humanize(c.recommendation)]);

    const signals = Array.isArray(d.signals) ? d.signals.filter(x => x && x.label) : [];
    const nodes = [];
    if (rows.length) nodes.push(kvList(rows));
    nodes.push(el('h4', { class: 'lbl dt-sub', text: 'Signals' }));
    if (signals.length) {
      nodes.push(el('ul', { class: 'dt-signals' }, signals.map(sg => {
        const tone = String(sg.tone || 'neutral').toLowerCase();
        const dotCls = tone === 'bullish' ? 'dot dot-ok' : tone === 'bearish' ? 'dot dot-err' : 'dot';
        return el('li', { class: 'dt-signal', dataset: { tone } },
          el('span', { class: dotCls, 'aria-hidden': 'true' }),
          el('span', { text: String(sg.label) }),
          el('span', { class: 'visually-hidden', text: ` (${tone})` }));
      })));
    } else {
      nodes.push(el('p', { class: 'muted dt-none', text: 'No signals right now.' }));
    }
    if (!rows.length && !signals.length) {
      renderState(body, { empty: 'No valuation data for this holding.' });
      return;
    }
    setContent(body, ...nodes);
  }

  // ---- news ---------------------------------------------------------------

  async function loadNews({ silent = false } = {}) {
    const s = S;
    if (!s) return;
    const t = lr.news.begin();
    if (!silent || !s.news) renderState(dom.news.body, { loading: 'Loading news\u2026' });
    const res = await apiGet(`/api/alerts-news?ticker=${encodeURIComponent(s.id)}&limit=20`, { signal: t.signal });
    if (s !== S || destroyed || !t.current() || res.aborted) return;
    if (!res.ok || !res.data || typeof res.data !== 'object') {
      if (!s.news) { s.newsFailed = true; renderState(dom.news.body, { error: res.error || 'Unexpected response', retry: () => loadNews() }); }
      dom.news.stamp.setTs(s.newsTs, true);
      return;
    }
    s.newsFailed = false;
    s.news = Array.isArray(res.data.entries) ? res.data.entries : [];
    s.newsTs = Date.now();
    dom.news.stamp.setTs(s.newsTs, false);
    paintNews();
  }

  function paintNews() {
    const s = S;
    if (!s || !s.news) return;
    if (!s.news.length) {
      renderState(dom.news.body, { empty: `No alerts or news for ${labelOf(s.id)} in the last 7 days.` });
      return;
    }
    setContent(dom.news.body, el('ul', { class: 'dt-news-list' }, s.news.slice(0, 20).map((item, i) => {
      const href = safeUrl(item.url);
      const link = href && /^https?:/.test(href) && new URL(href).host !== location.host ? href : '';
      const m = item.kind === 'price' ? /([+\-\u2212]\d+(?:\.\d+)?)%/.exec(item.title || '') : null;
      return el('li', { class: 'dt-news-item', dataset: { newsIndex: String(i) } },
        el('button', { class: 'dt-news-main', type: 'button', onclick: () => openNewsModal(item) },
          chip(String(item.kind || 'alert').toUpperCase(), item.kind === 'price' ? 'chip-info' : ''),
          el('time', { class: 'dt-news-time num muted', text: item.ts ? fmtDateTime24(item.ts) : '' }),
          el('span', { class: 'dt-news-title', text: String(item.title || '') }),
          m ? el('span', { class: 'num ' + (m[1].startsWith('+') ? 'up' : 'down'), text: m[1] + '%' }) : null),
        el('div', { class: 'dt-news-foot muted' },
          el('span', { class: 'dt-news-source', text: String(item.source || '') }),
          link ? el('a', { class: 'dt-ext-link', href: link, target: '_blank', rel: 'noopener noreferrer', text: 'Source \u2197',
            'aria-label': `Open source: ${item.title || 'link'}` }) : null));
    })));
  }

  // ---- lifecycle ------------------------------------------------------------

  function paintEmpty() {
    dom.content.hidden = true;
    dom.empty.hidden = false;
    root.setAttribute('aria-label', 'Holding details');
  }

  function resetSections() {
    chartCall('setState', { loading: true });
    dom.chartRetry.hidden = true;
    dom.chartNote.hidden = true;
    dom.chart.stamp.setTs(null);
    dom.ai.stamp.setTs(null);
    dom.pos.stamp.setTs(null);
    dom.val.stamp.setTs(null);
    dom.news.stamp.setTs(null);
    clear(dom.aiHist);
    dom.aiResult.hidden = true;
    clear(dom.aiResult);
    renderState(dom.val.body, { loading: 'Loading valuation\u2026' });
    renderState(dom.news.body, { loading: 'Loading news\u2026' });
    paintOverlays();
    chartCall('setOverlays', { ...prefs.overlays });
  }

  function startPolls() {
    stopChartPoll = usePolling(() => {
      loadHistory({ silent: true });
      if (anyOverlay()) loadIndicators();
    }, CHART_REFRESH_MS, { visibilityAware: true });
    stopNewsPoll = usePolling(() => loadNews({ silent: true }), NEWS_REFRESH_MS, { visibilityAware: true });
  }

  function show(id) {
    if (destroyed) return;
    id = id ? String(id) : null;
    if (S && id === S.id) {
      // Same holding: repaint from the store and retry only what failed.
      renderHeader(); renderPosition(); paintAiShell();
      if (S.histFailed) loadHistory();
      if (S.tickerFailed) loadTicker();
      if (S.newsFailed) loadNews();
      if (S.reportsError && !S.reports) loadReports();
      return;
    }
    abortAll();
    stopPolls();
    if (!id) { S = null; paintEmpty(); return; }
    S = newState(id);
    dom.empty.hidden = true;
    dom.content.hidden = false;
    resetSections();
    renderHeader();
    renderPosition();
    paintAiShell();
    loadHistory();
    loadMarkers();
    if (anyOverlay()) loadIndicators();
    loadTicker();
    loadNews();
    loadReports();
    adoptJob();
    startPolls();
    // Move focus to the heading on a user-driven switch, never when focus is already inside.
    if (!root.contains(document.activeElement) && dom.title.offsetParent !== null) {
      dom.title.focus({ preventScroll: true });
    }
  }

  // live store updates
  unsubs.push(store.subscribe('portfolio', () => { renderHeader(); renderPosition(); }));
  unsubs.push(store.subscribe('prices', () => renderHeader()));
  const onEntries = () => { if (S) { renderHeader(); paintAiShell(); } };
  unsubs.push(store.subscribe('watchlist', onEntries));
  unsubs.push(store.subscribe('config', onEntries));

  paintOverlays();
  paintEmpty();

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    stopPolls();
    abortAll();
    for (const u of unsubs) u();
    unsubs.length = 0;
    chartCall('destroy');
    chart = null;
    S = null;
    clear(root);
    root.classList.remove('dt-pane');
    root.removeAttribute('role');
    root.removeAttribute('aria-label');
  }

  return { show, destroy };
}
