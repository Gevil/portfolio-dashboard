// Practice-only "Compare" card body: daily vs intraday vs buy & hold from GET /api/paper/compare
// ({asOf, from, buckets: {daily, intraday}, buyHold, decisions, turnoverPct, costDragPct, warnings}).
// A bucket without a season (or disabled / failed metrics) is null: its column stays, cells show an
// en dash and the header says why. Every value is a number or text built here; nothing is HTML.
import { el, fmtNum, isNum } from '../util.js';
import { verdictOf, ciText, signedPp, plainPct, ratio } from './_perf.js';

export const COMPARE_BUCKETS = [['daily', 'Daily'], ['intraday', 'Intraday']];
const DASH = '\u2013';

const pp = v => (isNum(v) ? `${signedPp(v)} pp` : DASH);
const retPct = v => (isNum(v) ? `${signedPp(v)}%` : DASH);
const pct2 = v => (isNum(v) ? `${fmtNum(v, 2)}%` : DASH);
const signed = v => (isNum(v) ? (v > 0 ? 'pos' : v < 0 ? 'neg' : 'flat') : 'flat');

function decisionsText(d) {
  if (!d || typeof d !== 'object') return DASH;
  const n = k => (isNum(d[k]) ? d[k] : 0);
  return `${n('valid')} valid \u00b7 ${n('noChange')} no change \u00b7 ${n('invalid')} invalid \u00b7 ${n('deferred')} deferred`;
}

/** Why a bucket column is empty: 'not enabled (mode off)' | 'no season yet' | null when it has metrics. */
function emptyReason(id, m, statusBuckets) {
  if (m && typeof m === 'object') return null;
  const s = statusBuckets && statusBuckets[id];
  if (s && s.enabled === false) return `not enabled${s.mode ? ` (mode ${s.mode})` : ''}`;
  if (s && s.season && s.season.started === false) return 'no season yet';
  return 'no metrics yet';
}

/**
 * payload: the /api/paper/compare body. statusBuckets: status.buckets (optional, only used to explain empty columns).
 * Returns the card body node (<div id="compare-body">): a table with one column per bucket + buy & hold.
 */
export function buildCompare(payload, statusBuckets) {
  const p = payload && typeof payload === 'object' ? payload : {};
  const buckets = p.buckets && typeof p.buckets === 'object' ? p.buckets : {};
  const bh = p.buyHold && typeof p.buyHold === 'object' ? p.buyHold : null;
  const cols = COMPARE_BUCKETS.map(([id, label]) => ({
    id, label, m: buckets[id] && typeof buckets[id] === 'object' ? buckets[id] : null,
    why: emptyReason(id, buckets[id], statusBuckets),
  }));

  const cell = (text, cls) => el('td', { class: `num ${cls || ''}`.trim() }, text);
  // [label, bucketValue(m, id), buyHoldValue(bh)] -> {text, cls}
  const rows = [
    ['Return', m => ({ text: retPct(m.returnPct), cls: signed(m.returnPct) }), b => ({ text: retPct(b.returnPct), cls: signed(b.returnPct) })],
    ['Excess vs buy & hold', m => ({ text: pp(m.excessPct), cls: signed(m.excessPct) }), null],
    ['Excess, net of costs', m => ({ text: pp(m.netExcessPct), cls: signed(m.netExcessPct) }), null],
    ['95% interval', m => ({ text: ciText(m.excessCI95) || DASH }), null],
    ['Verdict', m => ({ verdict: verdictOf(m) }), null],
    ['Max drawdown', m => ({ text: plainPct(isNum(m.maxDrawdownPct) ? -Math.abs(m.maxDrawdownPct) : null) }),
      b => ({ text: plainPct(isNum(b.maxDrawdownPct) ? -Math.abs(b.maxDrawdownPct) : null) })],
    ['Sharpe', m => ({ text: ratio(m.sharpe) }), b => ({ text: ratio(b.sharpe) })],
    ['Sortino', m => ({ text: ratio(m.sortino) }), b => ({ text: ratio(b.sortino) })],
    ['Beta vs buy & hold', m => ({ text: ratio(m.betaToBuyHold) }), null],
    ['Turnover', (m, id) => ({ text: plainPct(isNum(p.turnoverPct && p.turnoverPct[id]) ? p.turnoverPct[id] : m.turnoverPct) }), null],
    ['Cost drag', (m, id) => ({ text: pct2(isNum(p.costDragPct && p.costDragPct[id]) ? p.costDragPct[id] : m.costDragPct) }), null],
    ['Decisions', (m, id) => ({ text: decisionsText(p.decisions && p.decisions[id]), wide: true }), null],
    ['Sessions', m => ({ text: isNum(m.sessions) ? String(m.sessions) : DASH }), b => ({ text: isNum(b.sessions) ? String(b.sessions) : DASH })],
  ];

  const render = (spec, hasData) => {
    if (!spec || !hasData) return cell(DASH, 'flat');
    if (spec.verdict) {
      const vd = spec.verdict;
      return el('td', {}, el('span', { class: `agent-chip perf-verdict is-verdict-${vd.key}` },
        el('span', { 'aria-hidden': 'true', text: `${vd.icon} ` }), vd.text));
    }
    return el('td', { class: `${spec.wide ? 'compare-wide' : 'num'} ${spec.cls || ''}`.trim() }, spec.text);
  };

  const head = el('tr', {},
    el('th', { scope: 'col' }, 'Measure'),
    ...cols.map(c => el('th', { scope: 'col', class: 'num', dataset: { bucket: c.id } }, c.label,
      c.why ? el('span', { class: 'compare-why muted', text: ` (${c.why})` }) : null)),
    el('th', { scope: 'col', class: 'num', dataset: { bucket: 'buy_hold' } }, 'Buy & hold'));

  const body = rows.map(([label, bucketFn, bhFn]) => el('tr', { dataset: { measure: label } },
    el('th', { scope: 'row' }, label),
    ...cols.map(c => render(c.m ? bucketFn(c.m, c.id) : null, !!c.m)),
    render(bh && bhFn ? bhFn(bh) : null, !!bh)));

  const box = el('div', { class: 'compare-body', id: 'compare-body' },
    el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table compare-table', id: 'compare-table' },
      el('caption', { class: 'visually-hidden' }, 'Daily bucket, intraday bucket and buy and hold compared over the same window'),
      el('thead', {}, head),
      el('tbody', {}, body))));
  const notes = [];
  if (p.from) notes.push(`Measured from ${p.from} (the later of the two season starts).`);
  if (!bh) notes.push('No buy & hold line yet: no bucket has a season.');
  if (Array.isArray(p.warnings)) for (const w of p.warnings) if (w && w.message) notes.push(String(w.message));
  if (notes.length) box.append(el('p', { class: 'agent-note muted', id: 'compare-note', text: notes.join(' ') }));
  return box;
}

