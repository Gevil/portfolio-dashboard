/* Digest view: latest advice per ticker, what changed, history, scoreboard v2,
   run-now, failed/skipped. export function mount(root, ctx) -> unmount() */
import { createKit, isObj, num, str, toSec, fmtAgo, fmtIn, fmtDay, fmtWhen, fmtNum, fmtPct, tone, errText } from './_kit.js';
import { openReport } from './_report.js';

const RATING_CHIP = { BUY: 'up', OVERWEIGHT: 'up', SELL: 'down', UNDERWEIGHT: 'down', HOLD: 'info', WATCH: 'info' };
const VERDICT_CHIP = { hit: 'up', miss: 'down', neutral: 'flat' };
const META_KEYS = new Set(['failed', 'failures', 'skipped', 'meta', 'errors', 'history', 'generated_at', 'ts', 'advice']);

/* ---------- advice normalisation (the /api/advice shape is evolving) ---------- */

function confidenceText(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  if (Number.isFinite(n)) return n <= 1 ? Math.round(n * 100) + '%' : String(Math.round(n * 10) / 10);
  return String(v);
}

function normAdvice(d) {
  const root = isObj(d) && isObj(d.advice) ? d.advice : (isObj(d) ? d : {});
  const items = [];
  Object.entries(root).forEach(([sym, v]) => {
    if (!isObj(v) || (META_KEYS.has(sym) && !('rating' in v))) return;
    const hist = Array.isArray(v.history) ? v.history.filter(isObj) : [];
    items.push({
      sym,
      rating: str(v.rating, 'OTHER').toUpperCase(),
      action: v.action || null,
      score: num(v.score),
      confidence: confidenceText(v.confidence),
      lane: v.lane || null,
      model: v.model || v.model_id || null,
      fallback: v.fallback === true || v.lane_role === 'fallback' || v.role === 'fallback',
      price: num(v.priceAtAdvice ?? v.price_at_advice),
      excerpt: str(v.excerpt || v.summary).trim(),
      report: v.report || null,
      ts: toSec(v.ts) || toSec(v.date),
      date: v.date || null,
      prev: isObj(v.previous) ? v.previous : isObj(v.prev) ? v.prev : (hist[1] || null),
      history: hist,
    });
  });
  items.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  const listOf = (x) => (Array.isArray(x) ? x : isObj(x) ? Object.entries(x).map(([k, val]) => ({ ticker: k, reason: isObj(val) ? (val.reason || val.error || val.message) : val })) : []);
  const top = isObj(d) ? d : {};
  return {
    items,
    failed: listOf(top.failed || top.failures || top.errors),
    skipped: listOf(top.skipped),
  };
}

/** scoreboard entries -> {SYM: [entry newest-first]} */
function timelineBySymbol(score) {
  const out = {};
  const entries = score && Array.isArray(score.entries) ? score.entries : [];
  entries.forEach(e => {
    if (!isObj(e) || !e.ticker) return;
    (out[e.ticker] = out[e.ticker] || []).push(e);
  });
  Object.values(out).forEach(a => a.sort((x, y) => (toSec(y.ts) || 0) - (toSec(x.ts) || 0)));
  return out;
}

function previousOf(item, timeline) {
  if (item.prev) return item.prev;
  const rows = timeline[item.sym] || [];
  const mine = item.ts;
  return rows.find(r => mine === null || (toSec(r.ts) || 0) < mine - 1) || null;
}

/* ---------- scoreboard cell helpers ---------- */

