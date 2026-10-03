/* AI Ops view: lane, workers, job queue, triage, approvals, evidence pack.
   export function mount(root, ctx) -> unmount() */
import { createKit, isObj, num, str, fmtAgo, fmtSpan, fmtNum, errText } from './_kit.js';
import { summarizeWorkers, renderWorkerTable } from './_workers.js';
import { openReport } from './_report.js';

const JOB_CHIP = { done: 'up', error: 'down', cancelled: 'flat', queued: 'warn', running: 'info' };
const JOB_LABEL = { done: 'done', error: 'failed', cancelled: 'stopped', queued: 'queued', running: 'running' };
const APPR_CHIP = { pending: 'warn', approved: 'up', denied: 'down', expired: 'flat', abandoned: 'flat' };
const SEV_CHIP = { info: 'info', warning: 'warn', warn: 'warn', error: 'down', critical: 'down', urgent: 'down' };
const LANE_STATE_CHIP = { ok: 'up', up: 'up', ready: 'up', serving: 'up', wrong_model: 'warn', down: 'down', unreachable: 'down', error: 'down' };

export function mount(root, ctx) {
  const kit = createKit(ctx, root);
  const { h, chip } = kit;
  const grid = h('div', { class: 'v-grid v-aiops' });
  root.appendChild(grid);

  /* ---------------- lane ---------------- */
  const lane = kit.section({ title: 'GPU lane', id: 'lane', cls: 'v-span-2' });
  const renderLane = (body, s) => {
    const role = s.role === 'primary' || s.role === 'fallback' ? s.role : null;
    const serving = s.serving_model !== false && !!(s.lane || s.model);
    body.appendChild(h('div', { class: 'v-lanehead' },
      chip(serving ? 'serving' : 'no model loaded', serving ? 'up' : 'down'),
      role ? chip(role === 'fallback' ? 'FALLBACK lane' : 'primary lane', role === 'fallback' ? 'warn' : 'up') : null,
      h('span', { class: 'v-mono v-strong' }, str(s.lane, '–')),
      h('span', { class: 'v-muted v-mono' }, str(s.model, ''))));
    if (role === 'fallback') {
      body.appendChild(h('p', { class: 'v-callout v-callout--warn', role: 'status' },
        'Answers are currently coming from the fallback lane. Quality and latency can differ from the primary model.'));
    }
    if (!serving) {
      body.appendChild(h('p', { class: 'v-muted' }, 'No large model is loaded; scheduled runs auto-load one when the GPU is free.'));
    }
    const pref = Array.isArray(s.preference) ? s.preference : [];
    if (pref.length) {
      const chain = h('ol', { class: 'v-chain', 'aria-label': 'Lane preference order' });
      pref.forEach((p, i) => chain.appendChild(h('li', { class: p === s.lane ? 'is-active' : null, 'aria-current': p === s.lane ? 'true' : null },
        (i + 1) + '. ' + str(isObj(p) ? p.name : p))));
      body.appendChild(h('div', { class: 'v-block' }, h('h3', { class: 'v-h3' }, 'Preference order'), chain));
    }
    const cands = Array.isArray(s.candidates) ? s.candidates : [];
    if (cands.length) {
      const rows = cands.map(c => h('tr', { class: c.name === s.lane ? 'v-row--active' : null },
        h('th', { scope: 'row', class: 'v-mono' }, str(c.name, '?')),
        h('td', { class: 'v-mono v-muted' }, str(c.model_id, '–')),
        h('td', {}, chip(str(c.state, 'unknown'), LANE_STATE_CHIP[str(c.state).toLowerCase()] || 'flat'))));
      body.appendChild(h('div', { class: 'v-block' }, h('h3', { class: 'v-h3' }, 'Candidates'),
        kit.table('Lane candidates', [{ label: 'Lane' }, { label: 'Model' }, { label: 'State' }], rows)));
    }
    const b = s.budget;
    if (isObj(b)) {
      const cap = num(b.cap), used = num(b.used) || 0;
      const blk = h('div', { class: 'v-block' }, h('h3', { class: 'v-h3' }, 'LLM budget' + (b.day ? ' · ' + b.day : '')));
      if (cap) {
        blk.appendChild(h('div', { class: 'v-budget' },
          h('label', { class: 'v-budget__label', for: 'v-bud-total' }, 'Total ' + fmtNum(used) + ' / ' + fmtNum(cap) + (num(b.left) !== null ? ' · ' + fmtNum(b.left) + ' left' : '')),
          h('meter', { id: 'v-bud-total', class: 'v-meter' + (used / cap > 0.9 ? ' is-hot' : ''), min: 0, max: cap, value: Math.min(used, cap), low: cap * 0.7, high: cap * 0.9, optimum: 0 }, used + ' of ' + cap)));
      }
      const purposes = isObj(b.purposes) ? Object.entries(b.purposes) : [];
      purposes.forEach(([name, v], i) => {
        const pu = isObj(v) ? num(v.used) || 0 : num(v) || 0;
        const pc = isObj(v) && num(v.cap) ? num(v.cap) : cap || Math.max(pu, 1);
        const id = 'v-bud-' + i;
        blk.appendChild(h('div', { class: 'v-budget' },
          h('label', { class: 'v-budget__label', for: id }, name + ' ' + fmtNum(pu) + (isObj(v) && num(v.cap) ? ' / ' + fmtNum(v.cap) : '')),
          h('meter', { id, class: 'v-meter', min: 0, max: pc, value: Math.min(pu, pc) }, pu + ' of ' + pc)));
      });
      if (!cap && !purposes.length) blk.appendChild(kit.empty('No budget data.'));
      body.appendChild(blk);
    }
  };
  kit.autoLoad(lane, { source: '/api/lane-status', render: renderLane, isEmpty: d => !isObj(d) }, 20000);

  /* ---------------- workers ---------------- */
  const workers = kit.section({ title: 'Background workers', id: 'workers', cls: 'v-span-2' });
  const workersSrc = kit.memo(async () => {
    const [bg, runs] = await Promise.all([kit.get('/api/background'), kit.get('/api/worker-runs?limit=200')]);
    if (!bg.ok && !runs.ok) return { ok: false, error: errText(bg.error), status: bg.status };
    return { ok: true, stale: !!(bg.stale || runs.stale), data: { bg: bg.ok ? bg.data : null, runs: runs.ok && Array.isArray(runs.data) ? runs.data : [], partial: !bg.ok || !runs.ok } };
  }, 500);
  kit.autoLoad(workers, {
    source: workersSrc, isEmpty: () => false,
    render: (body, d) => {
      const list = summarizeWorkers(d.bg, d.runs);
      const bad = list.filter(w => w.verdict === 'fail' || w.verdict === 'stale').length;
      body.appendChild(h('p', { class: 'v-summary' + (bad ? ' v-summary--bad' : ''), role: 'status' },
        list.length + ' workers · ' + (bad ? bad + ' need attention' : 'all healthy')));
      if (d.partial) body.appendChild(h('p', { class: 'v-callout v-callout--warn' }, 'One of the two worker feeds did not answer; state may be incomplete.'));
      renderWorkerTable(kit, body, list);
    },
  }, 30000);

  /* ---------------- jobs ---------------- */
  const jobs = kit.section({ title: 'Analysis queue', id: 'jobs' });
  const cancelJob = async (job, btn) => {
    btn.disabled = true;
    const r = await ctx.api.apiSend('POST', '/api/cancel/' + encodeURIComponent(job.id));
    if (!r.ok) { ctx.ui.toast(errText(r.error), { type: 'error', title: 'Cancel failed' }); btn.disabled = false; }
    else ctx.ui.toast(job.ticker + ' job removed from the queue', { type: 'ok' });
    jobs.reload();
  };
  kit.autoLoad(jobs, {
    source: '/api/jobs?limit=30',
    map: d => (Array.isArray(d) ? d : isObj(d) && Array.isArray(d.jobs) ? d.jobs : []),
    isEmpty: d => !d.length,
    emptyText: 'No analysis jobs yet. A started analysis shows up here straight away.',
    render: (body, rows) => {
      const inflight = rows.filter(j => j.status === 'queued' || j.status === 'running').length;
      body.appendChild(h('p', { class: 'v-summary', role: 'status' }, inflight ? inflight + ' in flight' : 'queue idle'));
      const trs = rows.slice(0, 15).map(j => {
        const when = j.finished_at || j.created_at;
        const actions = [];
        if (j.status === 'queued') {
          const b = h('button', { type: 'button', class: 'v-btn v-btn--sm', 'aria-label': 'Cancel queued job for ' + str(j.ticker) }, 'Cancel');
          b.addEventListener('click', () => cancelJob(j, b));
          actions.push(b);
        }
        if (j.status === 'done' && j.result_path) {
          const b = h('button', { type: 'button', class: 'v-btn v-btn--sm', 'aria-label': 'Open report for ' + str(j.ticker) }, 'Report');
          b.addEventListener('click', () => openReport(kit, j.result_path, str(j.ticker) + ' · ' + str(j.mode)));
          actions.push(b);
        }
        return h('tr', {},
          h('th', { scope: 'row', class: 'v-mono v-strong' }, str(j.ticker, '?')),
          h('td', {}, chip(JOB_LABEL[j.status] || str(j.status, '?'), JOB_CHIP[j.status] || 'flat')),
          h('td', { class: 'v-muted' }, str(j.mode, '–') + (j.source ? ' · ' + j.source : '')),
          h('td', {}, j.decision ? chip(str(j.decision), 'flat') : '–'),
          h('td', { class: 'v-muted v-note', title: str(j.message) }, str(j.message).slice(0, 90) || '–'),
          h('td', { class: 'v-muted' }, (j.finished_at ? 'ended ' : j.status === 'running' ? 'running ' : 'queued ') + fmtAgo(when)),
          h('td', { class: 'v-actions' }, ...actions));
      });
      body.appendChild(kit.table('Analysis jobs', [{ label: 'Ticker' }, { label: 'Status' }, { label: 'Mode' }, { label: 'Decision' }, { label: 'Message' }, { label: 'When' }, { label: 'Actions' }], trs));
    },
  }, 10000);

  /* ---------------- approvals ---------------- */
  const appr = kit.section({ title: 'Approvals', id: 'appr' });
  const decide = async (row, approve, btns) => {
    btns.forEach(b => { b.disabled = true; });
    const r = await ctx.api.apiSend('POST', '/api/approvals/' + encodeURIComponent(row.id) + (approve ? '/approve' : '/deny'));
    const d = r.data;
    if (!r.ok || (d && d.ok === false)) {
      ctx.ui.toast(errText((d && d.error) || r.error), { type: 'error', title: 'Decision not recorded' });
      btns.forEach(b => { b.disabled = false; });
    } else if (approve) {
      ctx.ui.toast(str(row.ticker) + ': deep dive ' + (d && d.deduped ? 'already queued' : 'queued'), { type: 'ok', title: 'Approved' });
    } else {
      ctx.ui.toast(str(row.ticker) + ': dismissed', { type: 'info' });
    }
    appr.reload();
    jobs.reload();
  };
  kit.autoLoad(appr, {
    source: '/api/approvals?limit=30',
    isEmpty: d => !isObj(d) || !Array.isArray(d.items) || !d.items.length,
    emptyText: 'Nothing to approve. Triage proposes a deep dive only when a story looks worth a full run.',
    render: (body, d) => {
      const counts = isObj(d.counts) ? Object.entries(d.counts) : [];
      if (counts.length) body.appendChild(h('p', { class: 'v-summary' }, counts.map(([k, v]) => k + ' ' + v).join(' · ')));
      const list = h('ul', { class: 'v-list' });
      d.items.slice(0, 12).forEach(row => {
        const open = row.status === 'pending';
        const li = h('li', { class: 'v-item' + (open ? ' v-item--open' : '') },
          h('div', { class: 'v-item__head' },
            h('span', { class: 'v-mono v-strong' }, str(row.ticker, '?')),
            chip(str(row.status, '?'), APPR_CHIP[row.status] || 'flat'),
            h('span', { class: 'v-muted' }, open && num(row.ttl_left_s) !== null ? 'expires in ' + fmtSpan(row.ttl_left_s) : row.decided_at ? 'decided ' + fmtAgo(row.decided_at) : 'proposed ' + fmtAgo(row.created_at))),
          h('p', { class: 'v-item__text' }, str(row.intent, '(no intent recorded)')),
          row.raw ? h('p', { class: 'v-muted v-item__sub' }, str(row.raw)) : null,
          row.source_id ? h('p', { class: 'v-muted v-item__sub' }, 'source ' + row.source_id) : null);
        if (open) { // only an open proposal gets buttons: deciding a decided row is refused server-side
          const yes = h('button', { type: 'button', class: 'v-btn v-btn--primary', 'aria-label': 'Approve deep dive for ' + str(row.ticker) }, 'Approve deep dive');
          const no = h('button', { type: 'button', class: 'v-btn', 'aria-label': 'Dismiss proposal for ' + str(row.ticker) }, 'Dismiss');
          yes.addEventListener('click', () => decide(row, true, [yes, no]));
          no.addEventListener('click', () => decide(row, false, [yes, no]));
          li.appendChild(h('div', { class: 'v-item__actions' }, yes, no));
        }
        list.appendChild(li);
      });
      body.appendChild(list);
    },
  }, 15000);

  /* ---------------- triage ---------------- */
  const triage = kit.section({ title: 'AI triage', id: 'triage', cls: 'v-span-2' });
  kit.autoLoad(triage, {
    source: '/api/triage?limit=30',
    isEmpty: d => !isObj(d) || !isObj(d.status),
    emptyText: 'The triage worker did not report.',
    render: (body, d) => {
      const st = d.status;
      const state = st.enabled === false ? 'disabled' : st.running ? 'every 10 min' : 'loop stopped';
      const counters = h('p', { class: 'v-summary' },
        state + (st.min_relevance != null ? ' · relevance ≥ ' + st.min_relevance : '') + ' · ' +
        [['runs', st.runs], ['pushed', st.pushed], ['suppressed', st.suppressed], ['queued', st.queued], ['proposals', st.proposals], ['errors', st.errors]]
          .map(([k, v]) => k + ' ' + (v || 0)).join(' · ') +
        (st.lane_down ? ' · lane down ' + st.lane_down : '') +
        ' · ' + (st.last_run ? 'last pass ' + fmtAgo(st.last_run) : 'never ran'));
      body.appendChild(counters);
      const recent = Array.isArray(d.recent) ? d.recent : [];
      if (!recent.length) {
        body.appendChild(kit.empty('No triage decisions logged yet. Keyword-warm news, short-ratio spikes and fresh filings queue up here; one lane call per pass classifies the batch.'));
        return;
      }
      const trs = recent.slice(0, 15).map(it => h('tr', { class: it.delivered ? null : 'v-row--dim' },
        h('th', { scope: 'row', class: 'v-mono v-strong' }, str(it.ticker, '?')),
        h('td', {}, chip(str(it.severity, '–'), SEV_CHIP[it.severity] || 'flat')),
        h('td', { class: 'v-num' }, num(it.relevance) === null ? '–' : Number(it.relevance).toFixed(2)),
        h('td', {}, h('span', {}, str(it.thesis, '(no thesis)')),
          it.headline ? h('div', { class: 'v-muted v-note', title: str(it.headline) }, str(it.headline).slice(0, 110) + (it.source ? ' · ' + it.source : '')) : null),
        h('td', { class: 'v-muted' }, it.action_hint && it.action_hint !== 'none' ? str(it.action_hint) : '–'),
        h('td', {}, chip(it.delivered ? 'pushed' : 'suppressed', it.delivered ? 'up' : 'flat'), it.approval ? h('span', { class: 'v-muted' }, ' approval ' + it.approval) : null),
        h('td', { class: 'v-muted' }, fmtAgo(it.ts))));
      body.appendChild(kit.table('Recent triage decisions (suppressed ones kept on purpose)', [
        { label: 'Ticker' }, { label: 'Severity' }, { label: 'Relevance', num: true }, { label: 'Thesis' }, { label: 'Hint' }, { label: 'Outcome' }, { label: 'When' }], trs));
    },
  }, 60000);

  /* ---------------- evidence pack ---------------- */
  const evid = kit.section({ title: 'Evidence pack', id: 'evidence', hint: 'what the analysis may cite', cls: 'v-span-2' });
  mountEvidence(kit, evid);

  grid.append(lane.el, workers.el, jobs.el, appr.el, triage.el, evid.el);
  return () => kit.dispose();
}

