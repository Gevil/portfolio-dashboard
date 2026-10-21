// Practice-only "Forecaster" and "Candidates" cards (Overview, below the Agent card).
//  - Forecaster: worker/Claude status from /api/paper/status (`forecaster`, `claude`), the latest cohort and
//    its lane-vs-Claude source counts from /api/paper/forecasts, and the experiment from /api/paper/arms:
//    arm x horizon grading table, the 4 pre-registered comparisons (Holm-adjusted) and a short arm legend.
//  - Candidates: the newest cohort's arm-B forecasts sorted by p_20d (top 30). Selecting a row loads that
//    id's logged rows (/api/paper/forecasts?id=) and prints its frozen packet summary as plain text.
// Everything is textContent (no model-written markup reaches this file); polling is visibility-aware and is
// stopped, together with in-flight requests, by stop()/destroy().
import { el, fmtNum, fmtDateTime24, isNum } from '../util.js';
import { renderState, stamp, usePolling } from '../ui.js';
import { apiGet, latestRequest } from '../api.js';

const DASH = '\u2013';
const BOOK_Q = 'book=practice';
const FORECASTS_POLL_MS = 120000;   // cohorts are weekly/daily; progress itself arrives with the 30 s status poll
const ARMS_POLL_MS = 300000;        // grading runs a bootstrap server side: keep it rare
const TOP_N = 30;
const ARM_ORDER = ['R', 'Q', 'A', 'B', 'D', 'E'];

const ARM_LEGEND = [
  ['R', 'Reference: always 50 %. Skill scores are measured against it.'],
  ['Q', 'Code only: ranks names by 12-1 momentum and short-term reversal. No model call.'],
  ['A', 'One local-lane sample per name (no pooling).'],
  ['B', 'Pooled local-lane samples per name. The primary arm; the Candidates table and the PM use it.'],
  ['D', 'Blind Claude Opus forecast (no tools, no web).'],
  ['E', 'Blend of B and D.'],
];

const STATUS_LABEL = {
  pending: 'waiting for data',
  insufficient: 'waiting for data',
  sanity: 'sanity check (week 4)',
  interim: 'interim (week 8)',
  verdict: 'verdict (week 12)',
};
const STATUS_ICON = { pending: '\u2026', insufficient: '\u2026', sanity: '\u25d0', interim: '\u25d1', verdict: '\u2713' };
const COMPARISON_HELP = {
  'B-A': 'pooled vs a single sample',
  'B-Q': 'pooled vs code only',
  'D-B': 'Claude Opus vs pooled',
  'E-B': 'blend vs pooled',
};
const SOURCE_LABEL = { local: 'local', opus: 'opus', sonnet: 'sonnet', fallback: 'fallback' };
const SOURCE_ICON = { local: '\u25cf', opus: '\u25c6', sonnet: '\u25c7', fallback: '\u25b3' };

function th(text, cls) { return el('th', { scope: 'col', class: cls || null }, text); }
const obj = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const arr = v => (Array.isArray(v) ? v : []);
const num = (v, dec) => (isNum(v) ? fmtNum(v, dec) : DASH);
function signedNum(v, dec) {
  if (!isNum(v)) return DASH;
  const t = fmtNum(Math.abs(v), dec);
  return v > 0 ? `+${t}` : (v < 0 ? `\u2212${t}` : t);
}

/** Chip key of a forecast source: local lane, Claude opus/sonnet, anything else (hold, none, ...) = fallback. */
export function sourceKey(src) {
  const s = String(src || '').toLowerCase();
  return s === 'local' || s === 'opus' || s === 'sonnet' ? s : 'fallback';
}

function sourceChip(src) {
  const k = sourceKey(src);
  return el('span', { class: `fc-chip is-${k}`, dataset: { source: k }, title: src ? `source: ${src}` : 'no source' },
    el('span', { 'aria-hidden': 'true' }, `${SOURCE_ICON[k]} `), SOURCE_LABEL[k]);
}

/**
 * Newest arm-B row per id and horizon from /api/paper/forecasts items (newest first) ->
 * [{id, name, sector, p5, p20, spread, source, n, cohort, asOf}] sorted by p20 desc (ids without p20 last), top n.
 */
