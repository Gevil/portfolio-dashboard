// Practice-only "Agent" card (Overview, below the positions): mode, broker reachability, bucket
// budget/cash/equity, season state with a "Start season" confirm dialog (buildAgent), plus the
// Phase 2 controls and orders table (createAgentPanel): mode control, halt/resume, seed bucket,
// rebalance, orders. Input is the /api/paper/status payload (plan §9 / contract §4); the HTTP
// contract is §3.7. overview.js owns the card shell and the Start-season POST; the panel owns its
// own polling (30 s while active) and its dialogs, and cleans both up in destroy()/stop().
// Phase 3 adds the "Decisions" section (contract phase3 §5): today's paper-PM decision, the last 10
// decisions (expandable) and a "Run decision now" button; GET /api/paper/decisions is polled every 60 s
// while the card is active. Model-written text (rationale, per-id reasons) is rendered ONLY through
// ui.setMarkdown; everything else is textContent.
import { el, fmtEur, fmtNum, fmtDateTime24, isNum } from '../util.js';
import { openDialog, renderState, setMarkdown, toast, usePolling } from '../ui.js';
import { apiGet, apiSend, latestRequest } from '../api.js';
import { getBucket, practiceQuery } from '../book.js';

const MODE_LABEL = { off: 'Off', shadow: 'Shadow', demo: 'Demo' };
const MODES = ['off', 'shadow', 'demo'];
const MODE_HELP = {
  off: 'No orders are recorded or sent.',
  shadow: 'Order intents are recorded in the ledger but never sent to the broker.',
  demo: 'Orders are sent to the Trading 212 demo account (paper money).',
};
const STATE_LABEL = {
  shadow: 'Shadow', pending: 'Pending', submitted: 'Submitted', unknown: 'Unknown', not_sent: 'Not sent',
  rejected: 'Rejected', partially_filled: 'Partly filled', filled: 'Filled', cancelled: 'Cancelled',
};
const STATE_ICON = {
  shadow: '\u25cb', pending: '\u2026', submitted: '\u2192', unknown: '?', not_sent: '\u2212',
  rejected: '\u2715', partially_filled: '\u25d0', filled: '\u2713', cancelled: '\u2715',
};
const TERMINAL = new Set(['shadow', 'rejected', 'filled', 'cancelled', 'not_sent']);
const ORDERS_SHOWN = 20;
const POLL_MS = 30000;
const BOOK = 'practice';
const DECISIONS_SHOWN = 10;
const DECISIONS_POLL_MS = 60000;
const GRADING_POLL_MS = 300000;   // decision quality: every 5 min while visible
const BRIEFS_SHOWN = 10;          // briefings listed (newest first)
const BRIEFS_POLL_MS = 300000;    // a briefing is written once per session, after the close
const GRADING_MIN_N = 30;         // scoreboard insufficient-sample rule
const DECIDE_TIMEOUT_MS = 300000;   // a decision is 1-2 GPU turns; the default request timeout is too short
const DECISION_LABEL = { targets: 'Targets', nochange: 'No change', deferred: 'Deferred', invalid: 'Invalid' };
const DECISION_ICON = { targets: '\u2192', nochange: '=', deferred: '\u2026', invalid: '\u2715' };
const VIEW_LABEL = { overweight: 'Overweight', neutral: 'Neutral', underweight: 'Underweight' };
const VIEW_ICON = { overweight: '\u25b2', neutral: '\u25cf', underweight: '\u25bc' };

function row(label, value, cls) {
  return el('div', { class: 'agent-row' },
    el('span', { class: 'lbl' }, label),
    el('span', { class: `num agent-row-val ${cls || ''}`.trim() }, value));
}

// lastError is the broker envelope {error, status, detail} (a plain string from an older payload is shown as is).
function brokerErrorKind(e) {
  return e && typeof e === 'object' ? (e.kind || e.error || 'error') : '';
}

function brokerErrorText(e) {
  if (e && typeof e === 'object') {
    const kind = brokerErrorKind(e);
    const status = e.status;
    return `${kind}${status ? ' ' + status : ''}: ${e.detail == null ? '' : e.detail}`;
  }
  return String(e);
}

// Not reachable and the only recorded problem is a rate limit: the broker is being throttled, not down.
function brokerRateLimited(b) {
  return !b.reachable && brokerErrorKind(b.lastError) === 'rate_limited';
}

function brokerLabel(b) {
  if (!b.configured) return 'Not configured';
  if (b.reachable) return 'Reachable';
  return brokerRateLimited(b) ? 'Rate limited (retrying)' : 'Unreachable';
}

function brokerClass(b) {
  if (!b.configured || b.reachable) return '';
  return brokerRateLimited(b) ? 'warn-ink' : 'down-ink';
}

/**
 * status: the /api/paper/status payload ({enabled, mode, broker, bucket, season, banner, worker}).
 * handlers: {onStartSeason(): Promise} - called when the confirm dialog's "Start season" is clicked.
 * Returns an array of child nodes for the status part of the card (the caller owns the card shell/title).
 */
export function buildAgent(status, { onStartSeason } = {}) {
  const b = status.broker || {};
  const bucket = status.bucket || {};
  const season = status.season || {};
  const out = [];

  out.push(el('div', { class: 'agent-grid' },
    row('Mode', MODE_LABEL[status.mode] || status.mode || '\u2013'),
    row('Broker', brokerLabel(b), brokerClass(b)),
    row('Bucket budget', isNum(bucket.budgetEur) ? fmtEur(bucket.budgetEur) : '\u2013'),
    row('Strategy', bucket.strategyVersion || '\u2013')));

  if (season.started) {
    out.push(el('p', { class: 'agent-note' }, `Season started${season.inceptionDate ? ` ${season.inceptionDate}` : ''}.`));
  } else {
    out.push(el('p', { class: 'agent-note muted' }, 'No practice season yet \u2014 the bucket tracks the demo account but has no baseline to compare against.'));
    out.push(el('button', {
      type: 'button', class: 'btn btn-primary', id: 'agent-start-season',
      onclick: () => startSeasonDialog(onStartSeason),
    }, 'Start season'));
  }
  if (b.lastError) out.push(el('p', { class: 'agent-note warn-ink', role: 'status', id: 'agent-broker-error', text: `Broker: ${brokerErrorText(b.lastError)}` }));
  return out;
}