const BENCH_IDS = ['buy_hold', 'buy_hold_sxr8', 'buy_hold_real'];

/**
 * Benchmark return table for the Compare card (open-universe season): the primary EUNL buy & hold, SXR8 and the
 * owner's real weights (buy_hold_real), each measured over the chart range of /api/portfolio/history. Returns null
 * when the payload carries no secondary benchmark (watchlist seasons keep the original card).
 */
export function buildBenchmarks(history, rangeLabel) {
  const h = history && typeof history === 'object' ? history : null;
  const list = h && Array.isArray(h.baselines) ? h.baselines : [];
  if (!list.some(b => b && (b.id === 'buy_hold_sxr8' || b.id === 'buy_hold_real'))) return null;
  const ix = h.indexed && typeof h.indexed === 'object' ? h.indexed : {};
  const lastPct = id => {
    const s = Array.isArray(ix[id]) ? ix[id] : [];
    const p = s.length ? s[s.length - 1].pct : null;
    return isNum(p) ? p : null;
  };
  const bucket = lastPct('portfolio');
  const rows = BENCH_IDS.map(id => list.find(b => b && b.id === id)).filter(Boolean).map(b => {
    const pct = lastPct(b.id);
    const primary = b.id === 'buy_hold';
    return el('tr', { dataset: { benchmark: b.id } },
      el('th', { scope: 'row' }, b.label || b.id, primary ? el('span', { class: 'chip', text: 'primary' }) : null),
      el('td', { class: `num ${signed(pct)}` }, retPct(pct)),
      el('td', { class: `num ${signed(isNum(bucket) && isNum(pct) ? bucket - pct : null)}` }, isNum(bucket) && isNum(pct) ? pp(bucket - pct) : DASH));
  });
  return el('div', { class: 'compare-bench', id: 'compare-benchmarks' },
    el('h3', { class: 'agent-subtitle' }, `Benchmarks over ${rangeLabel}`),
    el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table', id: 'compare-benchmarks-table' },
      el('caption', { class: 'visually-hidden' }, 'Benchmark buy and hold returns over the chart range and the bucket difference'),
      el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, 'Benchmark'), el('th', { scope: 'col', class: 'num' }, 'Return'), el('th', { scope: 'col', class: 'num' }, 'Bucket minus benchmark'))),
      el('tbody', {}, rows))),
    el('p', { class: 'agent-note muted' }, 'Listing prices only (no dividends). EUNL is the primary benchmark; SXR8 and the real weights are frozen at season start.'));
}

/**
 * Read-only history of earlier seasons for the Agent card. `versions` = performance.versions rows
 * ({strategyVersion, from, to}); `previous` = status.season.previous when the server sends it
 * ({strategyVersion, inceptionDate, startedAt}). The current strategy version is not listed.
 * Returns null when there is nothing earlier to show.
 */
export function buildSeasonHistory(versions, previous, currentVersion) {
  const seen = new Map();
  for (const v of Array.isArray(versions) ? versions : []) {
    if (!v || !v.strategyVersion) continue;
    seen.set(v.strategyVersion, { version: v.strategyVersion, from: v.from || null, to: v.to || null });
  }
  for (const p of Array.isArray(previous) ? previous : []) {
    if (!p || !p.strategyVersion) continue;
    const prior = seen.get(p.strategyVersion) || {};
    seen.set(p.strategyVersion, { version: p.strategyVersion, from: prior.from || p.inceptionDate || null, to: prior.to || null });
  }
  const rows = [...seen.values()].filter(r => r.version !== currentVersion);
  if (!currentVersion || !rows.length) return null;
  rows.sort((a, b) => String(a.from || '').localeCompare(String(b.from || '')));
  return el('section', { class: 'season-history', id: 'season-history', 'aria-labelledby': 'season-history-title' },
    el('h3', { class: 'agent-subtitle', id: 'season-history-title' }, 'Previous seasons'),
    el('ul', { class: 'season-history-list' }, rows.map(r => el('li', { dataset: { version: r.version } },
      el('strong', {}, r.version), ` ${r.from || DASH} \u2013 ${r.to || 'ended'}`, el('span', { class: 'muted' }, ' \u00b7 read-only')))));
}