export function candidatesFrom(items, limit = TOP_N) {
  const by = new Map();
  for (const r of arr(items)) {
    if (!r || r.kind !== 'aggregate' || r.arm !== 'B' || !r.id || !isNum(r.p)) continue;
    const h = Number(r.horizon);
    if (h !== 5 && h !== 20) continue;
    const c = by.get(r.id) || { id: r.id, name: null, sector: null, p5: null, p20: null, spread: null, source: null, n: null, cohort: r.cohort || null, asOf: r.asOf || null, held: r.held === true };
    if (h === 5 && c.p5 == null) c.p5 = r.p;
    if (h === 20 && c.p20 == null) { c.p20 = r.p; c.spread = isNum(r.spread) ? r.spread : c.spread; c.source = r.source || c.source; c.n = r.n; }
    if (c.source == null && r.source) c.source = r.source;
    if (c.spread == null && isNum(r.spread)) c.spread = r.spread;
    if (!c.name && typeof r.name === 'string' && r.name) c.name = r.name;
    if (!c.sector && typeof r.sector === 'string' && r.sector) c.sector = r.sector;
    by.set(r.id, c);
  }
  const rows = [...by.values()];
  rows.sort((a, b) => {
    if (a.p20 == null && b.p20 == null) return String(a.id).localeCompare(String(b.id));
    if (a.p20 == null) return 1;
    if (b.p20 == null) return -1;
    return b.p20 - a.p20 || String(a.id).localeCompare(String(b.id));
  });
  return rows.slice(0, limit);
}

/** Source counts over arm-B 20d rows: {local, opus, sonnet, fallback, total}. */
export function sourceCounts(items) {
  const seen = new Set();
  const out = { local: 0, opus: 0, sonnet: 0, fallback: 0, total: 0 };
  for (const r of arr(items)) {
    if (!r || r.kind !== 'aggregate' || r.arm !== 'B' || Number(r.horizon) !== 20 || !r.id || seen.has(r.id)) continue;
    seen.add(r.id);
    out[sourceKey(r.source)] += 1;
    out.total += 1;
  }
  return out;
}

/** Plain-text packet summary of one candidate from its logged rows (newest first). */
export function packetSummary(cand, items) {
  const rows = arr(items).filter(r => r && r.id === cand.id && r.kind === 'aggregate' && (!cand.cohort || r.cohort === cand.cohort));
  const others = new Set(arr(items).filter(r => r && r.id === cand.id && r.cohort && r.cohort !== cand.cohort).map(r => r.cohort));
  const lines = [];
  lines.push(`${cand.id}${cand.name ? ` (${cand.name})` : ''}${cand.sector ? ` \u00b7 ${cand.sector}` : ''}`);
  const hash = (rows.find(r => r.packetHash) || {}).packetHash;
  lines.push(`Cohort ${cand.cohort || DASH}${cand.asOf ? `, as of ${cand.asOf}` : ''}`);
  lines.push(hash ? `Frozen packet: ${String(hash).slice(0, 16)} (the same packet fed every arm)` : 'Frozen packet: no hash logged');
  const order = a => { const i = ARM_ORDER.indexOf(a); return i < 0 ? 99 : i; };
  const sorted = rows.slice().sort((a, b) => order(a.arm) - order(b.arm) || Number(a.horizon) - Number(b.horizon) || String(a.source).localeCompare(String(b.source)));
  if (!sorted.length) lines.push('No logged forecasts for this cohort.');
  for (const r of sorted) {
    const parts = [`Arm ${r.arm} \u00b7 ${r.source || DASH} \u00b7 ${r.horizon}d: ${isNum(r.p) ? `p ${fmtNum(r.p, 3)}` : 'no p'}`];
    if (isNum(r.n)) parts.push(`n ${r.n}`);
    if (isNum(r.spread)) parts.push(`spread ${fmtNum(r.spread, 3)}`);
    if (r.model) parts.push(`model ${r.model}`);
    if (r.error) parts.push(`error ${r.error}`);
    lines.push(parts.join(' \u00b7 '));
  }
  if (others.size) lines.push(`Also logged in ${others.size} other cohort${others.size === 1 ? '' : 's'}.`);
  return lines;
}

// ------------------------------------------------------------------ status block