function startSeasonDialog(onStartSeason) {
  const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: () => dlg.close() });
  const confirm = el('button', {
    type: 'button', class: 'btn btn-primary', id: 'agent-confirm-start', text: 'Start season',
    onclick: async () => {
      confirm.disabled = true;
      try { await onStartSeason(); } finally { if (dlg) dlg.close(); }
    },
  });
  const dlg = openDialog({
    title: 'Start practice season',
    size: 'sm',
    initialFocus: cancel,
    body: el('div', { class: 'confirm-body' },
      el('p', {},
        'This starts the AI paper-trading season on the ', el('strong', {}, 'Trading 212 demo'),
        ' account, seeded from your real portfolio\u2019s current weights. No real money is involved.'),
      el('div', { class: 'dialog-actions' }, cancel, confirm)),
  });
}

// ------------------------------------------------------------------ preview tables

function th(text, cls) { return el('th', { scope: 'col', class: cls || null }, text); }

/** Table of planned intents + skipped reasons + warnings (plan_orders output, contract §2). */
function planView(plan) {
  const p = plan && typeof plan === 'object' ? plan : {};
  const intents = Array.isArray(p.intents) ? p.intents : [];
  const skipped = Array.isArray(p.skipped) ? p.skipped : [];
  const warnings = Array.isArray(p.warnings) ? p.warnings : [];
  const box = el('div', { class: 'agent-plan' });
  if (intents.length) {
    box.append(el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table agent-plan-table' },
      el('caption', { class: 'visually-hidden' }, 'Planned orders'),
      el('thead', {}, el('tr', {}, th('Ticker'), th('Side'), th('Qty', 'num'), th('Limit', 'num'), th('Value', 'num'))),
      el('tbody', {}, intents.map(i => {
        const limit = i.limit_price != null ? i.limit_price : i.limitPrice;
        const value = i.value_eur != null ? i.value_eur : i.valueEur;
        return el('tr', {},
          el('td', {}, i.ticker || i.id || '\u2013'),
          el('td', {}, String(i.side || '\u2013')),
          el('td', { class: 'num' }, isNum(i.quantity) ? fmtNum(i.quantity, 4).replace(/\.?0+$/, '') : '\u2013'),
          el('td', { class: 'num' }, isNum(limit) ? fmtEur(limit, { dec: 2 }) : '\u2013'),
          el('td', { class: 'num' }, isNum(value) ? fmtEur(value) : '\u2013'));
      })))));
  } else {
    box.append(el('p', { class: 'agent-note' }, 'No orders would be placed.'));
  }
  if (skipped.length) {
    box.append(el('p', { class: 'agent-note' }, 'Skipped:'),
      el('ul', { class: 'agent-skipped' }, skipped.map(s => el('li', {}, `${s.id}: ${s.reason}`))));
  }
  if (warnings.length) {
    box.append(el('ul', { class: 'agent-skipped warn-ink' }, warnings.map(w => el('li', {}, String(w)))));
  }
  return box;
}

// ------------------------------------------------------------------ decision views

/** deferred | invalid | nochange | targets (contract phase3 §2.3 row shape). */
function decisionKind(r) {
  if (r.deferred) return 'deferred';
  if (r.valid === false) return 'invalid';
  return r.noChange ? 'nochange' : 'targets';
}

function outputOf(r) { return r.output && typeof r.output === 'object' ? r.output : {}; }

function targetsOf(r) {
  const t = outputOf(r).targets;
  return t && typeof t === 'object' ? t : {};
}

function pctText(v) { return isNum(v) ? `${fmtNum(v, 1)}%` : '\u2013'; }

function deltaText(d) {
  if (!isNum(d)) return '\u2013';
  const sign = d > 0.05 ? '+' : (d < -0.05 ? '\u2212' : '');
  return `${sign}${fmtNum(Math.abs(d), 1)} pp`;
}

/** One-line plain-text summary (system text only; never model prose). */
function decisionSummary(r) {
  const kind = decisionKind(r);
  if (kind === 'deferred') return `Deferred: ${r.deferred}`;
  if (kind === 'invalid') return r.error ? `Invalid output, no orders: ${r.error}` : 'Invalid output, no orders';
  if (kind === 'nochange') return 'No change';
  const parts = Object.entries(targetsOf(r)).filter(([, v]) => isNum(v)).map(([id, v]) => `${id} ${fmtNum(v, 1)}%`);
  const cash = outputOf(r).cash_pct;
  if (isNum(cash)) parts.push(`cash ${fmtNum(cash, 1)}%`);
  return parts.length ? parts.join(' \u00b7 ') : 'Targets';
}

function decisionChip(kind) {
  return el('span', { class: `agent-chip is-${kind}` },
    el('span', { 'aria-hidden': 'true' }, `${DECISION_ICON[kind] || '?'} `), DECISION_LABEL[kind] || kind);
}

/** Target table; weights = {id: currentPct} or null (then the current/change columns are omitted). */
function targetsTable(r, weights) {
  const out = outputOf(r);
  const targets = targetsOf(r);
  const ids = Object.keys(targets);
  const withCur = !!weights;
  const cur = (id) => (withCur && isNum(weights[id]) ? weights[id] : (withCur ? 0 : null));
  const rows = ids.map(id => {
    const t = targets[id];
    const c = cur(id);
    return el('tr', { dataset: { id } },
      el('td', {}, id),
      withCur ? el('td', { class: 'num' }, pctText(c)) : null,
      el('td', { class: 'num' }, pctText(t)),
      withCur ? el('td', { class: 'num' }, deltaText(isNum(t) && isNum(c) ? t - c : null)) : null);
  });
  if (isNum(out.cash_pct)) {
    const held = withCur ? Object.values(weights).reduce((a, w) => a + (isNum(w) ? w : 0), 0) : 0;
    const cashNow = withCur ? Math.max(0, 100 - held) : null;
    rows.push(el('tr', { dataset: { id: 'cash' } },
      el('td', {}, 'Cash'),
      withCur ? el('td', { class: 'num' }, pctText(cashNow)) : null,
      el('td', { class: 'num' }, pctText(out.cash_pct)),
      withCur ? el('td', { class: 'num' }, deltaText(out.cash_pct - cashNow)) : null));
  }
  return el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table agent-targets-table' },
    el('caption', { class: 'visually-hidden' }, withCur ? 'Target weights versus current weights' : 'Target weights'),
    el('thead', {}, el('tr', {},
      th('Instrument'), withCur ? th('Current', 'num') : null, th('Target', 'num'), withCur ? th('Change', 'num') : null)),
    el('tbody', {}, rows)));
}

