/* Digest view: latest advice per ticker (published rating, pending/draft/trigger badges,
   p(beat benchmark in 20d)), what changed, history, scoreboard v3 (directional hit rates,
   probabilistic metrics per horizon, baselines), run-now, failed/skipped.
   export function mount(root, ctx) -> unmount() */
import { createKit, isObj, num, str, toSec, fmtAgo, fmtIn, fmtDay, fmtWhen, fmtNum, fmtPct, tone, errText } from './_kit.js';
import { isDeferredNote } from './_workers.js';
import { openReport } from './_report.js';

const RATING_CHIP = { BUY: 'up', OVERWEIGHT: 'up', SELL: 'down', UNDERWEIGHT: 'down', HOLD: 'info', WATCH: 'info' };
const VERDICT_CHIP = { hit: 'up', miss: 'down', neutral: 'flat' };
const META_KEYS = new Set(['failed', 'failures', 'skipped', 'meta', 'errors', 'history', 'generated_at', 'ts', 'advice']);
const BENCH_KIND = { total_return: 'total return', price: 'price' };
const BASELINE_LABEL = { always_hold: 'always_hold', trend_10m: 'trend_10m', momentum_12_1: 'momentum_12_1' };

/** 'YYYY-MM-DD' | epoch | ISO -> 'MM-DD' (the "since" of a pending change). */
function mmdd(v) {
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v)) return v.slice(5, 10);
  const s = toSec(v);
  if (s === null) return '?';
  const d = new Date(s * 1000);
  return String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