function claudeText(c) {
  const cl = obj(c);
  if (!Object.keys(cl).length) return { text: DASH, cls: 'muted' };
  if (!cl.enabled) return { text: 'Disabled', cls: 'muted' };
  if (cl.tokenPresent === false) return { text: 'Enabled, no token', cls: 'warn-ink' };
  const caps = obj(cl.caps);
  const c5 = isNum(cl.calls5h) ? `${cl.calls5h}${isNum(caps['5h']) ? `/${caps['5h']}` : ''}` : DASH;
  const cd = isNum(cl.callsDay) ? `${cl.callsDay}${isNum(caps.day) ? `/${caps.day}` : ''}` : DASH;
  const capped = (isNum(cl.calls5h) && isNum(caps['5h']) && cl.calls5h >= caps['5h']) || (isNum(cl.callsDay) && isNum(caps.day) && cl.callsDay >= caps.day);
  const nowS = Date.now() / 1000;
  const blocked = isNum(cl.blockedUntil) && cl.blockedUntil > nowS;
  const bits = [`Enabled \u00b7 5 h ${c5} \u00b7 day ${cd}`];
  if (blocked) bits.push(`blocked until ${fmtDateTime24(cl.blockedUntil)}`);
  else if (capped) bits.push('quota reached');
  return { text: bits.join(' \u00b7 '), cls: blocked || capped ? 'warn-ink' : '' };
}

function countsText(c) {
  if (!c || !c.total) return DASH;
  const bits = [`lane ${c.local}`];
  bits.push(`Claude ${c.opus + c.sonnet}${c.opus + c.sonnet ? ` (opus ${c.opus}, sonnet ${c.sonnet})` : ''}`);
  bits.push(`fallback hold ${c.fallback}`);
  return `${bits.join(' \u00b7 ')} of ${c.total}`;
}

function statRow(label, value, cls, id) {
  return el('div', { class: 'agent-row', id: id || null },
    el('span', { class: 'lbl' }, label),
    el('span', { class: `num agent-row-val ${cls || ''}`.trim() }, value));
}

/** Status grid. status = /api/paper/status payload (or null), fc = {cohort, counts}. */
export function buildForecasterStatus(status, fc) {
  const st = obj(status);
  const w = obj(st.forecaster);
  const job = obj(w.job);
  const prog = obj(job.progress);
  const cohort = fc && fc.cohort ? fc.cohort : null;
  const rows = [];
  const hasWorker = Object.keys(w).length > 0;
  rows.push(statRow('Forecaster', !hasWorker ? DASH : (w.enabled ? (job.running ? 'Enabled \u00b7 running' : 'Enabled') : 'Disabled'),
    hasWorker && !w.enabled ? 'muted' : '', 'fc-enabled'));
  rows.push(statRow('Cohort', cohort
    ? `${cohort.id}${cohort.kind ? ` (${cohort.kind})` : ''} \u00b7 ${cohort.status || 'unknown'}${cohort.asOf ? ` \u00b7 ${cohort.asOf}` : ''}`
    : (fc && fc.loaded ? 'None yet' : DASH), '', 'fc-cohort'));
  let progress;
  if (job.running) {
    const total = isNum(prog.total) ? prog.total : 0;
    const done = isNum(prog.done) ? prog.done : 0;
    progress = el('span', { class: 'fc-progress' },
      `${prog.phase || 'working'} ${done}/${total}${prog.laneWaiting ? ' \u00b7 waiting for the lane' : ''} `,
      total > 0 ? el('progress', { max: String(total), value: String(Math.min(done, total)), 'aria-label': `Cohort ${job.cohort || ''} progress` }) : null);
  } else if (cohort) {
    progress = `${isNum(cohort.ids) ? `${cohort.ids} names \u00b7 ` : ''}${cohort.status === 'done' ? 'complete' : (cohort.status || 'idle')}`;
  } else {
    progress = 'idle';
  }
  rows.push(statRow('Progress', progress, '', 'fc-progress-row'));
  rows.push(statRow('Lane vs Claude (B, 20d)', countsText(fc && fc.counts), '', 'fc-sources'));
  const cl = claudeText(st.claude);
  rows.push(statRow('Claude', cl.text, cl.cls, 'fc-claude'));
  const out = [el('div', { class: 'agent-grid fc-grid', id: 'fc-status' }, rows)];
  if (hasWorker && w.lastError) out.push(el('p', { class: 'agent-note warn-ink', role: 'status', id: 'fc-error' }, `Last worker error: ${String(w.lastError)}`));
  if (st.universeSource && st.universeSource !== 'paper_universe') {
    out.push(el('p', { class: 'agent-note muted' }, 'The practice buckets still trade the watchlist universe; forecasts feed the open universe only.'));
  }
  return out;
}