/* ---------- evidence pack viewer ---------- */

function mountEvidence(kit, sec) {
  const { ctx, h } = kit;
  const select = h('select', { id: 'v-ev-sym', class: 'v-input', 'aria-label': 'Ticker for evidence pack' });
  const load = h('button', { type: 'button', class: 'v-btn' }, 'Load pack');
  const out = h('div', { class: 'v-evidence' });
  const controls = h('div', { class: 'v-controls' }, h('label', { for: 'v-ev-sym', class: 'v-label' }, 'Ticker'), select, load);
  sec.body.append(controls, out);
  sec.hasContent = true;
  sec.body.removeAttribute('aria-busy');
  sec.stamp(null);
  let seq = 0;

  const loadList = () => {
    ctx.ui.renderState(out, { loading: 'Loading watchlist…' });
    kit.get('/api/watchlist').then((res) => {
      if (kit.isDisposed()) return;
      if (!res.ok) { ctx.ui.renderState(out, { error: errText(res.error), retry: loadList }); return; }
      const entries = Array.isArray(res.data) ? res.data : res.data && Array.isArray(res.data.entries) ? res.data.entries : [];
      const ids = entries.filter(e => e && e.analyzeable !== false && e.kind !== 'index' && e.role !== 'benchmark').map(e => str(e.id || e.symbol)).filter(Boolean);
      if (!ids.length) { ctx.ui.renderState(out, { empty: 'No watchlist tickers.' }); return; }
      select.textContent = '';
      ids.forEach(id => select.appendChild(h('option', { value: id }, id)));
      fetchPack();
    });
  };

  const fetchPack = async () => {
    const id = select.value;
    if (!id) return;
    const mine = ++seq;
    ctx.ui.renderState(out, { loading: 'Loading evidence for ' + id + '…' });
    const res = await kit.get('/api/evidence/' + encodeURIComponent(id));
    if (kit.isDisposed() || mine !== seq) return;
    if (!res.ok || !isObj(res.data)) { ctx.ui.renderState(out, { error: errText(res.error) || 'Evidence pack unavailable', retry: fetchPack }); return; }
    out.textContent = '';
    renderPack(kit, out, res.data);
    sec.stamp(Date.now() / 1000, res.stale);
  };
  load.addEventListener('click', fetchPack);
  select.addEventListener('change', fetchPack);
  loadList();
}

