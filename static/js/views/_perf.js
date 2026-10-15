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

/** Label of the performance benchmark for the hero KPI: history label, else the id from /performance. */
export function perfBenchmarkLabel(history, performance, labelOf) {
  if (history && history.perfBenchmark && history.perfBenchmark.label) return history.perfBenchmark.label;
  if (performance && performance.perfBenchmarkId) return labelOf(performance.perfBenchmarkId);
  return null;
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