// ------------------------------------------------------------------ experiment

function cellsTable(arms) {
  const cells = arr(arms.cells);
  const armIds = arr(arms.arms).length ? arms.arms : ARM_ORDER;
  const horizons = arr(arms.horizons).length ? arms.horizons : [5, 20];
  const body = [];
  for (const arm of armIds) {
    for (const h of horizons) {
      const hit = cells.filter(c => c && c.arm === arm && Number(c.horizon) === Number(h));
      if (!hit.length) {
        body.push(el('tr', { dataset: { arm, horizon: String(h) } },
          el('th', { scope: 'row' }, arm), el('td', {}, `${h}d`), el('td', {}, DASH),
          el('td', { class: 'num' }, '0'), el('td', { class: 'num' }, DASH), el('td', { class: 'num' }, DASH), el('td', { class: 'num' }, DASH)));
        continue;
      }
      for (const c of hit) {
        body.push(el('tr', { dataset: { arm, horizon: String(h), source: c.source || '' } },
          el('th', { scope: 'row' }, arm), el('td', {}, `${h}d`),
          el('td', {}, c.source ? sourceChip(c.source) : DASH),
          el('td', { class: 'num' }, isNum(c.n) ? String(c.n) : '0'),
          el('td', { class: 'num' }, num(c.brier, 4)),
          el('td', { class: 'num' }, signedNum(c.bss, 3)),
          el('td', { class: 'num' }, signedNum(c.ic, 3))));
      }
    }
  }
  return el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table', id: 'fc-arms-table' },
    el('caption', { class: 'visually-hidden' }, 'Forecast grading by arm and horizon'),
    el('thead', {}, el('tr', {}, th('Arm'), th('Horizon'), th('Source'), th('n', 'num'), th('Brier', 'num'), th('Skill vs R', 'num'), th('IC', 'num'))),
    el('tbody', {}, body)));
}

function comparisonRow(c) {
  const status = String(c.status || 'pending');
  const label = STATUS_LABEL[status] || status;
  const ci = arr(c.ci95);
  const verdict = c.significant === true
    ? `significant, favours ${c.favours || DASH}`
    : (c.significant === false ? 'not significant' : null);
  const holm = c.primary
    ? (isNum(c.pHolm) ? `${fmtNum(c.pHolm, 3)}${verdict ? ` \u00b7 ${verdict}` : ''}` : DASH)
    : 'secondary';
  return el('tr', { dataset: { comparison: c.id || '', horizon: String(c.horizon), status } },
    el('th', { scope: 'row' }, `${c.first} vs ${c.second}`, el('span', { class: 'muted fc-help' }, ` ${COMPARISON_HELP[c.id] || ''}`)),
    el('td', {}, `${c.horizon}d${c.primary ? ' (primary)' : ''}`),
    el('td', { class: 'num' }, `${isNum(c.n) ? c.n : 0} / ${isNum(c.cohorts) ? c.cohorts : 0}`),
    el('td', { class: 'num' }, signedNum(c.meanDeltaBrier, 4)),
    el('td', { class: 'num' }, ci.length === 2 && isNum(ci[0]) && isNum(ci[1]) ? `${signedNum(ci[0], 4)} to ${signedNum(ci[1], 4)}` : DASH),
    el('td', {}, holm),
    el('td', {}, el('span', { class: `fc-chip is-status-${status}` }, el('span', { 'aria-hidden': 'true' }, `${STATUS_ICON[status] || '?'} `), label)));
}

function comparisonsTable(arms) {
  const rows = arr(arms.comparisons).filter(Boolean);
  return el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table', id: 'fc-comparisons-table' },
    el('caption', { class: 'visually-hidden' }, 'The four pre-registered paired comparisons, change in Brier score'),
    el('thead', {}, el('tr', {}, th('Comparison'), th('Horizon'), th('Pairs / weeks', 'num'), th('\u0394 Brier', 'num'), th('95% CI', 'num'), th('Holm-adjusted p'), th('Status'))),
    el('tbody', {}, rows.map(comparisonRow))));
}

