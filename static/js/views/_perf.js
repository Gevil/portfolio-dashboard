// Performance card helpers: benchmark choice (perf benchmark vs role benchmark), KPI strip and the
// "history applies today's share counts" warning. Inputs are the /api/portfolio/history and
// /api/portfolio/performance payloads (C4); every field may be missing on an older server.
import { el, fmtEur, fmtNum, fmtPct, isNum, signCls } from '../util.js';

export const PERF_RANGES = ['1M', '3M', '6M', '1Y', 'ALL'];

export const BASIS_WARNING = 'History applies today\'s share counts to the whole window \u2014 add purchase dates (lots) in Settings \u2192 Positions for a true return';

export function kindText(kind) {
  return kind === 'total_return' ? 'total return' : 'price index';
}

/**
 * Benchmark lines available for the chart: {perf, role, toggle}. Each = {id, label, kind, series} or null.
 * `perf` is the performance benchmark (SXR8 total return, ...), `role` the GSPC price index.
 * The toggle is only offered when the two lines actually differ.
 */
export function benchmarkChoices(h) {
  if (!h) return { perf: null, role: null, toggle: false };
  const ix = h.indexed || {};
  const role = h.benchmark ? { id: h.benchmark.id, label: h.benchmark.label || h.benchmark.id, kind: 'price', series: ix.benchmark || [] } : null;
  const pb = h.perfBenchmark
    ? { id: h.perfBenchmark.id, label: h.perfBenchmark.label || h.perfBenchmark.id, kind: h.perfBenchmark.kind === 'total_return' ? 'total_return' : 'price', series: ix.perfBenchmark || [] }
    : null;
  return { perf: pb || role, role, toggle: !!(pb && role && (pb.id !== role.id || pb.kind !== role.kind)) };
}

/**
 * Label of the performance benchmark for the hero KPI: history label, baselines list, else the id from /performance.
 * Practice never falls back to the raw baseline id ('buy_hold'): unresolved -> null, and the label drops the
 * trailing parenthetical ('Buy & hold (your weights)' -> 'Buy & hold') to fit the KPI caption.
 */
export function perfBenchmarkLabel(history, performance, labelOf, practice = false) {
  if (practice) {
    const id = performance && performance.perfBenchmarkId;
    const list = history && Array.isArray(history.baselines) ? history.baselines : [];
    const b = id ? list.find(x => x && x.id === id) : null;
    return b && b.label ? String(b.label).replace(/\s*\(.*\)\s*$/, '') || b.label : null;
  }
  if (history && history.perfBenchmark && history.perfBenchmark.label) return history.perfBenchmark.label;
  const id = performance && performance.perfBenchmarkId;
  if (id && history && Array.isArray(history.baselines)) {
    const b = history.baselines.find(x => x && x.id === id);
    if (b && b.label) return b.label;
  }
  return id ? labelOf(id) : null;
}

/**
 * Practice-book baseline lines for the performance chart (plan §8.1): [{id, label, series}], series
 * pulled from `indexed[id]`. Empty when no season has started yet (server sends baselines: []).
 */
export function baselineSeries(h) {
  if (!h || !Array.isArray(h.baselines) || !h.baselines.length) return [];
  const ix = h.indexed || {};
  return h.baselines
    .map(b => ({ id: b.id, label: b.label || b.id, series: Array.isArray(ix[b.id]) ? ix[b.id] : [] }))
    .filter(b => b.series.length);
}

/** 'current_shares' | 'lots' | null, preferring the performance payload. */
export function basisOf(history, performance) {
  return (performance && performance.basis) || (history && history.basis) || null;
}

function kpi(label, value, cls, sub) {
  return el('div', { class: 'perf-kpi' },
    el('dt', { class: 'lbl', text: label }),
    el('dd', { class: `num ${cls || ''}`.trim() }, value, sub ? el('span', { class: 'perf-kpi-sub muted', text: ` ${sub}` }) : null));
}

/** <dl id="perf-kpis">: TWR, XIRR (only when not null), benchmark return, excess, start -> end value. */
export function buildKpis(performance, benchLabel) {
  const p = performance;
  const dl = el('dl', { class: 'perf-kpis', id: 'perf-kpis' });
  dl.append(kpi('TWR', fmtPct(p.twrPct), signCls(p.twrPct), 'time-weighted'));
  if (isNum(p.xirrPct)) dl.append(kpi('XIRR', fmtPct(p.xirrPct), signCls(p.xirrPct), 'annualised'));
  if (isNum(p.perfBenchmarkPct)) dl.append(kpi(benchLabel || 'Benchmark', fmtPct(p.perfBenchmarkPct), signCls(p.perfBenchmarkPct)));
  if (isNum(p.excessPct)) dl.append(kpi('Excess', `${p.excessPct >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(p.excessPct), 1)} pp`, signCls(p.excessPct)));
  if (isNum(p.startValueEur) && isNum(p.endValueEur)) dl.append(kpi('Value', `${fmtEur(p.startValueEur)} \u2192 ${fmtEur(p.endValueEur)}`, 'flat'));
  return dl;
}

/** The current_shares warning with its Settings shortcut. */
export function buildBasisWarning(onFix) {
  return el('div', { class: 'perf-basis', id: 'perf-basis-warning', role: 'note' },
    el('span', { class: 'perf-basis-text warn-ink', text: BASIS_WARNING }),
    el('button', { type: 'button', class: 'btn btn-sm', id: 'perf-basis-fix', onclick: onFix, text: 'Add lots' }));
}