function wilsonOf(cell) {
  let lo = null, hi = null;
  const w = cell.wilson ?? cell.wilson95 ?? cell.ci ?? cell.interval;
  if (Array.isArray(w)) { lo = num(w[0]); hi = num(w[1]); }
  else if (isObj(w)) { lo = num(w.lo ?? w.low ?? w.lower); hi = num(w.hi ?? w.high ?? w.upper); }
  else {
    lo = num(cell.wilson_lo ?? cell.wilsonLo ?? cell.wilsonLoPct ?? cell.wilsonLowPct ?? cell.ciLowPct);
    hi = num(cell.wilson_hi ?? cell.wilsonHi ?? cell.wilsonHiPct ?? cell.wilsonHighPct ?? cell.ciHighPct);
  }
  if (lo === null || hi === null) return null;
  const hr = num(cell.hitRatePct);
  if (hi <= 1 && (hr === null || hr > 1)) { lo *= 100; hi *= 100; }
  return [Math.max(0, lo), Math.min(100, hi)];
}
function gradedOf(c) {
  const g = num(c.graded ?? c.n_graded ?? c.gradedN);
  if (g !== null) return g;
  const parts = [c.hits, c.misses, c.neutral].map(num);
  if (parts.every(p => p !== null)) return parts.reduce((a, b) => a + b, 0);
  return num(c.n ?? c.advice);
}
function isCell(v) {
  return isObj(v) && ('n' in v || 'hitRatePct' in v || 'hits' in v || 'advice' in v);
}
/** flatten {k: cell} or {k: {k2: cell}} into [{label, cell}] */
function flattenCells(map, prefix = '') {
  const out = [];
  if (!isObj(map)) return out;
  Object.keys(map).sort().forEach(k => {
    const v = map[k];
    if (isCell(v)) out.push({ label: prefix + k, cell: v });
    else if (isObj(v)) out.push(...flattenCells(v, prefix + k + ' · '));
  });
  return out;
}

/* ---------- mount ---------- */