/** Experiment block from the /api/paper/arms payload. */
export function buildExperiment(arms) {
  const a = obj(arms);
  const exp = obj(a.experiment);
  const ro = obj(a.readout);
  const out = [];
  const week = isNum(ro.currentWeek) ? ro.currentWeek : (isNum(exp.cohorts) ? exp.cohorts : 0);
  const next = obj(ro.next);
  const facts = [`Week ${week} of the experiment`];
  if (isNum(next.week)) facts.push(`next readout: ${next.kind || 'check'} at week ${next.week}`);
  else if (week > 0) facts.push('all scheduled readouts reached');
  facts.push(`${arr(exp.maturedCohorts5d).length} cohort${arr(exp.maturedCohorts5d).length === 1 ? '' : 's'} with 5-day labels`);
  out.push(el('p', { class: 'agent-note', id: 'fc-readout' }, `${facts.join(' \u00b7 ')}.`));
  if (exp.underpowered) out.push(el('p', { class: 'agent-note warn-ink', role: 'status' }, 'Too many cohorts used the Claude fallback: results are underpowered and mixed cohorts are graded apart.'));
  out.push(el('h4', { class: 'agent-minititle' }, 'Grading by arm and horizon'), cellsTable(a));
  out.push(el('p', { class: 'agent-note muted' }, 'Pure cohorts only; a source is never pooled with another. Skill vs R = 1 \u2212 Brier / Brier of 50 %. IC = rank correlation with the realised 5/20-day outcome.'));
  out.push(el('h4', { class: 'agent-minititle' }, 'Pre-registered comparisons'), comparisonsTable(a));
  out.push(el('p', { class: 'agent-note muted' }, '\u0394 Brier = first minus second (negative = the first arm is better). Holm adjustment covers the four 5-day comparisons; the 20-day rows are secondary.'));
  out.push(el('h4', { class: 'agent-minititle' }, 'Arms'),
    el('dl', { class: 'fc-legend', id: 'fc-legend' }, ARM_LEGEND.map(([k, t]) => el('div', { class: 'fc-legend-row' }, el('dt', {}, k), el('dd', {}, t)))));
  return out;
}

// ------------------------------------------------------------------ panel

/**
 * createForecasterPanel({heldIds: () => Set<string>}) ->
 *   {forecasterNode, candidatesNode, update(status), repaint(), stop(), destroy()}
 * The caller owns visibility (hidden attribute) and calls stop() when Practice is left.
 */