export const MIN_SESSIONS = 20;

const VERDICT = {
  ahead: { icon: '\u25b2', text: 'Ahead' },
  behind: { icon: '\u25bc', text: 'Behind' },
  inconclusive: { icon: '\u2248', text: 'Inconclusive' },
};

export const signedPp = v => `${v >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(v), 1)}`;
export const plainPct = v => (isNum(v) ? `${fmtNum(v, 1)}%` : '\u2013');
/** Ratio with two decimals; null below the server's minimum sample is shown as an en dash. */
export const ratio = v => (isNum(v) ? fmtNum(v, 2) : '\u2013');

/** Verdict chip content: {key, icon, text}. Text + icon, never colour only. */
export function verdictOf(m) {
  const n = m && isNum(m.sessions) ? m.sessions : 0;
  const v = m && m.verdict;
  if (v === 'insufficient_sample' || !VERDICT[v]) {
    return { key: 'insufficient_sample', icon: '\u25cc', text: `Too few sessions (${n}/${MIN_SESSIONS})` };
  }
  return { key: v, ...VERDICT[v] };
}

/** "\u2212x.x \u2026 +y.y pp" or null. */
export function ciText(ci) {
  return Array.isArray(ci) && ci.length === 2 && isNum(ci[0]) && isNum(ci[1]) ? `${signedPp(ci[0])} \u2026 ${signedPp(ci[1])} pp` : null;
}

/**
 * Practice KPI strip (contract phase4 §6) from /api/portfolio/performance `metrics` / `versions`.
 * <section id="perf-practice-kpis"> always renders in Practice; missing metrics -> #perf-practice-note.
 * opts: {version: selected strategyVersion | null, onVersion(v)}.
 */
export function buildPracticeKpis(performance, { version = null, onVersion } = {}) {
  const sec = el('section', { class: 'perf-practice', id: 'perf-practice-kpis', 'aria-label': 'Excess return versus buy and hold' });
  const versions = performance && Array.isArray(performance.versions) ? performance.versions.filter(v => v && v.metrics) : [];
  const picked = version ? versions.find(v => v.strategyVersion === version) : null;
  const m = picked ? picked.metrics : (performance && performance.metrics) || null;
  if (versions.length > 1) {
    const cur = picked ? picked.strategyVersion : (versions[versions.length - 1] || {}).strategyVersion;
    const sel = el('select', {
      class: 'agent-input', id: 'perf-version',
      onchange: ev => { if (onVersion) onVersion(ev.target.value); },
    }, versions.map(v => el('option', { value: v.strategyVersion, selected: v.strategyVersion === cur }, `${v.strategyVersion}${v.from ? ` (from ${v.from})` : ''}`)));
    sec.append(el('div', { class: 'perf-version' }, el('label', { class: 'lbl', for: 'perf-version', text: 'Strategy version' }), sel));
  }
  if (!m || typeof m !== 'object') {
    sec.append(el('p', { class: 'pos-note muted', id: 'perf-practice-note', role: 'status' }, 'Evaluation metrics unavailable yet.'));
    return sec;
  }
  const vd = verdictOf(m);
  const ci = ciText(m.excessCI95);
  const dl = el('dl', { class: 'perf-kpis perf-practice-grid' });
  dl.append(
    kpi('Excess vs buy & hold', isNum(m.excessPct) ? `${signedPp(m.excessPct)} pp` : '\u2013', signCls(m.excessPct), 'gross'),
    kpi('Excess, net of costs', isNum(m.netExcessPct) ? `${signedPp(m.netExcessPct)} pp` : '\u2013', signCls(m.netExcessPct), 'net'),
    kpi('95% interval', ci || '\u2013', 'flat'),
    el('div', { class: 'perf-kpi' },
      el('dt', { class: 'lbl', text: 'Verdict' }),
      el('dd', {}, el('span', { class: `agent-chip perf-verdict is-verdict-${vd.key}`, id: 'perf-verdict' },
        el('span', { 'aria-hidden': 'true', text: `${vd.icon} ` }), vd.text))),
    kpi('Max drawdown', plainPct(isNum(m.maxDrawdownPct) ? -Math.abs(m.maxDrawdownPct) : null), 'flat'),
    kpi('Tracking error', plainPct(m.trackingErrorPct), 'flat', 'annualised'),
    kpi('Information ratio', isNum(m.informationRatio) ? fmtNum(m.informationRatio, 2) : '\u2013', 'flat'),
    kpi('Sharpe', ratio(m.sharpe), 'flat', isNum(m.sharpe) ? 'annualised' : `needs ${MIN_SESSIONS} sessions`),
    kpi('Sortino', ratio(m.sortino), 'flat', isNum(m.sortino) ? 'annualised' : `needs ${MIN_SESSIONS} sessions`),
    kpi('Beta vs buy & hold', ratio(m.betaToBuyHold), 'flat', isNum(m.betaToBuyHold) ? null : `needs ${MIN_SESSIONS} sessions`),
    kpi('Turnover', plainPct(m.turnoverPct), 'flat'),
    kpi('Cost drag', isNum(m.costDragPct) ? `${fmtNum(m.costDragPct, 2)}%` : '\u2013', 'flat'),
    kpi('Sessions', isNum(m.sessions) ? String(m.sessions) : '\u2013', 'flat'));
  sec.append(dl);
  return sec;
}