/** Full detail of one stored decision row. Rationale and per-id reasons are model text -> setMarkdown only. */
function decisionView(r, weights) {
  const kind = decisionKind(r);
  const out = outputOf(r);
  const box = el('div', { class: 'agent-decision-body', dataset: { decisionId: r.decisionId || '' } });
  const meta = [fmtDateTime24(r.createdAt), r.trigger, [r.lane, r.model].filter(Boolean).join(' / '),
    r.mode ? `mode ${r.mode}` : '', r.strategyVersion].filter(Boolean);
  box.append(el('p', { class: 'agent-decision-meta' }, decisionChip(kind), ` ${meta.join(' \u00b7 ')}`));
  if (kind === 'deferred' || kind === 'invalid') {
    box.append(el('p', { class: 'agent-note' }, decisionSummary(r)));
    return box;
  }
  if (kind === 'nochange') box.append(el('p', { class: 'agent-note' }, 'The agent kept the current weights.'));
  if (kind === 'targets') box.append(targetsTable(r, weights));
  const facts = [];
  if (isNum(out.confidence)) facts.push(`Confidence ${fmtNum(out.confidence * 100, 0)}%`);
  if (isNum(out.horizon_days)) facts.push(`Horizon ${out.horizon_days} trading days`);
  if (kind === 'targets') {
    const n = Array.isArray(r.rowIds) ? r.rowIds.length : 0;
    facts.push(`${n} order${n === 1 ? '' : 's'} recorded`);
  }
  if (facts.length) box.append(el('p', { class: 'agent-note' }, facts.join(' \u00b7 ')));
  if (typeof out.rationale === 'string' && out.rationale.trim()) {
    const rat = el('div', { class: 'agent-rationale' });
    setMarkdown(rat, out.rationale);
    box.append(el('h4', { class: 'agent-minititle' }, 'Rationale'), rat);
  }
  const per = out.per_id && typeof out.per_id === 'object' ? Object.entries(out.per_id) : [];
  if (per.length) {
    box.append(el('h4', { class: 'agent-minititle' }, 'Per instrument'),
      el('ul', { class: 'agent-views' }, per.map(([id, v]) => {
        const view = v && typeof v === 'object' ? String(v.view || '') : '';
        const reason = el('div', { class: 'agent-reason' });
        if (v && typeof v.reason === 'string' && v.reason.trim()) setMarkdown(reason, v.reason);
        return el('li', { dataset: { id } },
          el('strong', {}, id),
          view ? el('span', { class: `agent-chip is-${view}` },
            el('span', { 'aria-hidden': 'true' }, `${VIEW_ICON[view] || ''} `), VIEW_LABEL[view] || view) : null,
          reason);
      })));
  }
  return box;
}

// ------------------------------------------------------------------ panel

/**
 * createAgentPanel({onChanged}) -> {node, update(status, {bucketEmpty}), stop(), destroy()}
 * update() (re)renders and starts the 30 s orders polling; stop() halts polling and closes dialogs
 * (call it whenever the card is hidden / leaves Practice / is disabled); destroy() = stop + final.
 * onChanged(): called after every successful action so the caller can refresh /api/paper/status etc.
 */