export function createForecasterPanel({ heldIds = () => new Set() } = {}) {
  const stampHost = el('span', { class: 'fc-stamp' });
  const fcBody = el('div', { class: 'fc-body', id: 'fc-body' });
  const forecasterNode = el('section', { class: 'card forecaster-card', id: 'forecaster-card', 'aria-labelledby': 'forecaster-title', hidden: true },
    el('div', { class: 'card-head' }, el('h2', { class: 'card-title', id: 'forecaster-title' }, 'Forecaster'), el('div', { class: 'card-tools' }, stampHost)),
    fcBody);
  const candBody = el('div', { class: 'cand-body', id: 'candidates-body' });
  const packetHost = el('div', { class: 'cand-packet', id: 'candidate-packet', 'aria-live': 'polite' });
  const candidatesNode = el('section', { class: 'card candidates-card', id: 'candidates-card', 'aria-labelledby': 'candidates-title', hidden: true },
    el('div', { class: 'card-head' }, el('h2', { class: 'card-title', id: 'candidates-title' }, 'Candidates')),
    candBody, packetHost);

  const fcReq = latestRequest();
  const armsReq = latestRequest();
  const packetReq = latestRequest();
  let stops = [];
  let status = null;
  let fc = null;            // {cohort, items, loaded}
  let fcError = null;
  let fcDisabled = false;
  let arms = null;
  let armsError = null;
  let armsDisabled = false;
  let ts = null;
  let selected = null;      // candidate id
  let packet = null;        // {id, lines} | {id, error} | {id, loading}
  let sig = '';
  let candSig = '';

  const isDisabled = res => res.status === 503 || /disabled/i.test(String(res.error || ''));

  async function loadForecasts() {
    const t = fcReq.begin();
    const res = await apiGet(`/api/paper/forecasts?${BOOK_Q}&limit=2000`, { signal: t.signal, timeoutMs: 30000 });
    if (!t.current()) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) {
      const d = res.data;
      const newest = arr(d.cohorts)[0] || null;
      const cohort = (d.cohort && arr(d.cohorts).find(c => c.id === d.cohort)) || newest;
      fc = { cohort, items: d.items, loaded: true, cohortId: d.cohort || (newest && newest.id) || null };
      fcError = null; fcDisabled = false; ts = Date.now();
    } else if (res.aborted) {
      return;
    } else {
      fcError = res.error || 'invalid response';
      fcDisabled = isDisabled(res);
    }
    paint();
  }

  async function loadArms() {
    const t = armsReq.begin();
    const res = await apiGet(`/api/paper/arms?${BOOK_Q}`, { signal: t.signal, timeoutMs: 60000 });
    if (!t.current()) return;
    if (res.ok && res.data && typeof res.data === 'object') {
      arms = res.data; armsError = null; armsDisabled = false; ts = Date.now();
    } else if (res.aborted) {
      return;
    } else {
      armsError = res.error || 'invalid response';
      armsDisabled = isDisabled(res);
    }
    paint();
  }

  async function loadPacket(id) {
    const cand = candidates().find(c => c.id === id);
    if (!cand) return;
    const t = packetReq.begin();
    packet = { id, loading: true };
    paintPacket();
    const q = `${BOOK_Q}&id=${encodeURIComponent(id)}&limit=200`;
    const res = await apiGet(`/api/paper/forecasts?${q}`, { signal: t.signal, timeoutMs: 30000 });
    if (!t.current()) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) packet = { id, lines: packetSummary(cand, res.data.items) };
    else if (res.aborted) return;
    else packet = { id, error: res.error || 'invalid response' };
    paintPacket();
  }

  function candidates() {
    return fc ? candidatesFrom(fc.items) : [];
  }

  function select(id) {
    selected = id;
    packet = null;
    candSig = '';
    paintCandidates();
    loadPacket(id);
  }

  // ---------------------------------------------------------------- painting
  function paintForecaster() {
    const kids = [];
    kids.push(...buildForecasterStatus(status, fc ? { cohort: fc.cohort, counts: sourceCounts(fc.items), loaded: true } : { loaded: false }));
    if (fcError && !fc) kids.push(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Cohorts unavailable: ${fcError}`));
    else if (fcError) kids.push(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Cohort refresh failed, showing older data: ${fcError}`));
    const exp = el('div', { class: 'fc-experiment', id: 'fc-experiment' });
    kids.push(el('h3', { class: 'agent-subtitle' }, 'Experiment'), exp);
    if (!arms) {
      if (armsDisabled) renderState(exp, { empty: 'The forecaster is disabled, so there is no experiment to grade.' });
      else if (armsError) renderState(exp, { error: `Experiment unavailable: ${armsError}`, retry: () => loadArms() });
      else renderState(exp, { loading: 'Loading experiment' });
    } else {
      exp.replaceChildren(...buildExperiment(arms));
      if (armsError) exp.append(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${armsError}`));
    }
    fcBody.replaceChildren(...kids);
    fcBody.removeAttribute('aria-busy');
    stampHost.replaceChildren(ts ? stamp(ts, { stale: !!(fcError || armsError) }) : '');
  }

  function paintCandidates() {
    const held = heldIds();
    const list = candidates();
    const s = JSON.stringify([list, [...held].sort(), selected, fcError, fcDisabled, !!fc]);
    if (s === candSig) return;
    candSig = s;
    const hadFocus = candBody.contains(document.activeElement);
    if (!fc) {
      if (fcDisabled) renderState(candBody, { empty: 'The forecaster is disabled, so there are no candidates.' });
      else if (fcError) renderState(candBody, { error: `Candidates unavailable: ${fcError}`, retry: () => loadForecasts() });
      else renderState(candBody, { loading: 'Loading candidates' });
      return;
    }
    if (!list.length) {
      candBody.replaceChildren(el('div', { class: 'state state-empty', id: 'candidates-empty' },
        el('div', { class: 'state-text' }, 'No forecasts yet \u2014 the first cohort is created on the weekly schedule.')));
      candBody.removeAttribute('aria-busy');
      return;
    }
    const cohortId = fc.cohortId || (list[0] && list[0].cohort) || DASH;
    const rows = list.map(c => {
      const isHeld = held.has(String(c.id).toUpperCase()) || c.held;
      const sel = selected === c.id;
      return el('tr', { class: `cand-row${sel ? ' is-selected' : ''}`, dataset: { id: c.id }, onclick: () => select(c.id) },
        el('th', { scope: 'row', class: 'c-name' }, el('button', {
          type: 'button', class: 'pos-btn cand-btn', dataset: { id: c.id }, 'aria-current': sel ? 'true' : null,
          'aria-label': `${c.name || c.id}, 20-day probability ${isNum(c.p20) ? fmtNum(c.p20 * 100, 1) + ' percent' : 'unknown'}. Show packet summary`,
          onclick: ev => { ev.stopPropagation(); select(c.id); },
        }, el('span', { class: 'pos-name' }, c.name || c.id), c.name ? el('span', { class: 'pos-meta' }, c.id) : null)),
        el('td', { class: 'cand-sector' }, c.sector || DASH),
        el('td', { class: 'num' }, isNum(c.p5) ? `${fmtNum(c.p5 * 100, 1)}%` : DASH),
        el('td', { class: 'num' }, isNum(c.p20) ? `${fmtNum(c.p20 * 100, 1)}%` : DASH),
        el('td', { class: 'num' }, isNum(c.spread) ? fmtNum(c.spread, 3) : DASH),
        el('td', {}, sourceChip(c.source)),
        el('td', {}, isHeld ? el('span', { class: 'fc-chip is-held', title: 'Held by a practice bucket' }, el('span', { 'aria-hidden': 'true' }, '\u25cf '), 'held') : ''));
    });
    candBody.replaceChildren(
      el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table cand-table', id: 'candidates-table' },
        el('caption', { class: 'visually-hidden' }, `Top ${list.length} forecasts of cohort ${cohortId} by 20-day probability`),
        el('thead', {}, el('tr', {}, th('Name'), th('Sector'), th('p 5d', 'num'), th('p 20d', 'num'), th('Spread', 'num'), th('Source'), th('Held'))),
        el('tbody', {}, rows))),
      el('p', { class: 'agent-note muted', id: 'candidates-note' },
        `Cohort ${cohortId}${fc.cohort && fc.cohort.asOf ? `, as of ${fc.cohort.asOf}` : ''}. Arm B probability that the name beats the universe median; top ${TOP_N} by 20 days. Spread = disagreement between samples.`));
    candBody.removeAttribute('aria-busy');
    if (hadFocus && document.activeElement === document.body) {
      const again = candBody.querySelector(selected ? `.cand-btn[data-id="${CSS.escape(selected)}"]` : '.cand-btn');
      if (again) again.focus();
    }
  }

  function paintPacket() {
    if (!selected || !packet) { packetHost.replaceChildren(); return; }
    const head = el('h3', { class: 'agent-subtitle', id: 'candidate-packet-title' }, `Packet summary \u2014 ${packet.id}`);
    const body = el('div', { class: 'cand-packet-body' });
    if (packet.loading) renderState(body, { loading: 'Loading packet summary' });
    else if (packet.error) renderState(body, { error: `Packet summary unavailable: ${packet.error}`, retry: () => loadPacket(packet.id) });
    else body.append(el('div', { class: 'cand-packet-text', id: 'candidate-packet-text' }, packet.lines.map(l => el('p', {}, l))));
    packetHost.replaceChildren(head, body);
  }

  function paint() {
    paintForecaster();
    paintCandidates();
    paintPacket();
  }

  // ---------------------------------------------------------------- lifecycle
  function start() {
    if (stops.length) return;
    paint();
    stops = [usePolling(loadForecasts, FORECASTS_POLL_MS, { immediate: true }), usePolling(loadArms, ARMS_POLL_MS, { immediate: true })];
  }

  function stop() {
    stops.forEach(fn => fn());
    stops = [];
    fcReq.abort(); armsReq.abort(); packetReq.abort();
    status = null; fc = null; fcError = null; fcDisabled = false; arms = null; armsError = null; armsDisabled = false;
    ts = null; selected = null; packet = null; sig = ''; candSig = '';
    fcBody.replaceChildren(); candBody.replaceChildren(); packetHost.replaceChildren(); stampHost.replaceChildren();
  }

  /** status: the /api/paper/status payload (null while it loads). Starts polling on first call. */
  function update(next) {
    const s = JSON.stringify([next && next.forecaster, next && next.claude, next && next.universeSource]);
    const changed = s !== sig;
    status = next || null;
    sig = s;
    if (!stops.length) { start(); return; }
    if (changed) paintForecaster();
    paintCandidates();
  }

  /** Re-render the candidates (held markers change with the portfolio payload). */
  function repaint() { if (stops.length) paintCandidates(); }

  return { forecasterNode, candidatesNode, update, repaint, stop, destroy: stop };
}