function renderPack(kit, out, pack) {
  const { h, chip } = kit;
  const missing = Array.isArray(pack.missing) ? pack.missing : [];
  out.appendChild(h('p', { class: 'v-summary' }, str(pack.label || pack.symbol || ''),
    pack.as_of ? ' · as of ' + str(pack.as_of) : '',
    missing.length ? ' · missing: ' + missing.join(', ') : ' · complete'));
  const sections = Object.entries(pack).filter(([k]) => !['missing', 'symbol', 'label', 'as_of', 'generated_at'].includes(k));
  sections.forEach(([name, val]) => {
    const isMissing = val === 'MISSING';
    const meta = isObj(val) ? [val.as_of && 'as of ' + str(val.as_of), val.source && str(val.source)].filter(Boolean).join(' · ') : '';
    const body = h('div', { class: 'v-ev__body' });
    if (!isMissing) body.appendChild(valueNode(kit, val, 0));
    out.appendChild(h('details', { class: 'v-ev' + (isMissing ? ' is-missing' : '') },
      h('summary', {}, h('span', { class: 'v-mono v-strong' }, name), isMissing ? chip('MISSING', 'warn') : null, meta ? h('span', { class: 'v-muted' }, meta) : null),
      body));
  });
}

function scalar(v) {
  if (v === null || v === undefined) return '–';
  if (typeof v === 'number') return fmtNum(v, Number.isInteger(v) ? 0 : 2);
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return String(v);
}