export function createAgentPanel({ onChanged, onStartSeason } = {}) {
  const haltHost = el('div', { class: 'agent-halt-host' });
  const statusHost = el('div', { class: 'agent-status' });
  const controlsHost = el('div', { class: 'agent-controls', id: 'agent-controls' });
  const ordersHost = el('div', { class: 'agent-orders', id: 'agent-orders' });
  const decisionsHost = el('section', { class: 'agent-decisions', id: 'agent-decisions', 'aria-labelledby': 'agent-decisions-title' });
  const gradingHost = el('section', { class: 'agent-grading', id: 'agent-grading', 'aria-labelledby': 'agent-grading-title' });
  const briefsHost = el('section', { class: 'agent-briefs', id: 'agent-briefs', 'aria-labelledby': 'agent-briefs-title' });
  const node = el('div', { class: 'agent-panel' }, haltHost, statusHost, controlsHost, decisionsHost, gradingHost, briefsHost, ordersHost);

  let status = null;
  let bucketEmpty = false;
  let orders = null;          // {items, halt} from the last successful poll
  let ordersError = null;
  let busy = false;
  let stopPoll = null;
  let controlsSig = '';
  const dialogs = new Set();
  const req = latestRequest();
  const decReq = latestRequest();
  let decisions = null;       // {items, today, budget} from the last successful poll
  let decisionsError = null;
  let weights = null;         // {id: weightPct} of the bucket now (only fetched when today's decision has targets)
  let deciding = false;
  let stopDecPoll = null;
  let decisionsSig = '';
  const openDecisions = new Set();
  const gradeReq = latestRequest();
  let grading = null;         // /api/paper/grading payload from the last successful poll
  let gradingError = null;
  let stopGradePoll = null;
  const briefReq = latestRequest();
  let briefs = null;          // /api/paper/briefs payload from the last successful poll
  let briefsError = null;
  let stopBriefPoll = null;
  let briefsSig = '';
  const openBriefs = new Set();

  const season = () => (status && status.season) || {};
  const halt = () => {
    const h = (orders && orders.halt) || (status && status.halt) || null;
    return h && h.halted ? h : null;
  };
  const hasOpenRows = () => !!(orders && (orders.items || []).some(r => !TERMINAL.has(r.state)));

  function dialog(opts) {
    const d = openDialog({ ...opts, onClose: () => { dialogs.delete(d); if (opts.onClose) opts.onClose(); } });
    dialogs.add(d);
    return d;
  }

  /** Dialog with confirm/cancel (+ optional reason input). Resolves null on cancel, {reason} on confirm. */
  function ask({ title, paragraphs, confirmText, danger = false, reasonLabel = null, id }) {
    return new Promise(resolve => {
      let answer = null;
      const input = reasonLabel
        ? el('input', { type: 'text', class: 'agent-input', id: `${id}-reason`, maxlength: '200', autocomplete: 'off' })
        : null;
      const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: () => d.close() });
      const ok = el('button', {
        type: 'button', class: danger ? 'btn btn-danger' : 'btn btn-primary', id, text: confirmText,
        onclick: () => { answer = { reason: input ? input.value.trim() : '' }; d.close(); },
      });
      const d = dialog({
        title, size: 'sm', initialFocus: input || cancel, onClose: () => resolve(answer),
        body: el('div', { class: 'confirm-body' },
          paragraphs.map(p => el('p', {}, p)),
          input ? el('label', { class: 'agent-field', for: `${id}-reason` }, el('span', { class: 'lbl' }, reasonLabel), input) : null,
          el('div', { class: 'dialog-actions' }, cancel, ok)),
      });
    });
  }

  async function post(path, body, opts) {
    return apiSend('POST', path, { book: BOOK, bucket: getBucket(), ...body }, opts);
  }

  function changed() {
    if (!stopPoll) return;
    if (onChanged) onChanged();
    loadOrders();
    loadDecisions();
  }

  // ---------------------------------------------------------------- actions
  async function setMode(mode) {
    if (busy || !status || status.mode === mode) return;
    const answer = await ask({
      id: 'agent-confirm-mode',
      title: `Set mode: ${MODE_LABEL[mode]}`,
      paragraphs: [
        ['Switch the agent from ', el('strong', {}, MODE_LABEL[status.mode] || status.mode), ' to ', el('strong', {}, MODE_LABEL[mode]), '.'],
        MODE_HELP[mode],
        ['Every order goes to the ', el('strong', {}, 'Trading 212 demo'), ' account only. No real money is involved.'],
      ],
      confirmText: `Set ${MODE_LABEL[mode]}`,
    });
    if (!answer) return;
    busy = true; paintControls();
    const res = await post('/api/paper/mode', { mode });
    busy = false;
    if (!status) return;
    if (res.ok) {
      if (res.data && typeof res.data === 'object' && res.data.mode) status = { ...status, ...res.data };
      toast(`Agent mode: ${MODE_LABEL[mode]}.`, { type: 'ok' });
      paintStatus();
      changed();
    } else {
      toast(res.error, { type: 'error', title: 'Could not change mode' });
      paintControls();
    }
  }

  async function haltAction() {
    if (busy) return;
    const answer = await ask({
      id: 'agent-confirm-halt', title: 'Halt the agent', danger: true, confirmText: 'Halt',
      paragraphs: ['No new orders are placed while halted. Open orders are cancelled. Resume manually afterwards.'],
      reasonLabel: 'Reason (optional)',
    });
    if (!answer) return;
    busy = true; paintControls();
    const body = answer.reason ? { reason: answer.reason } : {};
    const res = await post('/api/paper/halt', body);
    busy = false;
    if (res.ok) { toast('Agent halted.', { type: 'warn' }); changed(); } else { toast(res.error, { type: 'error', title: 'Could not halt' }); }
    paintControls();
  }

  async function resumeAction() {
    if (busy) return;
    const answer = await ask({
      id: 'agent-confirm-resume', title: 'Resume the agent', confirmText: 'Resume',
      paragraphs: ['The agent may place orders again according to the current mode.'],
    });
    if (!answer) return;
    busy = true; paintControls();
    const res = await post('/api/paper/resume', {});
    busy = false;
    if (res.ok) { toast('Agent resumed.', { type: 'ok' }); changed(); } else { toast(res.error, { type: 'error', title: 'Could not resume' }); }
    paintControls();
  }

  /**
   * Shared preview-then-confirm dialog. getPayload() -> {path, body} | {error}; run on Preview (dryRun
   * true) and again with dryRun false on Confirm using the SAME payload snapshot.
   */
  function previewDialog({ title, idPrefix, intro, fields, getPayload, confirmText, doneText }) {
    const out = el('div', { class: 'agent-preview', id: `${idPrefix}-preview`, 'aria-live': 'polite' });
    let snapshot = null;
    const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: () => d.close() });
    const previewBtn = el('button', { type: 'button', class: 'btn', id: `${idPrefix}-preview-btn`, text: 'Preview' });
    const confirmBtn = el('button', { type: 'button', class: 'btn btn-primary', id: `${idPrefix}-confirm`, text: confirmText });
    confirmBtn.disabled = true;
    const invalidate = () => { snapshot = null; confirmBtn.disabled = true; };
    const lock = (on) => { previewBtn.disabled = on; if (on) confirmBtn.disabled = true; };

    async function preview() {
      const p = getPayload();
      if (p.error) { snapshot = null; renderState(out, { error: p.error }); return; }
      lock(true);
      renderState(out, { loading: 'Previewing orders' });
      const res = await post(p.path, { ...p.body, dryRun: true });
      lock(false);
      if (!res.ok) { snapshot = null; renderState(out, { error: res.error }); return; }
      const plan = res.data && res.data.plan;
      snapshot = p;
      out.replaceChildren(planView(plan));
      confirmBtn.disabled = !(plan && Array.isArray(plan.intents) && plan.intents.length);
    }
    async function confirm() {
      if (!snapshot) return;
      const p = snapshot;
      lock(true);
      const res = await post(p.path, { ...p.body, dryRun: false });
      if (!res.ok) { lock(false); confirmBtn.disabled = false; renderState(out, { error: res.error }); return; }
      const n = res.data && Array.isArray(res.data.rows) ? res.data.rows.length : 0;
      toast(`${doneText}: ${n} order${n === 1 ? '' : 's'} recorded.`, { type: 'ok' });
      d.close();
      changed();
    }
    previewBtn.addEventListener('click', preview);
    confirmBtn.addEventListener('click', confirm);
    const d = dialog({
      title, size: 'lg', initialFocus: fields && fields.firstInput ? fields.firstInput : previewBtn,
      body: el('div', { class: 'confirm-body agent-dialog-body' },
        intro.map(p => el('p', {}, p)),
        fields ? fields.node : null,
        out,
        el('div', { class: 'dialog-actions' }, cancel, previewBtn, confirmBtn)),
    });
    if (fields) fields.onInput(invalidate);
    return { preview, d };
  }

  function seedDialog() {
    const { preview } = previewDialog({
      title: 'Seed bucket from baseline', idPrefix: 'agent-seed', confirmText: 'Confirm seed', doneText: 'Seed',
      intro: [['Buy the season\u2019s baseline weights in the bucket on the ', el('strong', {}, 'Trading 212 demo'),
        ' account. Mode ', el('strong', {}, MODE_LABEL[status && status.mode] || '\u2013'), ' decides whether orders are sent or only recorded.']],
      fields: null,
      getPayload: () => ({ path: '/api/paper/seed', body: {} }),
    });
    preview();
  }

  function rebalanceDialog() {
    const weights = season().baselineWeights || {};
    const ids = Object.keys(weights);
    const inputs = {};
    const listeners = [];
    const fieldFor = (key, label, value, id) => {
      const input = el('input', {
        type: 'number', class: 'agent-input num', id, min: '0', max: '100', step: '0.1', inputmode: 'decimal',
        value: value == null ? '' : String(value),
      });
      input.addEventListener('input', () => listeners.forEach(fn => fn()));
      inputs[key] = input;
      return el('label', { class: 'agent-field', for: id }, el('span', { class: 'lbl' }, label), input);
    };
    const idFields = ids.map(id => fieldFor(id, `${id} target %`, weights[id], `agent-rb-${id}`));
    const cashField = fieldFor('__cash', 'Cash % (optional)', '', 'agent-rb-cash');
    const grid = el('div', { class: 'agent-field-grid' }, idFields, cashField);
    const fields = {
      node: grid,
      firstInput: ids.length ? inputs[ids[0]] : inputs.__cash,
      onInput: (fn) => listeners.push(fn),
    };
    previewDialog({
      title: 'Rebalance', idPrefix: 'agent-rb', confirmText: 'Confirm rebalance', doneText: 'Rebalance',
      intro: [['Set target weights (percent of the bucket) per instrument. Preview shows the orders the risk engine would place; caps and the trading window still apply. Orders go to the ',
        el('strong', {}, 'Trading 212 demo'), ' account only.']],
      fields,
      getPayload: () => {
        const targets = {};
        let sum = 0;
        for (const id of ids) {
          const raw = inputs[id].value.trim();
          const v = raw === '' ? NaN : Number(raw);
          if (!Number.isFinite(v) || v < 0 || v > 100) return { error: `Target for ${id} must be a number between 0 and 100.` };
          targets[id] = v; sum += v;
        }
        const body = { targets };
        const cashRaw = inputs.__cash.value.trim();
        if (cashRaw !== '') {
          const c = Number(cashRaw);
          if (!Number.isFinite(c) || c < 0 || c > 100) return { error: 'Cash must be a number between 0 and 100.' };
          body.cashPct = c; sum += c;
        }
        if (sum > 100.0001) return { error: `Targets plus cash add up to ${fmtNum(sum, 1)}%, which is more than 100%.` };
        return { path: '/api/paper/rebalance', body };
      },
    });
  }

  // ---------------------------------------------------------------- decisions
  /** Why "Run decision now" is unavailable (visible text), or null. */
  function decideBlocked() {
    if (!season().started) return 'Start a season before running a decision.';
    if (status && status.mode === 'off') return 'Mode is Off: switch to Shadow or Demo to run a decision.';
    if (halt()) return 'The agent is halted: resume it first.';
    return null;
  }

  async function runDecision() {
    if (deciding || busy || !status || decideBlocked()) return;
    const b = decisions && decisions.budget;
    const trading = b && b.purposes && b.purposes.trading;
    const allowance = trading && isNum(trading.allowance) ? trading.allowance : (b && isNum(b.trading_cap) ? b.trading_cap : null);
    const left = b && isNum(b.left) ? b.left : null;
    const paragraphs = [
      'This asks the model for a new target allocation and uses 1\u20132 GPU turns from the trading budget.',
      left == null ? 'Trading budget left today: unknown.'
        : `Trading budget left today: ${left}${allowance != null ? ` of ${allowance}` : ''} turn${left === 1 ? '' : 's'}.`,
    ];
    if (left === 0) paragraphs.push('The budget is spent: the run will most likely be deferred.');
    paragraphs.push(['Orders from the decision go to the ', el('strong', {}, 'Trading 212 demo'), ' account only (mode ',
      el('strong', {}, MODE_LABEL[status.mode] || status.mode), ' decides whether they are sent or only recorded).']);
    const answer = await ask({ id: 'agent-confirm-decide', title: 'Run decision now', confirmText: 'Run decision', paragraphs });
    if (!answer || !status) return;
    deciding = true; paintDecisions();
    const res = await post('/api/paper/decide', {}, { timeoutMs: DECIDE_TIMEOUT_MS });
    deciding = false;
    if (!status) return;
    paintDecisions();
    if (res.ok) {
      const row = res.data && typeof res.data === 'object' ? res.data : {};
      if (row.deferred) toast(`Decision deferred: ${row.deferred}`, { type: 'warn' });
      else if (row.valid === false) toast(`The model\u2019s answer was invalid${row.error ? `: ${row.error}` : ''}. No orders were placed.`, { type: 'warn' });
      else toast(row.noChange ? 'Decision: no change.' : 'Decision recorded.', { type: 'ok' });
      changed();
    } else {
      toast(res.error, { type: 'error', title: 'Could not run decision' });
    }
  }

  async function loadDecisions() {
    const t = decReq.begin();
    const res = await apiGet(`/api/paper/decisions?${practiceQuery()}&limit=${DECISIONS_SHOWN}`, { signal: t.signal });
    if (!t.current()) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) {
      decisions = res.data; decisionsError = null;
    } else if (res.aborted) {
      return;
    } else {
      decisionsError = res.error || 'invalid response';
    }
    paintDecisions();
    const today = decisions && decisions.today;
    if (today && decisionKind(today) === 'targets') {
      const w = await apiGet(`/api/portfolio?${practiceQuery()}`, { signal: t.signal });
      if (!t.current()) return;
      if (w.ok && w.data && Array.isArray(w.data.positions)) {
        weights = {};
        for (const p of w.data.positions) if (p && p.id && isNum(p.weightPct)) weights[p.id] = p.weightPct;
      } else if (w.aborted) {
        return;
      }
    } else {
      weights = null;
    }
    paintDecisions();
  }

  function paintDecisions() {
    if (!status) { decisionsHost.replaceChildren(); decisionsSig = ''; return; }
    const blocked = decideBlocked();
    const sig = JSON.stringify([decisions, weights, decisionsError, blocked, deciding, busy, status.decision || null]);
    if (sig === decisionsSig) return;
    decisionsSig = sig;
    const active = document.activeElement;
    let focusSel = null;
    if (active && decisionsHost.contains(active)) {
      if (active.id) focusSel = `#${active.id}`;
      else if (active.tagName === 'SUMMARY' && active.parentElement && active.parentElement.dataset.decisionId) {
        focusSel = `[data-decision-id="${CSS.escape(active.parentElement.dataset.decisionId)}"] > summary`;
      }
    }

    const hintId = 'agent-decide-hint';
    const runBtn = el('button', {
      type: 'button', class: 'btn btn-primary', id: 'agent-decide', onclick: runDecision,
      'aria-describedby': blocked ? hintId : null,
      text: deciding ? 'Running decision\u2026' : 'Run decision now',
    });
    if (blocked || deciding || busy) runBtn.disabled = true;
    const head = el('div', { class: 'agent-decisions-head' },
      el('h3', { class: 'agent-subtitle', id: 'agent-decisions-title' }, 'Decisions'), runBtn);
    const kids = [head];
    if (blocked) kids.push(el('p', { class: 'agent-note', id: hintId }, blocked));
    if (deciding) kids.push(el('p', { class: 'agent-note', role: 'status' }, 'Asking the model\u2026 this can take a minute.'));
    const b = decisions && decisions.budget;
    if (b && isNum(b.left)) kids.push(el('p', { class: 'agent-note muted', id: 'agent-decisions-budget' }, `Trading budget left today: ${b.left} turn${b.left === 1 ? '' : 's'}.`));

    const body = el('div', { class: 'agent-decisions-body', id: 'agent-decisions-body' });
    if (!decisions) {
      if (decisionsError) renderState(body, { error: `Decisions unavailable: ${decisionsError}`, retry: () => loadDecisions() });
      else renderState(body, { loading: 'Loading decisions' });
      kids.push(body);
    } else {
      const today = decisions.today || null;
      const todayBox = el('div', { class: 'agent-decisions-today', id: 'agent-decisions-today' },
        el('h4', { class: 'agent-minititle' }, 'Today'));
      if (today) {
        todayBox.append(decisionView(today, weights));
      } else {
        const sched = status.decision && status.decision.reason ? ` Scheduler: ${status.decision.reason}` : '';
        todayBox.append(el('p', { class: 'agent-note' }, `No decision yet today.${sched}`));
      }
      const items = decisions.items.slice(0, DECISIONS_SHOWN);
      const histBox = el('div', { class: 'agent-decisions-history', id: 'agent-decisions-history' },
        el('h4', { class: 'agent-minititle' }, `Last ${DECISIONS_SHOWN} decisions`));
      if (!items.length) {
        histBox.append(el('p', { class: 'agent-note' }, 'No decisions yet.'));
      } else {
        histBox.append(el('ul', { class: 'agent-decision-list', id: 'agent-decisions-list' }, items.map(r => {
          const key = r.decisionId || '';
          const det = el('details', { class: 'agent-decision-item', dataset: { decisionId: key, kind: decisionKind(r) } },
            el('summary', {},
              el('span', { class: 'agent-decision-date' }, r.date || fmtDateTime24(r.createdAt)),
              decisionChip(decisionKind(r)),
              el('span', { class: 'agent-decision-sum' }, decisionSummary(r))),
            decisionView(r, null));
          if (openDecisions.has(key)) det.open = true;
          det.addEventListener('toggle', () => { if (det.open) openDecisions.add(key); else openDecisions.delete(key); });
          return el('li', {}, det);
        })));
      }
      body.append(todayBox, histBox);
      if (decisionsError) body.append(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${decisionsError}`));
      kids.push(body);
    }
    decisionsHost.replaceChildren(...kids);
    if (focusSel && document.activeElement === document.body) {
      const again = decisionsHost.querySelector(focusSel);
      if (again) again.focus();
    }
  }

  // ---------------------------------------------------------------- decision quality (contract phase4 §6)
  async function loadGrading() {
    const t = gradeReq.begin();
    const res = await apiGet(`/api/paper/grading?${practiceQuery()}`, { signal: t.signal });
    if (!t.current()) return;
    if (res.ok && res.data && typeof res.data === 'object' && res.data.horizons && typeof res.data.horizons === 'object') {
      grading = res.data; gradingError = null;
    } else if (!res.aborted) {
      gradingError = res.error || 'invalid response';
    }
    paintGrading();
  }

  function paintGrading() {
    if (!status) { gradingHost.replaceChildren(); return; }
    const head = el('h3', { class: 'agent-subtitle', id: 'agent-grading-title' }, 'Decision quality');
    if (!grading) {
      const body = el('div', { class: 'agent-grading-body', id: 'agent-grading-body' });
      if (gradingError) renderState(body, { error: `Decision quality unavailable: ${gradingError}`, retry: () => loadGrading() });
      else renderState(body, { loading: 'Loading decision quality' });
      gradingHost.replaceChildren(head, body);
      return;
    }
    const hz = grading.horizons;
    const keys = Object.keys(hz).sort((a, b) => Number(a) - Number(b));
    const rows = Array.isArray(grading.rows) ? grading.rows : [];
    const pendingRows = rows.filter(r => r && (r.pending === true || r.status === 'pending')).length;
    const kids = [head];
    if (!keys.length) {
      kids.push(el('div', { class: 'state state-empty' }, el('div', { class: 'state-text' }, 'No graded decisions yet.')));
    } else {
      const body = keys.map(k => {
        const h = hz[k] || {};
        const wil = Array.isArray(h.wilson95) && h.wilson95.length === 2 && isNum(h.wilson95[0]) && isNum(h.wilson95[1])
          ? ` (${fmtNum(h.wilson95[0], 0)}\u2013${fmtNum(h.wilson95[1], 0)}%)` : '';
        const mean = isNum(h.meanOutcomePct) ? h.meanOutcomePct : (isNum(h.avgOutcomePct) ? h.avgOutcomePct : null);
        const icv = h.ic && typeof h.ic === 'object' ? h.ic.spearman : h.ic;
        const low = h.insufficient_sample === true || h.insufficientSample === true;
        return el('tr', { dataset: { horizon: k } },
          el('th', { scope: 'row' }, `${k} sessions`),
          el('td', { class: 'num' }, isNum(h.n) ? String(h.n) : '0'),
          el('td', { class: 'num' }, isNum(h.hitRatePct) ? `${fmtNum(h.hitRatePct, 1)}%${wil}` : '\u2013'),
          el('td', { class: 'num' }, isNum(mean) ? `${mean >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(mean), 2)} pp` : '\u2013'),
          el('td', { class: 'num' }, isNum(icv) ? fmtNum(icv, 2) : '\u2013'),
          el('td', {}, low
            ? el('span', { class: 'agent-chip is-deferred' }, el('span', { 'aria-hidden': 'true' }, '\u26a0 '), `Insufficient sample (n<${GRADING_MIN_N})`)
            : el('span', { class: 'agent-chip is-filled' }, el('span', { 'aria-hidden': 'true' }, '\u2713 '), 'Sample ok')),
          el('td', { class: 'num' }, isNum(h.pending) ? String(h.pending) : '\u2013'));
      });
      kids.push(el('div', { class: 'agent-table-wrap' }, el('table', { class: 'agent-table', id: 'agent-grading-table' },
        el('caption', { class: 'visually-hidden' }, 'Decision quality by horizon'),
        el('thead', {}, el('tr', {}, th('Horizon'), th('Graded', 'num'), th('Hit rate (Wilson 95%)', 'num'), th('Mean outcome', 'num'), th('Rank IC', 'num'), th('Sample'), th('Pending', 'num'))),
        el('tbody', {}, body))));
    }
    const pend = pendingRows || keys.reduce((s, k) => s + (isNum((hz[k] || {}).pending) ? hz[k].pending : 0), 0);
    kids.push(el('p', { class: 'agent-note muted', id: 'agent-grading-pending' }, `${pend} decision${pend === 1 ? '' : 's'} pending (not yet matured)`));
    if (Array.isArray(grading.warnings) && grading.warnings.length) {
      kids.push(el('p', { class: 'agent-note muted' }, grading.warnings.map(String).join(' \u00b7 ')));
    }
    if (gradingError) kids.push(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${gradingError}`));
    gradingHost.replaceChildren(...kids);
  }

  // ---------------------------------------------------------------- briefings (contract phase5 §7)
  async function loadBriefs() {
    const t = briefReq.begin();
    const res = await apiGet(`/api/paper/briefs?${practiceQuery()}&limit=${BRIEFS_SHOWN}`, { signal: t.signal });
    if (!t.current()) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) {
      briefs = res.data; briefsError = null;
    } else if (res.aborted) {
      return;
    } else {
      briefsError = res.error || 'invalid response';
    }
    paintBriefs();
  }

  function briefDecisionKind(d) {
    if (d.deferred) return 'deferred';
    if (d.valid === false) return 'invalid';
    return d.noChange ? 'nochange' : 'targets';
  }

  function briefItem(b) {
    const date = String(b.date || '');
    const eq = b.equity && typeof b.equity === 'object' ? b.equity : {};
    const dec = Array.isArray(b.decisions) ? b.decisions : [];
    const ord = b.orders && typeof b.orders === 'object' ? b.orders : {};
    const day = isNum(eq.dayPct) ? ` ${eq.dayPct >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(eq.dayPct), 2)}%` : '';
    const body = el('div', { class: 'agent-brief-body' });
    if (b.text) body.append(el('p', { class: 'agent-brief-text' }, String(b.text)));
    if (dec.length) {
      body.append(el('h4', { class: 'agent-minititle' }, 'Decisions'),
        el('ul', { class: 'agent-brief-decisions' }, dec.map(d => {
          const kind = briefDecisionKind(d);
          const targets = d.targets && typeof d.targets === 'object'
            ? Object.entries(d.targets).filter(([, v]) => isNum(v)).map(([k, v]) => `${k} ${fmtNum(v, 1)}%`).join(' \u00b7 ') : '';
          const rationale = el('div', { class: 'agent-rationale' });
          if (d.rationale) setMarkdown(rationale, String(d.rationale));
          return el('li', { dataset: { kind } },
            el('span', { class: 'agent-brief-slot' }, d.slot || (d.createdAt ? fmtDateTime24(d.createdAt) : '\u2013')),
            decisionChip(kind),
            targets ? el('span', { class: 'agent-brief-targets num' }, targets) : null,
            d.deferred ? el('span', { class: 'muted' }, String(d.deferred)) : null,
            d.error ? el('span', { class: 'warn-ink' }, String(d.error)) : null,
            d.rationale ? rationale : null);
        })));
    }
    if (isNum(ord.count)) {
      body.append(el('p', { class: 'agent-note muted' },
        `Orders: ${ord.count}${isNum(ord.filled) ? `, ${ord.filled} filled` : ''}${isNum(ord.shadow) && ord.shadow ? `, ${ord.shadow} shadow` : ''}${isNum(ord.filledValueEur) && ord.filledValueEur ? ` (${fmtEur(ord.filledValueEur)})` : ''}.`));
    }
    const det = el('details', { class: 'agent-brief-item', dataset: { date } },
      el('summary', {},
        el('span', { class: 'agent-decision-date' }, date || '\u2013'),
        el('span', { class: 'agent-decision-sum' }, `${isNum(eq.equityEur) ? fmtEur(eq.equityEur) : '\u2013'}${day}${b.mode ? ` \u00b7 ${b.mode}` : ''}`)),
      body);
    if (openBriefs.has(date)) det.open = true;
    det.addEventListener('toggle', () => { if (det.open) openBriefs.add(date); else openBriefs.delete(date); });
    return el('li', {}, det);
  }

  function paintBriefs() {
    if (!status) { briefsHost.replaceChildren(); briefsSig = ''; return; }
    const sig = JSON.stringify([briefs, briefsError]);
    if (sig === briefsSig) return;
    briefsSig = sig;
    const head = el('h3', { class: 'agent-subtitle', id: 'agent-briefs-title' }, 'Briefings');
    const body = el('div', { class: 'agent-briefs-body', id: 'agent-briefs-body' });
    if (!briefs) {
      if (briefsError) renderState(body, { error: `Briefings unavailable: ${briefsError}`, retry: () => loadBriefs() });
      else renderState(body, { loading: 'Loading briefings' });
      briefsHost.replaceChildren(head, body);
      return;
    }
    const items = briefs.items.slice().sort((a, b) => String(b.date || '').localeCompare(String(a.date || ''))).slice(0, BRIEFS_SHOWN);
    if (!items.length) {
      body.append(el('div', { class: 'state state-empty' }, el('div', { class: 'state-text' }, 'No briefings yet \u2014 one is written after each session close.')));
    } else {
      body.append(el('ul', { class: 'agent-brief-list', id: 'agent-briefs-list' }, items.map(briefItem)));
    }
    if (briefsError) body.append(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${briefsError}`));
    briefsHost.replaceChildren(head, body);
  }

  // ---------------------------------------------------------------- orders polling
  async function loadOrders() {
    const t = req.begin();
    const res = await apiGet(`/api/paper/orders?${practiceQuery()}&limit=50`, { signal: t.signal });
    if (!t.current()) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) {
      orders = res.data; ordersError = null;
    } else if (!res.aborted) {
      ordersError = res.error || 'invalid response';
    }
    paintOrders();
    paintHalt();
    paintControls();
    paintDecisions();
  }

  // ---------------------------------------------------------------- painting
  function paintStatus() {
    if (!status) { statusHost.replaceChildren(); return; }
    statusHost.replaceChildren(...buildAgent(status, { onStartSeason }));
    paintHalt();
    paintControls();
    paintDecisions();
  }

  function paintHalt() {
    const h = halt();
    if (!h) { haltHost.replaceChildren(); return; }
    const parts = [];
    if (h.reason) parts.push(String(h.reason));
    if (h.by) parts.push(`by ${h.by}`);
    if (h.at) parts.push(fmtDateTime24(h.at));
    haltHost.replaceChildren(el('div', { class: 'agent-halt', id: 'agent-halt-banner', role: 'alert' },
      el('span', { class: 'agent-halt-icon', 'aria-hidden': 'true' }, '\u26d4'),
      el('div', { class: 'agent-halt-text' },
        el('strong', {}, 'HALTED'),
        parts.length ? el('span', {}, ` \u2014 ${parts.join(' \u00b7 ')}`) : null)));
  }

  function paintControls() {
    if (!status) { controlsHost.replaceChildren(); controlsSig = ''; return; }
    const h = !!halt();
    const s = season();
    const showSeed = !!s.started && bucketEmpty && !hasOpenRows() && !h;
    const showRebalance = !!s.started;
    const sig = [status.mode, h, showSeed, showRebalance, busy].join('|');
    if (sig === controlsSig) return;
    controlsSig = sig;
    const hadFocus = controlsHost.contains(document.activeElement);
    const modeBtns = MODES.map(m => {
      const b = el('button', {
        type: 'button', class: 'agent-mode-btn', 'aria-pressed': status.mode === m ? 'true' : 'false',
        dataset: { mode: m }, id: `agent-mode-${m}`, text: MODE_LABEL[m],
        onclick: () => setMode(m),
      });
      if (busy) b.disabled = true;
      return b;
    });
    const actions = [];
    actions.push(h
      ? el('button', { type: 'button', class: 'btn btn-primary', id: 'agent-resume', text: 'Resume', onclick: resumeAction })
      : el('button', { type: 'button', class: 'btn btn-danger', id: 'agent-halt', text: 'Halt', onclick: haltAction }));
    if (showSeed) actions.push(el('button', { type: 'button', class: 'btn', id: 'agent-seed', text: 'Seed bucket from baseline', onclick: seedDialog }));
    if (showRebalance) actions.push(el('button', { type: 'button', class: 'btn', id: 'agent-rebalance', text: 'Rebalance\u2026', onclick: rebalanceDialog }));
    if (busy) actions.forEach(a => { a.disabled = true; });
    controlsHost.replaceChildren(
      el('div', { class: 'agent-mode' },
        el('span', { class: 'lbl', id: 'agent-mode-label' }, 'Mode'),
        el('div', { class: 'agent-mode-group', id: 'agent-mode', role: 'group', 'aria-labelledby': 'agent-mode-label' }, modeBtns),
        el('span', { class: 'agent-mode-help' }, MODE_HELP[status.mode] || '')),
      el('div', { class: 'agent-actions' }, actions));
    if (hadFocus && !busy) {
      const again = controlsHost.querySelector('[aria-pressed="true"]');
      if (again && document.activeElement === document.body) again.focus();
    }
  }

  function paintOrders() {
    if (!orders) {
      if (ordersError) renderState(ordersHost, { error: `Orders unavailable: ${ordersError}`, retry: () => loadOrders() });
      else renderState(ordersHost, { loading: 'Loading orders' });
      return;
    }
    const items = orders.items.slice(0, ORDERS_SHOWN);
    const head = el('h3', { class: 'agent-subtitle', id: 'agent-orders-title' }, 'Orders');
    if (!items.length) {
      ordersHost.removeAttribute('aria-busy');
      ordersHost.replaceChildren(head, el('div', { class: 'state state-empty' }, el('div', { class: 'state-text' }, 'No orders yet.')));
      return;
    }
    const table = el('table', { class: 'agent-table', id: 'agent-orders-table' },
      el('caption', { class: 'visually-hidden' }, 'Latest agent orders'),
      el('thead', {}, el('tr', {},
        th('Time'), th('Ticker'), th('Side'), th('Qty', 'num'), th('Limit', 'num'), th('State'), th('Filled', 'num'), th('Message'))),
      el('tbody', {}, items.map(r => {
        const st = String(r.state || 'unknown');
        const filledQ = isNum(r.filledQuantity) ? r.filledQuantity : 0;
        const filled = filledQ > 0
          ? `${fmtNum(filledQ, 4).replace(/\.?0+$/, '')}${isNum(r.avgFillPrice) ? ` @ ${fmtEur(r.avgFillPrice, { dec: 2 })}` : ''}`
          : '\u2013';
        return el('tr', { dataset: { intent: r.intentId || '', state: st } },
          el('td', {}, fmtDateTime24(r.updatedAt || r.createdAt)),
          el('td', {}, r.ticker || r.id || '\u2013'),
          el('td', {}, String(r.side || '\u2013')),
          el('td', { class: 'num' }, isNum(r.quantity) ? fmtNum(r.quantity, 4).replace(/\.?0+$/, '') : '\u2013'),
          el('td', { class: 'num' }, isNum(r.limitPrice) ? fmtEur(r.limitPrice, { dec: 2 }) : '\u2013'),
          el('td', {}, el('span', { class: `agent-chip is-${st}` },
            el('span', { 'aria-hidden': 'true' }, `${STATE_ICON[st] || '?'} `), STATE_LABEL[st] || st)),
          el('td', { class: 'num' }, filled),
          el('td', { class: 'agent-msg' }, r.message ? String(r.message) : ''));
      })));
    const kids = [head, el('div', { class: 'agent-table-wrap' }, table)];
    if (ordersError) kids.push(el('p', { class: 'agent-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${ordersError}`));
    ordersHost.removeAttribute('aria-busy');
    ordersHost.replaceChildren(...kids);
  }

  // ---------------------------------------------------------------- lifecycle
  function closeDialogs() { for (const d of [...dialogs]) d.close(); }

  function stop() {
    if (stopPoll) { stopPoll(); stopPoll = null; }
    if (stopGradePoll) { stopGradePoll(); stopGradePoll = null; }
    gradeReq.abort();
    if (stopBriefPoll) { stopBriefPoll(); stopBriefPoll = null; }
    briefReq.abort();
    briefs = null; briefsError = null; briefsSig = '';
    grading = null; gradingError = null;
    if (stopDecPoll) { stopDecPoll(); stopDecPoll = null; }
    req.abort();
    decReq.abort();
    closeDialogs();
    status = null; orders = null; ordersError = null; busy = false; controlsSig = '';
    decisions = null; decisionsError = null; weights = null; deciding = false; decisionsSig = '';
  }

  function update(next, opts = {}) {
    const first = !status;
    status = next;
    bucketEmpty = !!opts.bucketEmpty;
    paintStatus();
    if (!stopPoll) {
      if (first) { paintOrders(); paintDecisions(); paintGrading(); paintBriefs(); }
      stopPoll = usePolling(loadOrders, POLL_MS, { immediate: true });
      stopDecPoll = usePolling(loadDecisions, DECISIONS_POLL_MS, { immediate: true });
      stopGradePoll = usePolling(loadGrading, GRADING_POLL_MS, { immediate: true });
      stopBriefPoll = usePolling(loadBriefs, BRIEFS_POLL_MS, { immediate: true });
    }
  }

  return { node, update, stop, destroy: stop };
}