/** Probability as 0-100 (accepts 0..1 fractions and 0..100 percents). */
function pctOf(v) {
  const n = num(v);
  return n === null ? null : (n <= 1 ? n * 100 : n);
}
const fmt3 = (v) => (num(v) === null ? '–' : Number(v).toFixed(3));
const fmtSigned3 = (v) => (num(v) === null ? '–' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(3));

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
    const pub = isObj(v.published) ? v.published : null;
    items.push({
      sym,
      /* the headline is the PUBLISHED state; the top-level fields are only the latest answer */
      rating: str((pub && pub.rating) || v.rating, 'OTHER').toUpperCase(),
      action: (pub && pub.action) || v.action || null,
      score: num(pub && pub.score !== undefined && pub.score !== null ? pub.score : v.score),
      published: pub,
      pending: isObj(v.pending) ? v.pending : null,
      draft: v.draft === true,
      trigger: v.trigger ? str(v.trigger) : null,
      taxTag: v.taxTag ? str(v.taxTag) : null,
      p20: num(v.p20),
      expExcess: num(v.expExcess20Pct),
      benchmark: v.benchmark ? str(v.benchmark) : null,
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
      a.items.forEach(it => list.appendChild(adviceCard(it, timeline, isObj(score) && typeof score.benchmark === 'string' ? score.benchmark : null)));
      body.appendChild(list);
    },
  }, 60000);

  function adviceCard(it, timeline, benchFallback) {
    const prev = previousOf(it, timeline);
    const hist = it.history.length ? it.history : (timeline[it.sym] || []);
    const benchName = it.benchmark || benchFallback || 'benchmark';
    const p20 = pctOf(it.p20);
    const meta = [
      it.action ? chip(str(it.action).toUpperCase(), 'flat', 'recommended action') : null,
      it.score !== null ? h('span', { class: 'v-kvp', title: 'score' }, h('span', { class: 'v-muted' }, 'score '), fmtNum(it.score, Number.isInteger(it.score) ? 0 : 1)) : null,
      it.confidence ? h('span', { class: 'v-kvp', title: 'confidence' }, h('span', { class: 'v-muted' }, 'conf '), it.confidence) : null,
      it.price !== null ? h('span', { class: 'v-kvp', title: 'price when the advice was given' }, h('span', { class: 'v-muted' }, '@ '), fmtNum(it.price, 2) + ' €') : null,
    ];
    const prob = [
      p20 !== null ? h('span', { class: 'v-kvp v-card__p20', title: 'the latest answer\'s probability that the EUR total return beats the benchmark over the next 20 trading days' },
        'p(beat ' + benchName + ' in 20d) ' + Math.round(p20) + '%') : null,
      it.expExcess !== null ? h('span', { class: 'v-kvp v-card__exp v-' + tone(it.expExcess), title: 'expected excess return over the benchmark, 20 trading days' },
        'exp. excess ' + fmtPct(it.expExcess, 1, true)) : null,
    ];
    const badges = [
      it.pending ? chip('pending: ' + str(it.pending.rating, '?').toUpperCase() + ' since ' + mmdd(it.pending.since), 'warn', 'An unconfirmed candidate change: it is published only if it repeats or is strong enough') : null,
      it.draft ? chip('DRAFT', 'warn', 'Answered by a non-publishing lane; a draft never changes the published rating') : null,
      it.trigger ? chip('trigger: ' + it.trigger, 'info', 'what started this run') : null,
      it.taxTag ? chip(it.taxTag, 'flat', 'tax status of a sale') : null,
    ].filter(Boolean);
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

    const li = h('li', { class: 'v-card v-card--' + (RATING_CHIP[it.rating] || 'flat') + (it.draft ? ' v-card--draft' : ''), 'data-symbol': it.sym },
      h('div', { class: 'v-card__head' },
        h('h3', { class: 'v-card__title' }, h('span', { class: 'v-mono' }, it.sym), labelOf(it.sym) !== it.sym ? h('span', { class: 'v-muted' }, ' ' + labelOf(it.sym)) : null),
        chip(it.rating, RATING_CHIP[it.rating] || 'flat', it.published ? 'published rating' + (it.published.date ? ' since ' + str(it.published.date) : '') : null),
        h('span', { class: 'v-muted v-card__age', title: it.ts ? fmtWhen(it.ts) : '' }, it.ts ? fmtAgo(it.ts) : str(it.date, '–'))),
      badges.length ? h('div', { class: 'v-card__badges' }, ...badges) : null,
      h('div', { class: 'v-card__meta' }, ...meta),
      prob.some(Boolean) ? h('div', { class: 'v-card__meta v-card__prob' }, ...prob) : null,
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
      d.runs.filter(r => r.worker === 'digest' && (r.ok === false || isDeferredNote(r.note))).slice(0, 8)
        .forEach(r => rows.push({ who: 'digest worker', kind: isDeferredNote(r.note) ? 'deferred' : 'run failed', why: str(r.note, 'no note'), when: r.ts }));
      const entries = d.score && Array.isArray(d.score.entries) ? d.score.entries : [];
      entries.filter(e => e.eval_status === 'unable').slice(0, 8)
        .forEach(e => rows.push({ who: str(e.ticker, '?'), kind: 'not gradable', why: str(e.unable_reason, 'no reason recorded'), when: e.ts }));
      if (!rows.length) { body.appendChild(kit.empty('Nothing failed or was skipped in the recent runs.')); return; }
      const trs = rows.map(r => h('tr', {},
        h('th', { scope: 'row', class: 'v-mono' }, r.who),
        h('td', {}, chip(r.kind, r.kind === 'not gradable' || r.kind === 'deferred' ? 'flat' : 'warn')),
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

  /* directional rows only: HOLD / WATCH are scored probabilistically, never hit or miss */
  const isProbOnly = (label) => /^(hold|watch)$/i.test(str(label).split(' · ').pop().trim());

  function wilsonBar(c) {
    const w = wilsonOf(c);
    const hr = num(c.hitRatePct);
    if (!w) return h('span', { class: 'v-muted' }, '–');
    const track = h('span', { class: 'v-wilson', role: 'img', 'aria-label': '95% interval ' + w[0].toFixed(0) + ' to ' + w[1].toFixed(0) + ' percent' });
    const fill = h('span', { class: 'v-wilson__fill' });
    fill.style.left = w[0] + '%';
    fill.style.width = Math.max(1.5, w[1] - w[0]) + '%';
    track.appendChild(fill);
    if (hr !== null) { const m = h('span', { class: 'v-wilson__mark' }); m.style.left = Math.min(100, Math.max(0, hr)) + '%'; track.appendChild(m); }
    return h('span', { class: 'v-wrap' }, track, h('span', { class: 'v-muted v-small' }, w[0].toFixed(0) + '–' + w[1].toFixed(0) + '%'));
  }

  function cellRow(label, c, minN) {
    const n = num(c.n ?? c.advice) ?? 0;
    const graded = gradedOf(c);
    const probOnly = isProbOnly(label);
    const thin = !probOnly && (c.insufficient_sample === true || (minN !== null && graded !== null && graded < minN));
    const hr = num(c.hitRatePct);
    const probChip = chip('probabilistic only', 'info', 'HOLD / WATCH are scored by Brier and log score, not by hit or miss');
    return h('tr', { class: thin ? 'v-row--thin' : null, 'data-prob-only': probOnly ? 'true' : null },
      h('th', { scope: 'row' }, label),
      h('td', { class: 'v-num' }, fmtNum(n)),
      h('td', { class: 'v-num' }, graded === null ? '–' : fmtNum(graded)),
      h('td', { class: probOnly ? null : 'v-num' }, probOnly ? probChip : (hr === null ? 'no verdicts' : fmtPct(hr, 0)), !probOnly && num(c.hits) !== null ? h('span', { class: 'v-muted' }, ' (' + c.hits + '/' + ((num(c.hits) || 0) + (num(c.misses) || 0)) + ')') : null),
      h('td', {}, probOnly ? h('span', { class: 'v-muted' }, '–') : wilsonBar(c)),
      h('td', { class: 'v-num v-' + tone(c.avgExcessPct) }, num(c.avgExcessPct) === null ? '–' : fmtPct(c.avgExcessPct, 2, true)),
      h('td', {}, probOnly ? probChip : thin ? chip('insufficient sample', 'warn', 'Below the minimum sample size: not a rate yet') : chip('ok', 'flat')));
  }

  /* one horizon of j.probabilistic: n, n_without_p, brier, brierRef, bss, logScore, ic, reliability */
  function probBlock(hz, P) {
    const hid = 'v-prob-h-' + hz;
    const ic = isObj(P.ic) ? P.ic : {};
    const bss = num(P.bss);
    const kvs = [
      ['n', fmtNum(num(P.n) || 0), 'graded rows that carry a probability'],
      ['n without p', fmtNum(num(P.n_without_p) || 0), 'graded rows with no probability: excluded from every metric below'],
      ['Brier', fmt3(P.brier), 'mean squared error of p(beat benchmark): lower is better'],
      ['reference Brier', fmt3(P.brierRef), 'Brier of a constant forecast at the base rate seen in the same rows'],
      ['BSS', fmtSigned3(P.bss), 'Brier skill score = 1 - Brier / reference: above 0 beats the constant forecast'],
      ['log score', fmt3(P.logScore), 'mean log loss: lower is better'],
      ['IC (Spearman)', fmtSigned3(ic.spearman) + ' (n ' + fmtNum(num(ic.n) || 0) + ')', 'rank correlation of the score with the realised excess return'],
    ];
    const dl = h('dl', { class: 'v-kv v-kv--row' });
    kvs.forEach(([k, v, t]) => dl.append(h('dt', { title: t }, k), h('dd', { class: k === 'BSS' ? 'v-' + tone(bss) : null }, v)));
    const rel = Array.isArray(P.reliability) ? P.reliability.filter(isObj) : [];
    const pc = (v) => (pctOf(v) === null ? '–' : fmtPct(pctOf(v), 0));
    const relRows = rel.map(b => h('tr', {},
      h('th', { scope: 'row', class: 'v-mono' }, Math.round((num(b.lo) || 0) * 100) + '–' + Math.round((num(b.hi) || 0) * 100) + '%'),
      h('td', { class: 'v-num' }, fmtNum(num(b.n) || 0)),
      h('td', { class: 'v-num' }, pc(b.meanP)),
      h('td', { class: 'v-num' }, pc(b.observed))));
    return h('section', { class: 'v-prob', id: 'v-prob-' + hz, 'data-horizon': hz, 'aria-labelledby': hid },
      h('h3', { class: 'v-h3', id: hid }, 'Probabilistic · T+' + hz),
      dl,
      (num(P.n) || 0) === 0 ? h('p', { class: 'v-muted v-small' }, 'No graded rows with a probability yet.') : null,
      relRows.length ? kit.table('Reliability at T+' + hz + ': stated probability against what happened', [
        { label: 'p bin' }, { label: 'n', num: true }, { label: 'mean p', num: true }, { label: 'observed', num: true }], relRows, 'v-table--rel') : null);
  }

  function baselineRow(key, label, c, brier, own) {
    const have = isObj(c);
    const hr = have ? num(c.hitRatePct) : null;
    const graded = have ? gradedOf(c) : null;
    return h('tr', { class: own ? 'v-row--active' : null, 'data-strategy': key },
      h('th', { scope: 'row', class: own ? null : 'v-mono' }, label),
      h('td', { class: 'v-num' }, have ? fmtNum(num(c.n) ?? 0) : '–'),
      h('td', { class: 'v-num' }, graded === null ? '–' : fmtNum(graded)),
      h('td', { class: 'v-num' }, hr === null ? '–' : fmtPct(hr, 0), have && num(c.hits) !== null ? h('span', { class: 'v-muted' }, ' (' + c.hits + '/' + ((num(c.hits) || 0) + (num(c.misses) || 0)) + ')') : null),
      h('td', {}, have ? wilsonBar(c) : h('span', { class: 'v-muted' }, '–')),
      h('td', { class: 'v-num' }, fmt3(brier)),
      h('td', { class: 'v-num v-' + tone(have ? c.avgExcessPct : null) }, have && num(c.avgExcessPct) !== null ? fmtPct(c.avgExcessPct, 2, true) : '–'));
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
    const kindText = BENCH_KIND[j.benchmark_kind] || '';
    body.appendChild(h('p', { class: 'v-summary', id: 'v-sb-header' },
      [benchLabel ? 'excess vs ' + benchLabel + (kindText ? ' (' + kindText + ')' : '') : null, horizons.length ? 'horizons T+' + horizons.join(' / T+') : null, minN !== null ? 'min n = ' + minN : null, j.engine_version ? 'engine ' + j.engine_version : null].filter(Boolean).join(' · ')));
    const regrade = num(isObj(j.status) ? j.status.regrade_pending : null);
    if (regrade !== null && regrade > 0) {
      body.appendChild(h('p', { class: 'v-callout v-callout--warn', id: 'v-sb-regrade', role: 'status' },
        regrade + (regrade === 1 ? ' advice row is' : ' advice rows are') + ' still waiting to be re-graded under the current engine; the figures below will change.'));
    }
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
    const baseRaw = j.baseline ?? cal.baseline;
    if (isCell(baseRaw)) groups.push(['Baseline', [{ label: str(baseRaw.name || baseRaw.label, 'baseline'), cell: baseRaw }]]);
    if (groups.length) {
      body.appendChild(h('h3', { class: 'v-h3' }, 'Directional hit rates'));
      body.appendChild(h('p', { class: 'v-muted v-small' }, 'Directional calls only (buy / add / sell / reduce / avoid / trim). HOLD and WATCH are never counted as hit or miss; they are scored below.'));
      const trs = [];
      groups.forEach(([title, cells]) => {
        trs.push(h('tr', { class: 'v-grouprow' }, h('th', { scope: 'rowgroup', colspan: 7 }, title)));
        cells.forEach(x => trs.push(cellRow(x.label, x.cell, minN)));
      });
      body.appendChild(kit.table('Scoreboard calibration', [
        { label: 'Group' }, { label: 'n', num: true }, { label: 'Graded', num: true }, { label: 'Hit rate', num: true },
        { label: '95% Wilson interval' }, { label: 'Avg excess', num: true }, { label: 'Sample' }], trs, 'v-table--score'));
    }

    const probKeys = isObj(j.probabilistic) ? Object.keys(j.probabilistic).filter(k => isObj(j.probabilistic[k])).sort((a, b) => Number(a) - Number(b)) : [];
    if (probKeys.length) {
      body.appendChild(h('h3', { class: 'v-h3' }, 'Probabilistic scores'));
      body.appendChild(h('p', { class: 'v-muted v-small' }, 'Scores the stated probability of beating the benchmark. This is the only grade HOLD and WATCH calls receive.'));
      const blocks = h('div', { class: 'v-probgrid', id: 'v-sb-prob' });
      probKeys.forEach(k => blocks.appendChild(probBlock(k, j.probabilistic[k])));
      body.appendChild(blocks);
    }

    const base = isObj(j.baselines) ? j.baselines : null;
    if (base) {
      const ph = num(j.primary_horizon) ?? (horizons.length ? horizons[horizons.length - 1] : 20);
      const byHc = isObj(cal.by_horizon) ? cal.by_horizon : {};
      const llm = [byHc[String(ph)], byHc['T+' + ph], byHc['T' + ph]].find(isCell) || null;
      const probP = isObj(j.probabilistic) && isObj(j.probabilistic[String(ph)]) ? j.probabilistic[String(ph)] : null;
      const trs = [baselineRow('llm', 'LLM advice', llm, probP ? probP.brier : null, true)];
      Object.keys(BASELINE_LABEL).forEach(k => { if (isObj(base[k])) trs.push(baselineRow(k, BASELINE_LABEL[k], base[k], base[k].brier, false)); });
      body.appendChild(h('h3', { class: 'v-h3', id: 'v-sb-baselines-h' }, 'Baselines vs the LLM · T+' + ph));
      body.appendChild(h('p', { class: 'v-muted v-small' }, 'Same anchor rows. trend_10m: BUY above the 210-day average, else SELL. momentum_12_1: BUY if the 12-1 month return is positive. always_hold: p = 0.5. A baseline that beats the LLM is the finding.'));
      body.appendChild(kit.table('Baselines against the LLM at T+' + ph, [
        { label: 'Strategy' }, { label: 'n', num: true }, { label: 'Graded', num: true }, { label: 'Hit rate', num: true },
        { label: '95% Wilson interval' }, { label: 'Brier', num: true }, { label: 'Avg excess', num: true }], trs, 'v-table--baselines'));
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
        const verdictCell = en.graded_as === 'probabilistic_only'
          ? chip('probabilistic only', 'info', 'HOLD / WATCH: scored by probability, not hit or miss')
          : en.verdict ? chip(str(en.verdict), VERDICT_CHIP[en.verdict] || 'flat')
            : (done ? h('span', { class: 'v-muted' }, str(en.unable_reason || en.eval_status)) : h('span', { class: 'v-muted' }, 'pending'));
        return h('tr', {},
          h('td', { class: 'v-muted' }, str(en.date, '–')),
          h('th', { scope: 'row', class: 'v-mono' }, str(en.ticker, '?')),
          h('td', {}, chip(str(en.rating, '?'), RATING_CHIP[String(en.rating).toUpperCase()] || 'flat'), en.action ? h('span', { class: 'v-muted' }, ' ' + en.action) : null),
          h('td', { class: 'v-num v-' + tone(en.excessT5Pct) }, num(en.excessT5Pct) === null ? '–' : fmtPct(en.excessT5Pct, 2, true)),
          h('td', { class: 'v-num v-' + tone(en.excessT20Pct) }, num(en.excessT20Pct) === null ? '–' : fmtPct(en.excessT20Pct, 2, true)),
          h('td', {}, verdictCell),
          h('td', { class: 'v-actions' }, ...votes));
      });
      body.appendChild(h('h3', { class: 'v-h3' }, 'Recent graded calls'));
      body.appendChild(kit.table('Recent calls and verdicts', [{ label: 'Date' }, { label: 'Ticker' }, { label: 'Call' }, { label: 'T+5', num: true }, { label: 'T+20', num: true }, { label: 'Verdict' }, { label: 'Was it right?' }], trs));
    }
    if (!groups.length && !entries.length && !probKeys.length && !base) body.appendChild(kit.empty('No graded advice yet.'));
  }

  grid.append(run.el, advice.el, issues.el, sb.el);
  return () => kit.dispose();
}