function valueNode(kit, v, depth) {
  const { h } = kit;
  if (v === 'MISSING') return kit.chip('MISSING', 'warn');
  if (!v || typeof v !== 'object') return h('span', { class: 'v-evval' }, scalar(v));
  if (depth >= 3) { const s = JSON.stringify(v); return h('code', { class: 'v-mono v-muted' }, s.length > 200 ? s.slice(0, 200) + '…' : s); }
  if (Array.isArray(v)) {
    if (!v.length) return h('span', { class: 'v-muted' }, '(none)');
    if (v.every(x => x === null || typeof x !== 'object')) return h('span', { class: 'v-evval' }, v.map(scalar).join(', '));
    const ul = h('ul', { class: 'v-evlist' });
    v.slice(0, 12).forEach(x => ul.appendChild(h('li', {}, valueNode(kit, x, depth + 1))));
    if (v.length > 12) ul.appendChild(h('li', { class: 'v-muted' }, '… ' + (v.length - 12) + ' more'));
    return ul;
  }
  const dl = h('dl', { class: 'v-kv' });
  Object.entries(v).forEach(([k, val]) => {
    if (k === 'as_of' || k === 'source') return;
    dl.append(h('dt', {}, k), h('dd', {}, valueNode(kit, val, depth + 1)));
  });
  return dl;
}