export function mount(root, ctx) {
  const kit = createKit(ctx, root);
  const { h, chip } = kit;
  const grid = h('div', { class: 'v-grid v-digest' });
  root.appendChild(grid);

  const labelOf = (sym) => {
    try {
      const p = ctx.store.get('portfolio');
      const hit = p && Array.isArray(p.positions) && p.positions.find(x => x.id === sym);
      return hit && hit.label ? hit.label : sym;
    } catch (_) { return sym; }
  };

  const scoreSrc = kit.memo(() => kit.get('/api/scoreboard'));
  const adviceSrc = kit.memo(() => kit.get('/api/advice'));
  const opsSrc = kit.memo(async () => {
    const [bg, jobs, runs] = await Promise.all([kit.get('/api/background'), kit.get('/api/jobs?limit=20'), kit.get('/api/worker-runs?limit=100')]);
    return { bg, jobs, runs };
  }, 800);

  /* ---------------- run control ---------------- */
  let notice = '';
  const runBtn = h('button', { type: 'button', class: 'v-btn v-btn--primary' }, 'Run digest now');
  const runMsg = h('p', { class: 'v-muted v-runmsg', role: 'status', 'aria-live': 'polite' });
  const run = kit.section({ title: 'Digest run', id: 'run', cls: 'v-span-2', actions: [runBtn] });
  let runState = { disabled: true, why: 'checking…' };
  const applyRunState = () => {
    runBtn.disabled = runState.disabled;
    runBtn.title = runState.why || '';
    runMsg.textContent = notice || runState.why || '';
  };
  runBtn.addEventListener('click', async () => {
    if (runBtn.disabled) return;
    runBtn.disabled = true;
    notice = 'starting…';
    applyRunState();
    const r = await ctx.api.apiSend('POST', '/api/digest/run');
    if (kit.isDisposed()) return;
    const msg = str((r.data && r.data.message) || '') || errText(r.error);
    if (r.ok) {
      notice = msg || 'digest batch started';
      ctx.ui.toast(notice, { type: 'ok', title: 'Digest' });
    } else if (r.status === 409) {
      notice = msg || 'a digest batch is already in flight'; // the lane is serialised: show the server's words, never re-fire
      ctx.ui.toast(notice, { type: 'warn', title: 'Digest already running' });
    } else {
      notice = 'not started: ' + msg;
      ctx.ui.toast(msg, { type: 'error', title: 'Digest not started' });
    }
    applyRunState();
    run.reload();
  });

  kit.autoLoad(run, {
    source: async () => {
      const o = await opsSrc();
      if (!o.bg.ok) return { ok: false, error: o.bg.error, status: o.bg.status };
      return { ok: true, stale: o.bg.stale, data: { d: isObj(o.bg.data) ? o.bg.data.digest : null, jobs: o.jobs.ok && Array.isArray(o.jobs.data) ? o.jobs.data : [] } };
    },
    isEmpty: () => false,
    render: (body, { d, jobs }) => {
      const inflightJob = jobs.find(j => j.source === 'digest' && (j.status === 'queued' || j.status === 'running'));
      if (!isObj(d)) {
        runState = { disabled: true, why: 'digest status unavailable' };
        body.appendChild(kit.empty('The digest worker did not report /api/background.'));
        body.appendChild(runMsg); applyRunState();
        return;
      }
      if (d.enabled === false) runState = { disabled: true, why: 'digest disabled (DIGEST_ENABLED != 1)' };
      else if (d.manual_running) runState = { disabled: true, why: 'manual batch in flight' };
      else if (inflightJob) runState = { disabled: true, why: 'digest batch in flight – ' + inflightJob.ticker + ' ' + inflightJob.status };
      else runState = { disabled: false, why: '' };
      body.appendChild(h('div', { class: 'v-lanehead' },
        chip(d.enabled === false ? 'disabled' : d.running ? 'scheduled loop alive' : 'loop stopped', d.enabled === false ? 'flat' : d.running ? 'up' : 'down'),
        h('span', { class: 'v-muted' }, (d.runs || 0) + ' tickers done · ' + (d.errors || 0) + ' errors · ' + (d.last_run ? 'last batch ' + fmtAgo(d.last_run) : 'no batch yet') + ' · next ' + (d.next_run ? fmtIn(d.next_run) : 'not scheduled'))));
      if (d.skip_reason) body.appendChild(h('p', { class: 'v-callout v-callout--warn', role: 'status' }, 'Last batch deferred: ' + d.skip_reason));
      body.appendChild(runMsg);
      applyRunState();
    },
  }, 20000);

  /* ---------------- advice cards ---------------- */
  const advice = kit.section({ title: 'Latest advice', id: 'advice', cls: 'v-span-2' });
  kit.autoLoad(advice, {
    source: async () => {
      const [a, s] = await Promise.all([adviceSrc(), scoreSrc()]);
      if (!a.ok) return { ok: false, error: a.error, status: a.status };
      return { ok: true, stale: a.stale, data: { advice: normAdvice(a.data), score: s.ok ? s.data : null } };
    },
    isEmpty: d => !d.advice.items.length,
    emptyText: 'No tracked advice yet. The scheduled digest fills this in after its first run.',
    render: (body, { advice: a, score }) => {
      const timeline = timelineBySymbol(score);
      const list = h('ul', { class: 'v-cards' });
      a.items.forEach(it => list.appendChild(adviceCard(it, timeline)));
      body.appendChild(list);
    },
  }, 60000);

  function adviceCard(it, timeline) {
    const prev = previousOf(it, timeline);
    const hist = it.history.length ? it.history : (timeline[it.sym] || []);
    const meta = [
      it.action ? chip(str(it.action).toUpperCase(), 'flat', 'recommended action') : null,
      it.score !== null ? h('span', { class: 'v-kvp', title: 'score' }, h('span', { class: 'v-muted' }, 'score '), fmtNum(it.score, Number.isInteger(it.score) ? 0 : 1)) : null,
      it.confidence ? h('span', { class: 'v-kvp', title: 'confidence' }, h('span', { class: 'v-muted' }, 'conf '), it.confidence) : null,
      it.price !== null ? h('span', { class: 'v-kvp', title: 'price when the advice was given' }, h('span', { class: 'v-muted' }, '@ '), fmtNum(it.price, 2) + ' €') : null,
    ];
    const prov = it.lane || it.model ? h('p', { class: 'v-muted v-card__prov' }, 'answered by ' + [it.lane, it.model].filter(Boolean).join(' · '), it.fallback ? chip('fallback lane', 'warn') : null) : null;

    let changed;
    if (prev) {
      const pr = str(prev.rating, '?').toUpperCase();
      const ps = num(prev.score);
      const parts = [];
      if (pr !== it.rating) parts.push(h('span', { class: 'v-change v-change--' + (RATING_CHIP[it.rating] || 'flat') }, pr + ' → ' + it.rating));
      else parts.push(h('span', { class: 'v-muted' }, 'rating unchanged (' + it.rating + ')'));
      if (prev.action && it.action && String(prev.action).toLowerCase() !== String(it.action).toLowerCase()) parts.push(h('span', {}, 'action ' + prev.action + ' → ' + it.action));
      if (ps !== null && it.score !== null && ps !== it.score) parts.push(h('span', { class: 'v-' + tone(it.score - ps) }, 'score ' + fmtNum(ps, 1) + ' → ' + fmtNum(it.score, 1)));
      if (str(prev.excerpt).trim() && it.excerpt && str(prev.excerpt).trim() !== it.excerpt) parts.push(h('span', { class: 'v-muted' }, 'reasoning text changed'));
      changed = h('p', { class: 'v-card__changed' }, h('span', { class: 'v-label' }, 'Since ' + fmtDay(prev.date || prev.ts) + ': '), ...parts.flatMap((p, i) => (i ? [' · ', p] : [p])));
    } else {
      changed = h('p', { class: 'v-muted v-card__changed' }, 'First tracked advice for this ticker.');
    }

    const li = h('li', { class: 'v-card v-card--' + (RATING_CHIP[it.rating] || 'flat') },
      h('div', { class: 'v-card__head' },
        h('h3', { class: 'v-card__title' }, h('span', { class: 'v-mono' }, it.sym), labelOf(it.sym) !== it.sym ? h('span', { class: 'v-muted' }, ' ' + labelOf(it.sym)) : null),
        chip(it.rating, RATING_CHIP[it.rating] || 'flat'),
        h('span', { class: 'v-muted v-card__age', title: it.ts ? fmtWhen(it.ts) : '' }, it.ts ? fmtAgo(it.ts) : str(it.date, '–'))),
      h('div', { class: 'v-card__meta' }, ...meta),
      prov, changed,
      h('p', { class: 'v-card__excerpt' }, it.excerpt || 'No excerpt recorded.'));
    const actions = h('div', { class: 'v-card__actions' });
    if (it.report) {
      const b = h('button', { type: 'button', class: 'v-btn v-btn--sm', 'aria-label': 'Open full report for ' + it.sym }, 'Full report');
      b.addEventListener('click', () => openReport(kit, it.report, it.sym + ' · ' + it.rating));
      actions.appendChild(b);
    }
    if (actions.childNodes.length) li.appendChild(actions);
    if (hist.length > 1) li.appendChild(timelineNode(it.sym, hist));
    return li;
  }

  function timelineNode(sym, hist) {
    const rows = hist.slice(0, 10);
    const strip = h('ol', { class: 'v-timeline', 'aria-label': 'Advice history for ' + sym + ', newest first' });
    rows.forEach(r => {
      const rating = str(r.rating, '?').toUpperCase();
      const verdict = r.verdict || r.verdictT5;
      strip.appendChild(h('li', { class: 'v-tl v-tl--' + (RATING_CHIP[rating] || 'flat') },
        h('span', { class: 'v-tl__date' }, fmtDay(r.date || r.ts)),
        chip(rating, RATING_CHIP[rating] || 'flat'),
        r.action ? h('span', { class: 'v-muted' }, str(r.action)) : null,
        num(r.score) !== null ? h('span', { class: 'v-muted' }, 'score ' + fmtNum(r.score, 1)) : null,
        verdict ? chip('T+20 ' + verdict, VERDICT_CHIP[verdict] || 'flat') : null));
    });
    return h('details', { class: 'v-history' }, h('summary', {}, 'History (' + hist.length + ')'), strip);
  }

  /* ---------------- skipped / failed ---------------- */
  const issues = kit.section({ title: 'Failed & skipped', id: 'issues', cls: 'v-span-2' });
  kit.autoLoad(issues, {
    source: async () => {
      const [o, a, s] = await Promise.all([opsSrc(), adviceSrc(), scoreSrc()]);
      if (!o.bg.ok && !a.ok) return { ok: false, error: o.bg.error || a.error, status: o.bg.status };
      return { ok: true, data: { bg: o.bg.ok ? o.bg.data : null, runs: o.runs.ok && Array.isArray(o.runs.data) ? o.runs.data : [], advice: a.ok ? normAdvice(a.data) : null, score: s.ok ? s.data : null } };
    },
    isEmpty: () => false,
    render: (body, d) => {
      const rows = [];
      const dg = d.bg && d.bg.digest;
      if (dg && dg.skip_reason) rows.push({ who: 'whole batch', kind: 'deferred', why: dg.skip_reason, when: dg.last_run });
      if (d.advice) {
        d.advice.failed.forEach(f => rows.push({ who: str(f.ticker || f.symbol || f.id, '?'), kind: 'failed', why: str(f.reason || f.error || f.message, 'no reason recorded'), when: f.ts }));
        d.advice.skipped.forEach(f => rows.push({ who: str(f.ticker || f.symbol || f.id, '?'), kind: 'skipped', why: str(f.reason || f.error || f.message, 'no reason recorded'), when: f.ts }));
      }
      d.runs.filter(r => r.worker === 'digest' && r.ok === false).slice(0, 8)
        .forEach(r => rows.push({ who: 'digest worker', kind: 'run failed', why: str(r.note, 'no note'), when: r.ts }));
      const entries = d.score && Array.isArray(d.score.entries) ? d.score.entries : [];
      entries.filter(e => e.eval_status === 'unable').slice(0, 8)
        .forEach(e => rows.push({ who: str(e.ticker, '?'), kind: 'not gradable', why: str(e.unable_reason, 'no reason recorded'), when: e.ts }));
      if (!rows.length) { body.appendChild(kit.empty('Nothing failed or was skipped in the recent runs.')); return; }
      const trs = rows.map(r => h('tr', {},
        h('th', { scope: 'row', class: 'v-mono' }, r.who),
        h('td', {}, chip(r.kind, r.kind === 'not gradable' ? 'flat' : 'warn')),
        h('td', {}, r.why),
        h('td', { class: 'v-muted' }, r.when ? fmtAgo(r.when) : '–')));
      body.appendChild(kit.table('Failed or skipped digest work', [{ label: 'Ticker' }, { label: 'What' }, { label: 'Reason' }, { label: 'When' }], trs));
    },
  }, 60000);

  /* ---------------- scoreboard v2 ---------------- */
  const sb = kit.section({ title: 'Scoreboard', id: 'scoreboard', hint: 'advice graded against realised returns', cls: 'v-span-2' });
  kit.autoLoad(sb, {
    source: () => scoreSrc(),
    isEmpty: d => !isObj(d),
    render: (body, j) => renderScoreboard(body, j),
  }, 120000);

  function cellRow(label, c, minN) {
    const n = num(c.n ?? c.advice) ?? 0;
    const graded = gradedOf(c);
    const w = wilsonOf(c);
    const thin = c.insufficient_sample === true || (minN !== null && graded !== null && graded < minN);
    const hr = num(c.hitRatePct);
    const track = h('span', { class: 'v-wilson', role: 'img', 'aria-label': w ? '95% interval ' + w[0].toFixed(0) + ' to ' + w[1].toFixed(0) + ' percent' : 'no interval' });
    if (w) {
      const fill = h('span', { class: 'v-wilson__fill' });
      fill.style.left = w[0] + '%';
      fill.style.width = Math.max(1.5, w[1] - w[0]) + '%';
      track.appendChild(fill);
      if (hr !== null) { const m = h('span', { class: 'v-wilson__mark' }); m.style.left = Math.min(100, Math.max(0, hr)) + '%'; track.appendChild(m); }
    }
    return h('tr', { class: thin ? 'v-row--thin' : null },
      h('th', { scope: 'row' }, label),
      h('td', { class: 'v-num' }, fmtNum(n)),
      h('td', { class: 'v-num' }, graded === null ? '–' : fmtNum(graded)),
      h('td', { class: 'v-num' }, hr === null ? 'no verdicts' : fmtPct(hr, 0), num(c.hits) !== null ? h('span', { class: 'v-muted' }, ' (' + c.hits + '/' + ((num(c.hits) || 0) + (num(c.misses) || 0)) + ')') : null),
      h('td', {}, w ? h('span', { class: 'v-wrap' }, track, h('span', { class: 'v-muted v-small' }, w[0].toFixed(0) + '–' + w[1].toFixed(0) + '%')) : h('span', { class: 'v-muted' }, '–')),
      h('td', { class: 'v-num v-' + tone(c.avgExcessPct) }, num(c.avgExcessPct) === null ? '–' : fmtPct(c.avgExcessPct, 2, true)),
      h('td', {}, thin ? chip('insufficient sample', 'warn', 'Below the minimum sample size: not a rate yet') : chip('ok', 'flat')));
  }

  function renderScoreboard(body, j) {
    const cal = isObj(j.calibration) ? j.calibration : {};
    const minN = num(j.min_samples);
    const horizons = Array.isArray(j.horizons) ? j.horizons : [];
    const byH = flattenCells(cal.by_horizon).map(x => ({ ...x, label: /^\d+$/.test(x.label) ? 'T+' + x.label : x.label }));
    const gradedTotal = byH.length ? byH.reduce((s, x) => s + (gradedOf(x.cell) || 0), 0)
      : Object.values(isObj(j.summary) ? j.summary : {}).reduce((s, c) => s + (isObj(c) ? (gradedOf(c) || 0) : 0), 0);
    const bench = j.benchmark;
    const benchLabel = isObj(bench) ? str(bench.id || bench.name || bench.label, 'benchmark') : str(bench, '');
    const benchStatus = isObj(bench) ? str(bench.status || bench.detail) : str(j.benchmark_status);
    body.appendChild(h('p', { class: 'v-summary' },
      [benchLabel ? 'excess vs ' + benchLabel : null, horizons.length ? 'horizons T+' + horizons.join(' / T+') : null, minN !== null ? 'min n = ' + minN : null, j.engine_version ? 'engine ' + j.engine_version : null].filter(Boolean).join(' · ')));
    if (gradedTotal === 0) {
      const last = horizons.length ? horizons[horizons.length - 1] : 20;
      body.appendChild(h('p', { class: 'v-callout v-callout--warn', role: 'status' },
        'No graded advice yet. A call needs T+' + last + ' trading days to mature before any hit rate means anything.',
        benchStatus ? ' Benchmark status: ' + benchStatus + '.' : ''));
    } else if (benchStatus && /(unavailable|missing|error|stale|fail|no)/i.test(benchStatus)) {
      body.appendChild(h('p', { class: 'v-callout v-callout--warn', role: 'status' }, 'Benchmark status: ' + benchStatus));
    }
    const groups = [];
    if (byH.length) groups.push(['By horizon', byH]);
    const sumCells = flattenCells(j.summary);
    if (sumCells.length) groups.push(['By rating', sumCells]);
    const act = flattenCells(cal.by_action);
    if (act.length) groups.push(['By action', act]);
    const phase = flattenCells(cal.by_phase);
    if (phase.length) groups.push(['By phase', phase]);
    const strata = flattenCells(cal.by_model || j.by_model || cal.strata || j.strata);
    if (strata.length) groups.push(['Per model', strata]);
    const baseRaw = j.baseline ?? cal.baseline ?? j.baselines ?? cal.baselines;
    if (isCell(baseRaw)) groups.push(['Baseline', [{ label: str(baseRaw.name || baseRaw.label, 'baseline'), cell: baseRaw }]]);
    else if (isObj(baseRaw)) { const b = flattenCells(baseRaw); if (b.length) groups.push(['Baseline', b]); }
    if (groups.length) {
      const trs = [];
      groups.forEach(([title, cells]) => {
        trs.push(h('tr', { class: 'v-grouprow' }, h('th', { scope: 'rowgroup', colspan: 7 }, title)));
        cells.forEach(x => trs.push(cellRow(x.label, x.cell, minN)));
      });
      body.appendChild(kit.table('Scoreboard calibration', [
        { label: 'Group' }, { label: 'n', num: true }, { label: 'Graded', num: true }, { label: 'Hit rate', num: true },
        { label: '95% Wilson interval' }, { label: 'Avg excess', num: true }, { label: 'Sample' }], trs, 'v-table--score'));
    }
    const fb = isObj(j.feedback_counts) ? j.feedback_counts : null;
    if (fb) body.appendChild(h('p', { class: 'v-muted' }, 'Operator votes: ' + (fb.up || 0) + ' up · ' + (fb.down || 0) + ' down'));
    const entries = Array.isArray(j.entries) ? j.entries.slice(0, 15) : [];
    if (entries.length) {
      const trs = entries.map(en => {
        const done = en.eval_status && en.eval_status !== 'completed';
        const votes = ['up', 'down'].map(v => {
          const cur = en.feedback === v;
          const b = h('button', { type: 'button', class: 'v-btn v-btn--sm v-vote' + (cur ? ' is-on' : ''), 'aria-pressed': cur ? 'true' : 'false', 'aria-label': 'Vote the ' + str(en.ticker) + ' call was ' + (v === 'up' ? 'right' : 'wrong') }, v === 'up' ? '+' : '–');
          b.addEventListener('click', async () => {
            b.disabled = true;
            const r = await ctx.api.apiSend('POST', '/api/scoreboard/feedback', { ticker: en.ticker, ts: en.ts, vote: v });
            if (!r.ok) { ctx.ui.toast(errText(r.error), { type: 'error', title: 'Vote not recorded' }); b.disabled = false; return; }
            sb.reload();
          });
          return b;
        });
        return h('tr', {},
          h('td', { class: 'v-muted' }, str(en.date, '–')),
          h('th', { scope: 'row', class: 'v-mono' }, str(en.ticker, '?')),
          h('td', {}, chip(str(en.rating, '?'), RATING_CHIP[String(en.rating).toUpperCase()] || 'flat'), en.action ? h('span', { class: 'v-muted' }, ' ' + en.action) : null),
          h('td', { class: 'v-num v-' + tone(en.excessT5Pct) }, num(en.excessT5Pct) === null ? '–' : fmtPct(en.excessT5Pct, 2, true)),
          h('td', { class: 'v-num v-' + tone(en.excessT20Pct) }, num(en.excessT20Pct) === null ? '–' : fmtPct(en.excessT20Pct, 2, true)),
          h('td', {}, en.verdict ? chip(str(en.verdict), VERDICT_CHIP[en.verdict] || 'flat') : (done ? h('span', { class: 'v-muted' }, str(en.unable_reason || en.eval_status)) : h('span', { class: 'v-muted' }, 'pending'))),
          h('td', { class: 'v-actions' }, ...votes));
      });
      body.appendChild(h('h3', { class: 'v-h3' }, 'Recent graded calls'));
      body.appendChild(kit.table('Recent calls and verdicts', [{ label: 'Date' }, { label: 'Ticker' }, { label: 'Call' }, { label: 'T+5', num: true }, { label: 'T+20', num: true }, { label: 'Verdict' }, { label: 'Was it right?' }], trs));
    }
    if (!groups.length && !entries.length) body.appendChild(kit.empty('No graded advice yet.'));
  }

  grid.append(run.el, advice.el, issues.el, sb.el);
  return () => kit.dispose();
}
