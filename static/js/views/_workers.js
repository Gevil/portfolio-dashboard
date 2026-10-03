/* Worker health model shared by the Market (rings) and AI Ops (table) views.
   GET /api/background -> {module: {last_run, runs, errors, running, enabled,
   next_run, skip_reason, ...}}; GET /api/worker-runs -> [{ts, worker, ok,
   duration_s, note}] newest first. */
import { isObj, num, str, toSec, fmtSpan, fmtAgo, fmtIn } from './_kit.js';

const RING_LEN = 14;

/* Not every module reports a plain last_run (macro: curve_last/earnings_last/
   cot_last, filings: last_fts ...): the freshest *_last / last_run wins. */
export function lastTs(st) {
  if (!isObj(st)) return null;
  let best = null;
  for (const [k, v] of Object.entries(st)) {
    if (k !== 'last_run' && !/(^|_)last(_|$)/.test(k)) continue;
    const s = toSec(v);
    if (s !== null && (best === null || s > best)) best = s;
  }
  return best;
}

function cadence(runs) {
  const ts = runs.map(r => toSec(r.ts)).filter(v => v !== null).sort((a, b) => a - b);
  if (ts.length < 3) return null;
  const gaps = [];
  for (let i = 1; i < ts.length; i++) gaps.push(ts[i] - ts[i - 1]);
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)] || null;
}

/** -> [{name, st, runs (oldest->newest), verdict: ok|fail|off|stale, last, age, ...}] */
export function summarizeWorkers(bg, runs) {
  const byWorker = new Map();
  (Array.isArray(runs) ? runs : []).forEach(r => {
    if (!isObj(r)) return;
    const w = str(r.worker, '?');
    if (!byWorker.has(w)) byWorker.set(w, []);
    byWorker.get(w).push(r); // newest first
  });
  const names = new Set([...(isObj(bg) ? Object.keys(bg) : []), ...byWorker.keys()]);
  const out = [];
  names.forEach(name => {
    const st = isObj(bg) && isObj(bg[name]) ? bg[name] : {};
    const newestFirst = byWorker.get(name) || [];
    const latest = newestFirst[0] || null;
    const stLast = lastTs(st);
    const runLast = latest ? toSec(latest.ts) : null;
    const last = Math.max(stLast || 0, runLast || 0) || null;
    const age = last === null ? null : Math.max(0, Date.now() / 1000 - last);
    const intervalS = num(st.interval_s) || cadence(newestFirst) || null;
    const limit = intervalS ? Math.max(intervalS * 3.5, 600) : 48 * 3600;
    const disabled = st.enabled === false;
    let verdict = 'ok';
    if (disabled) verdict = 'off';
    else if (latest && latest.ok === false) verdict = 'fail';
    else if (st.running === false && isObj(bg) && name in bg) verdict = 'fail';
    else if (age !== null && age > limit) verdict = 'stale';
    else if (age === null && !disabled && isObj(bg) && name in bg && newestFirst.length === 0) verdict = 'stale';
    const errors = num(st.errors);
    out.push({
      name, st, runs: newestFirst.slice(0, RING_LEN).reverse(), allRuns: newestFirst,
      verdict, last, age, intervalS, errors, disabled,
      note: str((latest && latest.note) || st.last_note || st.skip_reason || ''),
      nextRun: toSec(st.next_run),
    });
  });
  const rank = { fail: 0, stale: 1, ok: 2, off: 3 };
  out.sort((a, b) => rank[a.verdict] - rank[b.verdict] || a.name.localeCompare(b.name));
  return out;
}

export const VERDICT_LABEL = { ok: 'ok', fail: 'failing', stale: 'stale', off: 'disabled' };
const VERDICT_CHIP = { ok: 'up', fail: 'down', stale: 'warn', off: 'flat' };

export function verdictChip(kit, verdict) {
  return kit.chip(VERDICT_LABEL[verdict] || verdict, VERDICT_CHIP[verdict] || 'flat');
}

/** Cards with one dot per recent run (oldest -> newest). */
export function renderRings(kit, body, list) {
  const { h } = kit;
  if (!list.length) { body.appendChild(kit.empty('No worker has reported yet.')); return; }
  const grid = h('ul', { class: 'v-rings' });
  list.forEach(w => {
    const dots = h('span', { class: 'v-ring', role: 'img',
      'aria-label': w.runs.length ? w.runs.filter(r => r.ok !== false).length + ' of ' + w.runs.length + ' recent runs ok' : 'no recorded runs' });
    if (!w.runs.length) dots.appendChild(h('span', { class: 'v-dot v-dot--none' }));
    w.runs.forEach(r => dots.appendChild(h('span', {
      class: 'v-dot ' + (r.ok === false ? 'v-dot--bad' : 'v-dot--ok'),
      title: (r.ok === false ? 'FAIL ' : 'ok ') + fmtAgo(r.ts) + (r.note ? ' – ' + str(r.note).slice(0, 120) : ''),
    })));
    grid.appendChild(h('li', { class: 'v-ringcard v-ringcard--' + w.verdict },
      h('div', { class: 'v-ringcard__head' }, h('span', { class: 'v-mono v-strong' }, w.name), verdictChip(kit, w.verdict)),
      dots,
      h('div', { class: 'v-ringcard__meta v-muted' },
        'last ' + (w.last ? fmtAgo(w.last) : 'never'),
        w.errors ? ' · ' + w.errors + ' err' : '',
        w.nextRun ? ' · next ' + fmtIn(w.nextRun) : ''),
      w.note ? h('div', { class: 'v-ringcard__note v-muted', title: w.note }, w.note.slice(0, 110)) : null));
  });
  body.appendChild(grid);
}

/** Table: worker | state | last run | runs | errors | note. */
export function renderWorkerTable(kit, body, list) {
  const { h } = kit;
  if (!list.length) { body.appendChild(kit.empty('No worker has reported yet. Every background worker appends one run row per pass.')); return; }
  const rows = list.map(w => h('tr', { class: w.verdict === 'fail' || w.verdict === 'stale' ? 'v-row--' + w.verdict : null },
    h('th', { scope: 'row', class: 'v-mono' }, w.name),
    h('td', {}, verdictChip(kit, w.verdict)),
    h('td', {}, w.last ? fmtAgo(w.last) : 'never',
      w.verdict === 'stale' && w.intervalS ? h('span', { class: 'v-muted' }, ' (expected every ' + fmtSpan(w.intervalS) + ')') : null),
    h('td', { class: 'v-num' }, str(w.st.runs, '–')),
    h('td', { class: 'v-num' + (w.errors ? ' v-down' : '') }, w.errors === null ? '–' : String(w.errors)),
    h('td', { class: 'v-muted v-note', title: w.note }, w.note.slice(0, 140) || '–')));
  body.appendChild(kit.table('Background workers', [
    { label: 'Worker' }, { label: 'State' }, { label: 'Last run' }, { label: 'Runs', num: true },
    { label: 'Errors', num: true }, { label: 'Last note' }], rows));
}

