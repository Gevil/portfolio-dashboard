// Overview: since-your-last-visit strip, hero strip, needs-attention cards, positions table /
// allocation, performance card (TWR / XIRR, perf-vs-role benchmark), risk and policy panels, and the
// host for the selected-holding detail (docked pane on wide screens, drawer otherwise).
// Panel logic lives in _since / _attn / _perf / _risk / _policy; this file wires data, state and DOM.
import { el, clear, fmtEur, fmtPct, fmtNum, fmtDateTime24, signCls, isNum } from '../util.js';
import { get, slot, subscribe } from '../store.js';
import { apiGet, apiSend, latestRequest } from '../api.js';
import { renderState, stamp, openDialog, usePolling, toast } from '../ui.js';
import * as data from '../data.js';
import { withBook, getBucket, setBucket, BUCKETS, BUCKET_LABEL } from '../book.js';
import { drawSparkline, createDonut, createIndexedChart } from '../charts.js';
import { buildSince, computeSince, isCollapsed, isDismissed, markDismissed, setCollapsed } from './_since.js';
import { buildAttentionCard, buildAttentionItems } from './_attn.js';
import { buildBenchmarks, buildCompare, buildSeasonHistory } from './_compare.js';
import { PERF_RANGES, basisOf, baselineSeries, benchmarkChoices, buildBasisWarning, buildKpis, buildPracticeKpis, kindText, perfBenchmarkLabel } from './_perf.js';
import { buildRisk } from './_risk.js';
import { buildPolicy } from './_policy.js';
import { buildTaxNl } from './_taxnl.js';
import { createAgentPanel } from './_agent.js';
import { createForecasterPanel } from './_forecaster.js';

const SORTS = [
  { key: 'weightPct', label: 'Weight', th: 'Weight' },
  { key: 'valueEur', label: 'Value', th: 'Value' },
  { key: 'dayPnlEur', label: 'Day \u20ac', th: 'Day' },
  { key: 'dayPct', label: 'Day %', th: null },
  { key: 'pnlEur', label: 'P&L \u20ac', th: 'P&L' },
  { key: 'pnlPct', label: 'P&L %', th: null },
  { key: 'mddPct', label: 'Drawdown', th: 'MDD' },
  { key: 'label', label: 'Name', th: null },
];
const LIGHT_LABEL = { green: 'Green', yellow: 'Yellow', red: 'Red' };
const LIGHT_CLS = { green: 'ok', yellow: 'warn', red: 'err' };
const PERF_REFRESH_MS = 600000;
const COMPARE_REFRESH_MS = 300000;
const RISK_KEY = 'pd.riskOpen';

const wideQuery = window.matchMedia('(min-width: 1200px)');

function readFlag(key, dflt) {
  try {
    const v = localStorage.getItem(key);
    return v == null ? dflt : v === '1';
  } catch (_) { return dflt; }
}
function writeFlag(key, on) {
  try { localStorage.setItem(key, on ? '1' : '0'); } catch (_) { /* storage blocked: not remembered */ }
}
function openSettingsTab(tab) {
  import('./settings.js').then(m => m.openSettings({ tab })).catch(e => console.error('settings failed', e));
}
/** Routes are hashes owned by main.js (#detail/<ID>, #alerts/<id>, #aiops, ...). */
function go(hash) {
  location.hash = hash;
}

export function mountOverview(root) {
  const cleanups = [];
  const view = {
    sort: 'weightPct', dir: 'desc', mode: 'table', perfRange: '3M', perfBench: 'perf', attnAll: false,
    sinceCollapsed: isCollapsed(), sinceDismissed: isDismissed(get('lastVisit')), riskOpen: readFlag(RISK_KEY, true),
  };
  const prevValues = new Map();
  // history = /api/portfolio/history payload, performance = /api/portfolio/performance payload (same range).
  const perf = { history: null, performance: null, ts: null, error: null, perfError: null, loading: false };

  // ------------------------------------------------------------ skeleton DOM
  const sinceHost = el('div', { class: 'since-host' });
  const heroHost = el('section', { class: 'hero', 'aria-label': 'Portfolio summary' });
  const attnHost = el('section', { class: 'card attn', id: 'attn-card', 'aria-labelledby': 'attn-title' });
  const posCard = el('section', { class: 'card pos-card', 'aria-labelledby': 'pos-title' });
  const agentCard = el('section', { class: 'card agent-card', id: 'agent-card', 'aria-labelledby': 'agent-title', hidden: true });
  const compareCard = el('section', { class: 'card compare-card', id: 'compare-card', 'aria-labelledby': 'compare-title', hidden: true });
  const perfCard = el('section', { class: 'card perf-card', id: 'perf-card', 'aria-labelledby': 'perf-title' });
  const policyCard = el('section', { class: 'card policy-card', id: 'policy-card', 'aria-labelledby': 'policy-title' });
  const taxCard = el('section', { class: 'card taxnl-card', id: 'taxnl-card', 'aria-labelledby': 'taxnl-title', hidden: true });
  const riskCard = el('section', { class: 'card risk-card', id: 'risk-card', 'aria-labelledby': 'risk-title' });
  const dock = el('aside', { class: 'ov-dock', 'aria-label': 'Holding detail', hidden: true });
  // Practice-only Forecaster + Candidates cards (own polling; started/stopped by renderForecaster()).
  const forecaster = createForecasterPanel({
    heldIds: () => new Set((((slot('portfolio').data || {}).positions) || []).map(p => String(p.id).toUpperCase())),
  });
  cleanups.push(() => forecaster.destroy());
  const main = el('div', { class: 'ov-main' }, el('div', { class: 'ov-left' }, posCard, agentCard, forecaster.forecasterNode, forecaster.candidatesNode, compareCard, perfCard, policyCard, taxCard, riskCard), dock);
  root.replaceChildren(el('div', { class: 'ov' }, sinceHost, heroHost, attnHost, main));

  // capabilities gate the practice-only panels (missing = all true, per contract §4; 'real' never carries capabilities).
  function capsOf() {
    const d = slot('portfolio').data;
    const c = d && d.capabilities;
    const has = (k) => !c || c[k] !== false;
    return { lots: has('lots'), policy: has('policy'), tax: has('tax'), editPositions: has('editPositions'), alertActions: has('alertActions'), attribution: has('attribution'), risk: has('risk'), analysis: has('analysis') };
  }

  // ------------------------------------------------------------ helpers
  const roleBench = () => {
    const d = slot('portfolio').data;
    return (d && d.benchmark) || null;
  };
  const roleLabel = () => (roleBench() && roleBench().label) || 'Benchmark';
  /** Label of the PERFORMANCE benchmark (hero KPI, KPI strip); falls back to the role benchmark. */
  const benchLabel = () => perfBenchmarkLabel(perf.history, perf.performance, data.labelOf, get('book') === 'practice') || roleLabel();
  const labelsOf = ids => (ids || []).map(id => data.labelOf(id)).join(', ');

  /** The /performance payload for the selected range, when it carries an excess return. */
  function excess() {
    const p = perf.performance;
    return p && p.range === view.perfRange && isNum(p.excessPct) ? p : null;
  }

  // ------------------------------------------------------------ hero
  function statCard({ label, value, valueCls = '', sub, subCls = '', note, noteCls = '', extra }) {
    return el('div', { class: 'card stat' },
      el('div', { class: 'lbl' }, label),
      el('div', { class: `stat-value num ${valueCls}` }, value),
      sub != null ? el('div', { class: `stat-sub num ${subCls}` }, sub) : null,
      note ? el('div', { class: `stat-note ${noteCls}` }, note) : null,
      extra || null);
  }

  function renderHero() {
    const pf = slot('portfolio');
    const d = pf.data;
    if (!d) {
      const box = el('div', { class: 'card hero-state' });
      renderState(box, pf.error
        ? { error: `Portfolio unavailable: ${pf.error}`, retry: () => data.refreshPortfolio() }
        : { loading: 'Loading portfolio' });
      heroHost.replaceChildren(box);
      return;
    }
    const t = d.totals || {};
    const b = d.benchmark || {};
    const pe = excess();
    const stale = !!pf.error || !!pf.stale;
    const practice = get('book') === 'practice';
    const cards = [];

    cards.push(statCard({
      label: 'Portfolio value',
      value: fmtEur(t.valueEur),
      valueCls: 'stat-hero',
      sub: stamp(d.asOf || pf.ts, { stale }),
      note: d.fx && d.fx.stale ? 'FX rate is stale' : '',
      noteCls: 'warn-ink',
    }));
    cards.push(statCard({
      label: 'Today',
      value: fmtEur(t.dayPnlEur, { sign: true }),
      valueCls: signCls(t.dayPnlEur),
      sub: fmtPct(t.dayPnlPct),
      subCls: signCls(t.dayPnlPct),
    }));

    const missing = Array.isArray(t.costMissing) ? t.costMissing : [];
    const practiceEmpty = practice && (!Array.isArray(d.positions) || !d.positions.length || (isNum(t.investedEur) && t.investedEur === 0));
    if (practiceEmpty) {
      cards.push(statCard({ label: 'Total P&L', value: fmtEur(0), valueCls: 'flat', sub: 'no positions yet', subCls: 'flat' }));
    } else if (!isNum(t.pnlEur) && practice) {
      cards.push(statCard({ label: 'Total P&L', value: '\u2013', valueCls: 'flat' }));
    } else if (!isNum(t.pnlEur)) {
      cards.push(statCard({
        label: 'Total P&L',
        value: 'Cost unknown',
        valueCls: 'flat stat-small',
        sub: el('button', { type: 'button', class: 'link-btn', onclick: () => openSettingsTab('positions'), text: 'Add cost basis' }),
      }));
    } else {
      cards.push(statCard({
        label: 'Total P&L',
        value: fmtEur(t.pnlEur, { sign: true }),
        valueCls: signCls(t.pnlEur),
        sub: `${fmtPct(t.pnlPct)}${isNum(t.investedEur) ? ` on ${fmtEur(t.investedEur)}` : ''}`,
        subCls: signCls(t.pnlPct),
        note: missing.length ? `excl. ${labelsOf(missing)} (cost unknown)` : '',
        noteCls: 'warn-ink',
      }));
    }

    cards.push(statCard({
      label: `vs ${benchLabel()} \u00b7 ${view.perfRange}`,
      value: pe ? `${pe.excessPct >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(pe.excessPct), 1)} pp` : '\u2013',
      valueCls: pe ? signCls(pe.excessPct) : 'flat',
      sub: practice && !d.benchmark ? null : `${roleLabel()} today ${fmtPct(b.dayPct)}`,
      subCls: 'flat',
      note: pe ? `portfolio ${fmtPct(pe.twrPct)} vs ${fmtPct(pe.perfBenchmarkPct)}` : (perf.perfError ? 'comparison unavailable' : ''),
    }));

    cards.push(lightCard());
    heroHost.replaceChildren(...cards);
  }

  function lightCard() {
    const ml = slot('marketLight');
    const d = ml.data;
    const btn = el('button', { type: 'button', class: 'card stat stat-light', onclick: () => { location.hash = 'market'; } });
    btn.append(el('div', { class: 'lbl' }, 'Market light'));
    if (!d) {
      btn.append(el('div', { class: 'stat-value stat-small flat' }, ml.error ? 'Unavailable' : 'Loading\u2026'));
      if (ml.error) btn.append(el('div', { class: 'stat-note' }, ml.error));
      return btn;
    }
    const cls = LIGHT_CLS[d.status] || '';
    const reason = Array.isArray(d.reasons) && d.reasons.length ? String(d.reasons[0]).replace(/\s*\((green|yellow|red)\)\s*$/i, '') : '';
    btn.append(...[
      el('div', { class: 'stat-value stat-small' }, el('span', { class: `chip ${cls ? 'chip-' + cls : ''}` }, el('span', { class: `dot ${cls ? 'dot-' + cls : ''}`, 'aria-hidden': 'true' }), LIGHT_LABEL[d.status] || 'Unknown'),
        d.data_quality === 'limited' ? el('span', { class: 'stat-tag', text: 'limited data' }) : null),
      reason ? el('div', { class: 'stat-note clamp-2' }, reason) : null,
    ].filter(Boolean));
    return btn;
  }

  // ------------------------------------------------------------ since your last visit
  function renderSince() {
    const pf = slot('portfolio');
    if (view.sinceDismissed || !pf.data) { sinceHost.replaceChildren(); return; }
    const last = get('lastVisit') || null;
    const model = computeSince({
      portfolio: pf.data, rules: slot('alertRules').data, alerts: slot('alerts').data,
      advice: slot('advice').data, events: slot('events').data, lastVisit: last,
    });
    sinceHost.replaceChildren(buildSince(model, {
      lastVisit: last, collapsed: view.sinceCollapsed, labelOf: data.labelOf,
      onToggle: () => {
        view.sinceCollapsed = !view.sinceCollapsed;
        setCollapsed(view.sinceCollapsed);
        renderSince();
        const b = sinceHost.querySelector('#since-toggle');
        if (b) b.focus({ preventScroll: true });
      },
      onDismiss: () => { view.sinceDismissed = true; markDismissed(last); renderSince(); },
    }));
  }

  // ------------------------------------------------------------ attention
  function renderAttention() {
    const items = buildAttentionItems({
      portfolio: slot('portfolio'), alerts: slot('alerts'), lane: slot('lane'),
      config: slot('config').data, rules: slot('alertRules').data, advice: slot('advice').data,
      events: slot('events').data, policy: slot('policy').data,
      labelOf: data.labelOf, positionOf: data.positionOf, go, settings: openSettingsTab,
      retryAlerts: () => data.refreshAlerts(), retryPortfolio: () => data.refreshPortfolio(),
    });
    const shown = view.attnAll ? items : items.slice(0, 3);
    const head = el('div', { class: 'card-head' },
      el('h2', { class: 'card-title', id: 'attn-title' }, 'Needs attention',
        items.length ? el('span', { class: 'count', text: String(items.length) }) : null));
    if (!items.length) {
      attnHost.classList.add('is-clear');
      const pf = slot('portfolio');
      attnHost.replaceChildren(head, el('p', { class: 'attn-clear' }, pf.data ? '\u2713 Nothing needs attention right now.' : 'Waiting for data\u2026'));
      return;
    }
    attnHost.classList.remove('is-clear');
    const list = el('ul', { class: 'attn-list', id: 'attn-list' }, shown.map(buildAttentionCard));
    const more = items.length > 3
      ? el('button', { type: 'button', class: 'link-btn', id: 'attn-more', 'aria-expanded': view.attnAll ? 'true' : 'false', onclick: () => { view.attnAll = !view.attnAll; renderAttention(); }, text: view.attnAll ? 'Show fewer' : `Show all ${items.length}` })
      : null;
    attnHost.replaceChildren(...[head, list, more].filter(Boolean));
  }

  // ------------------------------------------------------------ positions
  const posBody = el('div', { class: 'pos-body' });
  const donutHost = el('div', { class: 'pos-alloc' });
  const donut = createDonut(donutHost, { onSelect: id => data.selectHolding(id) });
  cleanups.push(() => donut.destroy());
  const stampSlot = el('span', { class: 'pos-stamp' });
  const modeBtns = ['table', 'allocation'].map(m => el('button', {
    type: 'button', 'aria-pressed': m === view.mode ? 'true' : 'false', dataset: { mode: m }, text: m === 'table' ? 'Table' : 'Allocation',
    onclick: () => { view.mode = m; paintMode(); renderPositions(); },
  }));
  const sortSelect = el('select', { class: 'pos-sort', 'aria-label': 'Sort positions by', onchange: e => { view.sort = e.target.value; view.dir = view.sort === 'label' ? 'asc' : 'desc'; renderPositions(); } },
    SORTS.map(s => el('option', { value: s.key, text: `Sort: ${s.label}` })));
  posCard.append(
    el('div', { class: 'card-head' },
      el('h2', { class: 'card-title', id: 'pos-title' }, 'Positions'),
      el('div', { class: 'card-tools' }, stampSlot, sortSelect, el('div', { class: 'seg', role: 'group', 'aria-label': 'Positions view' }, modeBtns))),
    posBody, donutHost);

  function paintMode() {
    modeBtns.forEach(b => b.setAttribute('aria-pressed', b.dataset.mode === view.mode ? 'true' : 'false'));
    posBody.hidden = view.mode !== 'table';
    donutHost.hidden = view.mode !== 'allocation';
    sortSelect.hidden = view.mode !== 'table';
  }

  function sortedPositions(list) {
    const k = view.sort;
    const dir = view.dir === 'asc' ? 1 : -1;
    return [...list].sort((a, b) => {
      const av = a[k], bv = b[k];
      if (k === 'label') return dir * String(av || a.id).localeCompare(String(bv || b.id));
      const an = isNum(av), bn = isNum(bv);
      if (!an && !bn) return 0;
      if (!an) return 1;
      if (!bn) return -1;
      return dir * (av - bv);
    });
  }

  function twoLine(eur, pct, { missingText } = {}) {
    if (!isNum(eur) && !isNum(pct)) return el('span', { class: 'flat' }, missingText || '\u2013');
    return el('span', { class: `two-line ${signCls(isNum(eur) ? eur : pct)}` },
      el('span', { class: 'num tl-main' }, isNum(eur) ? fmtEur(eur, { sign: true }) : '\u2013'),
      el('span', { class: 'num tl-sub' }, fmtPct(pct)));
  }

  function posRow(p, prices) {
    const sel = get('selectedId') === p.id;
    const name = p.label || p.id;
    const canvas = el('canvas', { class: 'spark', 'aria-hidden': 'true', width: 90, height: 26 });
    const item = prices.get(p.id);
    const spark = item && Array.isArray(item.sparkline) ? item.sparkline : null;
    const tr = el('tr', {
      class: 'pos-row' + (sel ? ' is-selected' : ''), dataset: { symbol: p.id },
      onclick: () => data.selectHolding(p.id),
    },
      el('td', { class: 'c-name' }, el('button', {
        type: 'button', class: 'pos-btn', 'aria-current': sel ? 'true' : null, dataset: { symbol: p.id },
        'aria-label': `${name}, ${fmtEur(p.valueEur)}, today ${fmtPct(p.dayPct)}. Show details`,
      }, el('span', { class: 'pos-name' }, name),
        el('span', { class: 'pos-meta' }, p.kind ? p.kind.toUpperCase() : '', p.sector ? el('span', { class: 'pos-sector', dataset: { label: 'Sector' }, text: ` \u00b7 ${p.sector}` }) : null, p.stale ? el('span', { class: 'chip chip-warn pos-stale', title: `Price as of ${fmtDateTime24(p.priceAsOf)}`, text: 'stale' }) : null))),
      el('td', { class: 'c-weight num', dataset: { label: 'Weight' } }, fmtPct(p.weightPct, { sign: false, dec: 1 })),
      el('td', { class: 'c-value num', dataset: { label: 'Value' } }, fmtEur(p.valueEur)),
      el('td', { class: 'c-day', dataset: { label: 'Today' } }, twoLine(p.dayPnlEur, p.dayPct)),
      el('td', { class: 'c-pnl', dataset: { label: 'P&L' } }, isNum(p.pnlEur) || isNum(p.pnlPct) ? twoLine(p.pnlEur, p.pnlPct) : el('span', { class: 'flat', title: 'Cost basis not set', text: 'cost n/a' })),
      el('td', { class: `c-mdd num ${isNum(p.mddPct) && Math.abs(p.mddPct) >= 15 ? 'down' : 'muted'}`, dataset: { label: 'MDD' } }, isNum(p.mddPct) ? fmtPct(-Math.abs(p.mddPct), { dec: 1 }) : '\u2013'),
      el('td', { class: 'c-spark', dataset: { label: 'Trend' } }, canvas));
    const prev = prevValues.get(p.id);
    if (isNum(prev) && isNum(p.valueEur) && Math.abs(prev - p.valueEur) > 0.005) tr.classList.add(p.valueEur > prev ? 'flash-up' : 'flash-down');
    prevValues.set(p.id, p.valueEur);
    return { tr, canvas, spark, up: isNum(p.dayPct) ? p.dayPct >= 0 : null };
  }

  function sortHeader(s) {
    const active = view.sort === s.key || (s.th && ((s.key === 'dayPnlEur' && view.sort === 'dayPct') || (s.key === 'pnlEur' && view.sort === 'pnlPct')));
    const aria = active ? (view.dir === 'asc' ? 'ascending' : 'descending') : 'none';
    return el('th', { scope: 'col', 'aria-sort': aria, class: `th-${s.key}` },
      el('button', {
        type: 'button', class: 'th-btn', dataset: { sort: s.key },
        onclick: () => {
          if (view.sort === s.key) view.dir = view.dir === 'asc' ? 'desc' : 'asc';
          else { view.sort = s.key; view.dir = s.key === 'label' ? 'asc' : 'desc'; }
          renderPositions();
        },
      }, s.th, el('span', { class: 'th-arrow', 'aria-hidden': 'true', text: active ? (view.dir === 'asc' ? '\u25b4' : '\u25be') : '' })));
  }

  function renderPositions() {
    const pf = slot('portfolio');
    const d = pf.data;
    stampSlot.replaceChildren(pf.ts ? stamp(pf.ts, { stale: !!pf.error }) : '');
    sortSelect.value = view.sort;
    paintMode();
    if (!d) {
      renderState(posBody, pf.error ? { error: pf.error, retry: () => data.refreshPortfolio() } : { loading: 'Loading positions' });
      donut.update([], null);
      return;
    }
    const positions = Array.isArray(d.positions) ? d.positions : [];
    if (!positions.length) {
      renderState(posBody, { empty: get('book') === 'practice' ? 'No positions yet \u2014 the agent has not traded in this bucket.' : 'No positions yet. Add shares in Settings \u2192 Positions.' });
      donut.update([], null);
      return;
    }
    const focusedId = document.activeElement && document.activeElement.closest && posBody.contains(document.activeElement)
      ? (document.activeElement.dataset.symbol || document.activeElement.dataset.sort) : null;
    const focusedSort = document.activeElement && posBody.contains(document.activeElement) && document.activeElement.dataset.sort;
    const priceMap = new Map();
    const prices = slot('prices').data;
    if (Array.isArray(prices)) prices.forEach(p => priceMap.set(p.ticker || p.id, p));

    const tbody = el('tbody');
    const sparks = [];
    for (const p of sortedPositions(positions)) {
      const r = posRow(p, priceMap);
      tbody.append(r.tr);
      sparks.push(r);
    }
    const t = d.totals || {};
    const tfoot = el('tfoot', null, el('tr', { class: 'pos-total' },
      el('th', { scope: 'row', class: 'c-name' }, 'Total'),
      el('td', { class: 'c-weight num', dataset: { label: 'Weight' } }, '100%'),
      el('td', { class: 'c-value num', dataset: { label: 'Value' } }, fmtEur(t.valueEur)),
      el('td', { class: 'c-day', dataset: { label: 'Today' } }, twoLine(t.dayPnlEur, t.dayPnlPct)),
      el('td', { class: 'c-pnl', dataset: { label: 'P&L' } }, isNum(t.pnlEur) ? twoLine(t.pnlEur, t.pnlPct) : el('span', { class: 'flat', text: 'cost n/a' })),
      el('td', { class: 'c-mdd' }), el('td', { class: 'c-spark' })));
    const table = el('table', { class: 'pos-table' },
      el('caption', { class: 'visually-hidden', text: 'Positions, sortable by column' }),
      el('thead', null, el('tr', null,
        el('th', { scope: 'col', class: 'th-label', 'aria-sort': view.sort === 'label' ? (view.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
          el('button', { type: 'button', class: 'th-btn', dataset: { sort: 'label' }, onclick: () => { if (view.sort === 'label') view.dir = view.dir === 'asc' ? 'desc' : 'asc'; else { view.sort = 'label'; view.dir = 'asc'; } renderPositions(); } },
            'Holding', el('span', { class: 'th-arrow', 'aria-hidden': 'true', text: view.sort === 'label' ? (view.dir === 'asc' ? '\u25b4' : '\u25be') : '' }))),
        ...['weightPct', 'valueEur', 'dayPnlEur', 'pnlEur', 'mddPct'].map(k => sortHeader(SORTS.find(s => s.key === k))),
        el('th', { scope: 'col' }, 'Trend'))),
      tbody, tfoot);

    const children = [table];
    const b = d.benchmark;
    if (b) {
      const item = priceMap.get(b.id);
      const c = el('canvas', { class: 'spark', 'aria-hidden': 'true', width: 90, height: 26 });
      const bs = item && Array.isArray(item.sparkline) ? item.sparkline : null;
      sparks.push({ canvas: c, spark: bs, up: isNum(b.dayPct) ? b.dayPct >= 0 : null });
      children.push(el('div', { class: 'pos-bench' },
        el('span', { class: 'lbl' }, 'Benchmark'),
        el('span', { class: 'pos-bench-name' }, b.label || b.id),
        el('span', { class: `num ${signCls(b.dayPct)}` }, `${fmtPct(b.dayPct)} today`),
        el('span', { class: 'muted pos-bench-note' }, 'comparison only'),
        c));
    }
    if (slot('prices').error && !Array.isArray(prices)) {
      children.push(el('p', { class: 'pos-note warn-ink', role: 'status' }, `Trend lines unavailable: ${slot('prices').error}`));
    }
    posBody.replaceChildren(...children);
    posBody.removeAttribute('aria-busy');
    for (const s of sparks) drawSparkline(s.canvas, s.spark || [], { up: s.up });
    donut.update(sortedPositions(positions).map(p => ({ id: p.id, label: p.label || p.id, weightPct: p.weightPct, valueEur: p.valueEur })), get('selectedId'));

    if (focusedId && !focusedSort) {
      const again = posBody.querySelector(`.pos-btn[data-symbol="${CSS.escape(focusedId)}"]`);
      if (again) again.focus({ preventScroll: true });
    } else if (focusedSort) {
      const again = posBody.querySelector(`.th-btn[data-sort="${CSS.escape(focusedSort)}"]`);
      if (again) again.focus({ preventScroll: true });
    }
  }

  // ------------------------------------------------------------ performance card
  const perfChartHost = el('div', { class: 'perf-chart' });
  const perfKpiHost = el('div', { class: 'perf-kpi-host' });
  const perfNotes = el('div', { class: 'perf-notes' });
  const perfSummary = el('p', { class: 'perf-summary num' });
  const perfStamp = el('span', { class: 'perf-stamp' });
  const rangeBtns = PERF_RANGES.map(r => el('button', {
    type: 'button', 'aria-pressed': r === view.perfRange ? 'true' : 'false', dataset: { range: r }, text: r,
    onclick: () => { view.perfRange = r; paintRange(); loadPerf(); renderHero(); },
  }));
  const benchBtns = ['perf', 'role'].map(k => el('button', {
    type: 'button', 'aria-pressed': k === view.perfBench ? 'true' : 'false', dataset: { bench: k }, text: k === 'perf' ? 'Performance benchmark' : 'Role benchmark',
    onclick: () => { view.perfBench = k; paintPerf(); },
  }));
  const benchSeg = el('div', { class: 'seg perf-bench-seg', id: 'perf-bench-toggle', role: 'group', 'aria-label': 'Benchmark line', hidden: true }, benchBtns);
  let perfVersion = null;   // Practice: selected strategyVersion (null = current)
  function paintRange() { rangeBtns.forEach(b => b.setAttribute('aria-pressed', b.dataset.range === view.perfRange ? 'true' : 'false')); }
  const perfTitle = el('h2', { class: 'card-title', id: 'perf-title' });
  perfCard.append(
    el('div', { class: 'card-head' },
      perfTitle,
      el('div', { class: 'card-tools' }, perfStamp, el('div', { class: 'seg', id: 'perf-range', role: 'group', 'aria-label': 'Chart range' }, rangeBtns))),
    benchSeg, perfKpiHost, perfNotes, perfChartHost, perfSummary);
  const perfChart = createIndexedChart(perfChartHost);
  cleanups.push(() => perfChart.destroy());
  const histReq = latestRequest();
  const kpiReq = latestRequest();
  cleanups.push(() => { histReq.abort(); kpiReq.abort(); });

  function paintPerf() {
    perfStamp.replaceChildren(perf.ts ? stamp(perf.ts, { stale: !!perf.error }) : '');
    const h = perf.history;
    const ix = h && h.indexed;
    const n = ix && Array.isArray(ix.portfolio) ? ix.portfolio.length : 0;
    const practice = get('book') === 'practice';
    perfTitle.textContent = practice ? 'Bucket vs baselines (indexed %)' : 'Portfolio vs benchmark (indexed %)';
    const choices = practice ? { perf: null, role: null, toggle: false } : benchmarkChoices(h);
    const line = view.perfBench === 'role' && choices.role ? choices.role : choices.perf;
    benchSeg.hidden = !choices.toggle;
    if (choices.toggle) {
      benchBtns[0].textContent = `${choices.perf.label} \u00b7 ${kindText(choices.perf.kind)}`;
      benchBtns[1].textContent = `${choices.role.label} \u00b7 ${kindText(choices.role.kind)}`;
    }
    benchBtns.forEach(b => b.setAttribute('aria-pressed', (b.dataset.bench === 'role') === (line === choices.role && choices.toggle) ? 'true' : 'false'));

    const p = perf.performance;
    if (p && p.range === view.perfRange) {
      perfKpiHost.replaceChildren(buildKpis(p, benchLabel()),
        ...(practice ? [buildPracticeKpis(p, { version: perfVersion, onVersion: v => { perfVersion = v; paintPerf(); } })] : []));
    } else if (perf.perfError) {
      perfKpiHost.replaceChildren(el('p', { class: 'pos-note warn-ink', role: 'status' }, `Return figures unavailable: ${perf.perfError}`));
    } else {
      perfKpiHost.replaceChildren();
    }
    perfNotes.replaceChildren(...(basisOf(h, p && p.range === view.perfRange ? p : null) === 'current_shares'
      ? [buildBasisWarning(() => openSettingsTab('positions'))] : []));

    if (!h) {
      if (practice) perfChart.reset();
      perfChart.setState(perf.error ? { error: perf.error } : { loading: true });
      perfSummary.textContent = '';
      return;
    }
    if (n < 2) {
      perfChart.reset();
      perfChart.setState({ empty: 'Not enough history to compare yet.' });
      perfSummary.textContent = '';
      return;
    }
    perfChart.setState(null);
    if (practice) {
      // Practice book draws the buy-and-hold/trend/momentum baselines (plan §8.1) instead of a benchmark line.
      // An open-universe season (SXR8 / real-weights benchmarks present) draws only the three buy & hold lines:
      // EUNL (primary), SXR8 and the real weights, so the legend stays unambiguous.
      const all = baselineSeries(h);
      const open = all.some(b => b.id === 'buy_hold_sxr8' || b.id === 'buy_hold_real');
      const baselines = open ? all.filter(b => /^buy_hold/.test(b.id)) : all;
      perfChart.setData({ portfolio: ix.portfolio, baselines, portfolioLabel: 'Bucket' });
      const lp = ix.portfolio[ix.portfolio.length - 1];
      const bh = baselines[0];
      const lb = bh && bh.series[bh.series.length - 1];
      const others = baselines.slice(1).map(b => ({ label: b.label, last: b.series[b.series.length - 1] })).filter(b => b.last && isNum(b.last.pct))
        .map(b => ` \u00b7 ${b.label} ${fmtPct(b.last.pct)}`).join('');
      perfSummary.textContent = lp && lb && isNum(lp.pct) && isNum(lb.pct)
        ? `Bucket ${fmtPct(lp.pct)} \u00b7 ${bh.label} ${fmtPct(lb.pct)}${others} \u00b7 difference vs ${bh.label} ${lp.pct - lb.pct >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(lp.pct - lb.pct), 1)} pp over ${view.perfRange}`
        : (baselines.length ? '' : 'No practice season yet \u2014 start one from the Agent card.');
      if (perf.error) perfSummary.textContent += ` \u00b7 refresh failed: ${perf.error}`;
      return;
    }
    const label = line ? `${line.label} (${kindText(line.kind)})` : 'Benchmark';
    perfChart.setData({ portfolio: ix.portfolio, benchmark: line ? line.series : [], portfolioLabel: 'Portfolio', benchmarkLabel: label });
    const lp = ix.portfolio[ix.portfolio.length - 1];
    const lb = line && line.series[line.series.length - 1];
    perfSummary.textContent = lp && lb && isNum(lp.pct) && isNum(lb.pct)
      ? `Portfolio ${fmtPct(lp.pct)} \u00b7 ${label} ${fmtPct(lb.pct)} \u00b7 difference ${lp.pct - lb.pct >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(lp.pct - lb.pct), 1)} pp over ${view.perfRange}`
      : '';
    if (perf.error) perfSummary.textContent += ` \u00b7 refresh failed: ${perf.error}`;
  }

  async function loadPerf() {
    const range = view.perfRange;
    const q = encodeURIComponent(range);
    const th = histReq.begin();
    const tk = kpiReq.begin();
    perf.loading = true;
    if (!perf.history || perf.history.range !== range) perf.history = null;
    if (!perf.performance || perf.performance.range !== range) perf.performance = null;
    paintPerf();
    await Promise.all([
      apiGet(withBook(`/api/portfolio/history?range=${q}`), { signal: th.signal, timeoutMs: 30000 }).then(res => {
        if (!th.current()) return;
        perf.loading = false;
        if (res.ok) { perf.history = res.data; perf.ts = Date.now(); perf.error = null; } else { perf.error = res.error; }
        paintPerf();
        renderHero();
        renderCompare();
      }),
      apiGet(withBook(`/api/portfolio/performance?range=${q}`), { signal: tk.signal, timeoutMs: 30000 }).then(res => {
        if (!tk.current()) return;
        if (res.ok) { perf.performance = res.data; perf.perfError = null; } else { perf.perfError = res.error; }
        paintPerf();
        renderHero();
        renderSeasonHistory();
      }),
    ]);
  }
  cleanups.push(usePolling(loadPerf, PERF_REFRESH_MS));


  // ------------------------------------------------------------ risk panel (collapsible)
  const riskBody = el('div', { class: 'risk-body', id: 'risk-body' });
  const riskStamp = el('span', { class: 'risk-stamp' });
  const riskToggle = el('button', {
    type: 'button', class: 'btn btn-ghost btn-sm', id: 'risk-toggle', 'aria-controls': 'risk-body',
    onclick: () => { view.riskOpen = !view.riskOpen; writeFlag(RISK_KEY, view.riskOpen); paintRiskOpen(); },
  });
  riskCard.append(
    el('div', { class: 'card-head' },
      el('h2', { class: 'card-title', id: 'risk-title' }, 'Risk'),
      el('div', { class: 'card-tools' }, riskStamp, riskToggle)),
    riskBody);
  function paintRiskOpen() {
    riskBody.hidden = !view.riskOpen;
    riskToggle.setAttribute('aria-expanded', view.riskOpen ? 'true' : 'false');
    riskToggle.textContent = view.riskOpen ? 'Collapse' : 'Expand';
  }
  function renderRisk() {
    if (!capsOf().risk) { riskCard.hidden = true; return; }
    riskCard.hidden = false;
    const s = slot('risk');
    riskStamp.replaceChildren(s.ts ? stamp(s.ts, { stale: !!s.error }) : '');
    if (!s.data) {
      renderState(riskBody, s.error ? { error: `Risk unavailable: ${s.error}`, retry: () => data.refreshRisk() } : { loading: 'Loading risk' });
      return;
    }
    riskBody.replaceChildren(...[buildRisk(s.data, data.labelOf),
      s.error ? el('p', { class: 'pos-note warn-ink', role: 'status' }, `Refresh failed: ${s.error}`) : null].filter(Boolean));
    riskBody.removeAttribute('aria-busy');
  }

  // ------------------------------------------------------------ policy card
  const policyBody = el('div', { class: 'policy-body', id: 'policy-body' });
  const policyStamp = el('span', { class: 'policy-stamp' });
  policyCard.append(
    el('div', { class: 'card-head' },
      el('h2', { class: 'card-title', id: 'policy-title' }, 'Allocation policy'),
      el('div', { class: 'card-tools' }, policyStamp)),
    policyBody);
  function renderPolicy() {
    if (!capsOf().policy) { policyCard.hidden = true; return; }
    policyCard.hidden = false;
    const s = slot('policy');
    policyStamp.replaceChildren(s.ts ? stamp(s.ts, { stale: !!s.error }) : '');
    if (!s.data) {
      renderState(policyBody, s.error ? { error: `Policy unavailable: ${s.error}`, retry: () => data.refreshPolicy() } : { loading: 'Loading policy' });
      return;
    }
    policyBody.replaceChildren(...[...buildPolicy(s.data, { labelOf: data.labelOf, onSetTargets: () => openSettingsTab('policy') }),
      s.error ? el('p', { class: 'pos-note warn-ink', role: 'status' }, `Refresh failed: ${s.error}`) : null].filter(Boolean));
    policyBody.removeAttribute('aria-busy');
  }

  // ------------------------------------------------------------ Tax (NL box 3) card (Real book, residency NL only)
  const TAX_REFRESH_MS = 600000;
  const taxState = { data: null, error: null, ts: null, asked: false };
  const taxBody = el('div', { class: 'taxnl-body', id: 'taxnl-body' });
  const taxStamp = el('span', { class: 'taxnl-stamp' });
  taxCard.append(
    el('div', { class: 'card-head' },
      el('h2', { class: 'card-title', id: 'taxnl-title' }, 'Tax (NL box 3)'),
      el('div', { class: 'card-tools' }, taxStamp)),
    taxBody);
  const taxReq = latestRequest();
  cleanups.push(() => taxReq.abort());
  /** Shown only for the Real context with taxResidency NL (the CZ affordances are the mirror image). */
  const taxOn = () => get('book') !== 'practice' && capsOf().tax && data.taxResidencyOf() === 'NL';

  async function loadTax() {
    if (!taxOn()) return;
    const t = taxReq.begin();
    const res = await apiGet('/api/tax', { signal: t.signal, timeoutMs: 30000 });
    if (!t.current() || res.aborted || !taxOn()) return;
    if (res.ok && res.data && typeof res.data === 'object' && String(res.data.residency).toUpperCase() === 'NL') {
      taxState.data = res.data; taxState.error = null; taxState.ts = Date.now();
    } else {
      taxState.error = res.ok ? 'The server did not answer with the NL tax payload.' : (res.error || 'invalid response');
    }
    renderTax();
  }
  cleanups.push(usePolling(loadTax, TAX_REFRESH_MS));

  function renderTax() {
    if (!taxOn()) {
      taxCard.hidden = true;
      taxReq.abort();
      taxState.data = null; taxState.error = null; taxState.ts = null; taxState.asked = false;
      return;
    }
    taxCard.hidden = false;
    if (!taxState.asked) { taxState.asked = true; loadTax(); }
    taxStamp.replaceChildren(taxState.ts ? stamp(taxState.ts, { stale: !!taxState.error }) : '');
    if (!taxState.data) {
      renderState(taxBody, taxState.error ? { error: `Tax estimate unavailable: ${taxState.error}`, retry: () => loadTax() } : { loading: 'Loading tax estimate' });
      return;
    }
    taxBody.replaceChildren(...[...buildTaxNl(taxState.data, { onOpenSettings: () => openSettingsTab('policy') }),
      taxState.error ? el('p', { class: 'pos-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${taxState.error}`) : null].filter(Boolean));
    taxBody.removeAttribute('aria-busy');
  }

  // ------------------------------------------------------------ agent card (practice book only)
  const agentBody = el('div', { class: 'agent-body' });
  const seasonHistoryHost = el('div', { class: 'agent-history-host' });
  // Bucket switch (Practice only): Daily | Intraday, remembered in localStorage 'pd.bucket' (book.js).
  const bucketBtns = BUCKETS.map(id => el('button', {
    type: 'button', class: 'bucket-btn', id: `bucket-${id}`, dataset: { bucket: id }, text: BUCKET_LABEL[id],
    'aria-pressed': id === getBucket() ? 'true' : 'false',
    onclick: () => setBucket(id),
  }));
  const bucketSeg = el('div', { class: 'seg bucket-switch', id: 'bucket-switch', role: 'group', 'aria-label': 'Paper bucket' }, bucketBtns);
  function paintBucketSwitch() {
    bucketBtns.forEach(b => b.setAttribute('aria-pressed', b.dataset.bucket === getBucket() ? 'true' : 'false'));
  }
  agentCard.append(
    el('div', { class: 'card-head' }, el('h2', { class: 'card-title', id: 'agent-title' }, 'Agent'), el('div', { class: 'card-tools' }, bucketSeg)),
    agentBody, seasonHistoryHost);

  async function startSeason() {
    const res = await apiSend('POST', '/api/paper/season', { book: 'practice', bucket: getBucket() });
    if (res.ok) {
      toast('Practice season started.', { type: 'ok' });
      data.refreshPaperStatus();
      data.refreshPortfolio();
    } else {
      toast(`Could not start the season: ${res.error}`, { type: 'error' });
    }
  }

  const agentPanel = createAgentPanel({
    onStartSeason: startSeason,
    onChanged: () => { data.refreshPaperStatus(); data.refreshPortfolio(); },
  });
  cleanups.push(() => agentPanel.destroy());

  function renderAgent() {
    if (get('book') !== 'practice') { agentPanel.stop(); agentCard.hidden = true; return; }
    agentCard.hidden = false;
    paintBucketSwitch();
    const bucket = getBucket();
    const s = slot('paperStatus');
    // a status block of the other bucket (switch in flight) counts as "not loaded yet"
    if (!s.data || (s.data.bucketId && s.data.bucketId !== bucket)) {
      agentPanel.stop();
      renderState(agentBody, s.error ? { error: `Agent unavailable: ${s.error}`, retry: () => data.refreshPaperStatus() } : { loading: 'Loading agent status' });
      return;
    }
    if (s.data.enabled === false) {
      agentPanel.stop();
      renderState(agentBody, { empty: s.data.reason ? `Practice agent disabled: ${s.data.reason}` : 'Practice agent is disabled.' });
      return;
    }
    const bs = s.data.buckets && s.data.buckets[bucket];
    if (bs && bs.enabled === false) {
      agentPanel.stop();
      agentBody.replaceChildren(el('div', { class: 'state state-empty', id: 'agent-bucket-disabled', role: 'status' },
        el('div', { class: 'state-text' }, `${BUCKET_LABEL[bucket]} bucket not enabled${bs.mode ? ` (mode: ${bs.mode})` : ''}`)));
      agentBody.removeAttribute('aria-busy');
      return;
    }
    const pf = slot('portfolio').data;
    const bucketEmpty = !!pf && Array.isArray(pf.positions) && pf.positions.length === 0;
    agentPanel.update(s.data, { bucketEmpty });
    if (agentBody.firstChild !== agentPanel.node) agentBody.replaceChildren(agentPanel.node);
    agentBody.removeAttribute('aria-busy');
  }

  // ------------------------------------------------------------ compare card (practice book only)
  const cmp = { data: null, error: null, ts: null, disabled: false };
  const compareBody = el('div', { class: 'compare-card-body' });
  const compareStamp = el('span', { class: 'compare-stamp' });
  compareCard.append(
    el('div', { class: 'card-head' }, el('h2', { class: 'card-title', id: 'compare-title' }, 'Compare'), el('div', { class: 'card-tools' }, compareStamp)),
    compareBody);
  const compareReq = latestRequest();
  cleanups.push(() => compareReq.abort());

  async function loadCompare() {
    if (get('book') !== 'practice') return;
    const t = compareReq.begin();
    const res = await apiGet(withBook('/api/paper/compare'), { signal: t.signal, timeoutMs: 30000 });
    if (!t.current()) return;
    if (res.ok && res.data && typeof res.data === 'object') {
      cmp.data = res.data; cmp.error = null; cmp.disabled = false; cmp.ts = Date.now();
    } else if (res.aborted) {
      return;
    } else {
      cmp.error = res.error || 'invalid response';
      cmp.disabled = res.status === 503 && /paper_disabled/.test(String(res.error));
    }
    renderCompare();
  }
  cleanups.push(usePolling(loadCompare, COMPARE_REFRESH_MS));

  function renderCompare() {
    if (get('book') !== 'practice') { compareCard.hidden = true; return; }
    compareCard.hidden = false;
    compareStamp.replaceChildren(cmp.ts ? stamp(cmp.ts, { stale: !!cmp.error }) : '');
    if (!cmp.data) {
      if (cmp.disabled) renderState(compareBody, { empty: 'Paper trading is disabled, so there is nothing to compare.' });
      else renderState(compareBody, cmp.error ? { error: `Comparison unavailable: ${cmp.error}`, retry: () => loadCompare() } : { loading: 'Loading comparison' });
      return;
    }
    const ps = slot('paperStatus').data;
    compareBody.replaceChildren(...[buildCompare(cmp.data, ps && ps.buckets),
      buildBenchmarks(perf.history, view.perfRange),
      cmp.error ? el('p', { class: 'pos-note warn-ink', role: 'status' }, `Refresh failed, showing older data: ${cmp.error}`) : null].filter(Boolean));
    compareBody.removeAttribute('aria-busy');
  }

  // ------------------------------------------------------------ forecaster + candidates (practice book only)
  function renderForecaster() {
    const practice = get('book') === 'practice';
    forecaster.forecasterNode.hidden = !practice;
    forecaster.candidatesNode.hidden = !practice;
    if (!practice) { forecaster.stop(); return; }
    const s = slot('paperStatus');
    forecaster.update(s.data || null);
    forecaster.repaint();
  }

  // Earlier seasons (s1 / i1) as read-only history under the Agent card: dates come from the performance
  // payload's strategy-version windows, plus status.season.previous when the server sends it.
  function renderSeasonHistory() {
    if (get('book') !== 'practice') { seasonHistoryHost.replaceChildren(); return; }
    const ps = slot('paperStatus').data;
    const current = ps && ps.bucket && ps.bucket.strategyVersion;
    const node = buildSeasonHistory(perf.performance && perf.performance.versions, ps && ps.season && ps.season.previous, current);
    seasonHistoryHost.replaceChildren(...(node ? [node] : []));
  }

  // ------------------------------------------------------------ detail host
  const detailRoot = el('div', { class: 'ov-detail-root', dataset: { host: 'dock' } });
  let detail = null;
  let detailLoad = null;
  let drawer = null;
  let silentClose = false;

  function ensureDetail() {
    if (detail) return Promise.resolve(detail);
    if (!detailLoad) {
      detailLoad = import('./detail.js').then(m => {
        detail = m.mountDetail(detailRoot, { onClose: () => data.selectHolding(null) });
        return detail;
      }).catch(e => {
        detailLoad = null;
        console.error('detail failed to load', e);
        renderState(detailRoot, { error: `Detail view could not be loaded: ${e.message || e}`, retry: () => syncDetail() });
        return null;
      });
    }
    return detailLoad;
  }

  function focusRow(id) {
    const b = id && posBody.querySelector(`.pos-btn[data-symbol="${CSS.escape(id)}"]`);
    if (b) b.focus({ preventScroll: true });
  }

  function closeDrawerSilently() {
    if (!drawer) return;
    silentClose = true;
    const d = drawer;
    drawer = null;
    d.close();
    silentClose = false;
  }

  let lastShown = null;
  async function syncDetail() {
    const id = get('selectedId');
    const wide = wideQuery.matches;
    if (!id) {
      closeDrawerSilently();
      dock.hidden = true;
      main.classList.remove('has-detail');
      if (detail) detail.show(null);
      if (lastShown) focusRow(lastShown);
      lastShown = null;
      return;
    }
    const label = data.labelOf(id);
    if (wide) {
      closeDrawerSilently();
      detailRoot.dataset.host = 'dock';
      if (detailRoot.parentNode !== dock) dock.replaceChildren(detailRoot);
      dock.hidden = false;
      main.classList.add('has-detail');
    } else {
      dock.hidden = true;
      main.classList.remove('has-detail');
      detailRoot.dataset.host = 'drawer';
      if (!drawer) {
        const opener = document.activeElement;
        drawer = openDialog({
          title: label, kind: 'drawer', size: 'lg', body: detailRoot,
          onClose: () => {
            if (silentClose) return;
            drawer = null;
            data.selectHolding(null);
            if (opener && !opener.isConnected) focusRow(lastShown);
          },
        });
      } else drawer.setTitle(label);
    }
    lastShown = id;
    const d = await ensureDetail();
    if (d && get('selectedId') === id) d.show(id);
  }
  dock.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); data.selectHolding(null); }
  });
  const onWide = () => { if (get('selectedId')) syncDetail(); };
  wideQuery.addEventListener('change', onWide);
  cleanups.push(() => wideQuery.removeEventListener('change', onWide));

  // ------------------------------------------------------------ scheduling
  const dirty = new Set();
  let raf = 0;
  function schedule(...parts) {
    parts.forEach(p => dirty.add(p));
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const todo = [...dirty];
      dirty.clear();
      if (todo.includes('hero')) renderHero();
      if (todo.includes('since')) renderSince();
      if (todo.includes('attn')) renderAttention();
      if (todo.includes('pos')) renderPositions();
      if (todo.includes('risk')) renderRisk();
      if (todo.includes('policy')) renderPolicy();
      if (todo.includes('tax')) renderTax();
      if (todo.includes('agent')) { renderAgent(); renderForecaster(); renderSeasonHistory(); }
      if (todo.includes('compare')) renderCompare();
    });
  }
  const sub = (key, ...parts) => cleanups.push(subscribe(key, () => schedule(...parts)));
  sub('portfolio', 'hero', 'since', 'attn', 'pos', 'risk', 'policy', 'tax', 'agent');
  sub('prices', 'pos');
  sub('alerts', 'since', 'attn');
  sub('config', 'attn', 'tax');
  // A saved config (taxNL values, residency) changes the estimate: ask again on the next render.
  cleanups.push(subscribe('config', () => { taxState.asked = false; }));
  sub('lane', 'attn');
  sub('marketLight', 'hero');
  sub('watchlist', 'pos', 'attn', 'risk', 'policy');
  sub('advice', 'since', 'attn');
  sub('events', 'since', 'attn');
  sub('alertRules', 'since', 'attn');
  sub('policy', 'attn', 'policy');
  sub('risk', 'risk');
  sub('paperStatus', 'agent', 'compare');
  sub('lastVisit', 'since');
  // Switching books: the previous book's history/performance cache is local to this module (not a
  // store slot), so reset it here and reload; capability-gated cards and the Agent card recompute too.
  cleanups.push(subscribe('book', () => {
    perf.history = null; perf.performance = null; perf.error = null; perf.perfError = null; perf.ts = null;
    cmp.data = null; cmp.error = null; cmp.ts = null; cmp.disabled = false;
    // The chart keeps its last dataset + legend across setState(); drop them so the previous book's numbers cannot linger.
    perfChart.reset();
    paintPerf();
    loadPerf();
    loadCompare();
    taxState.data = null; taxState.error = null; taxState.ts = null; taxState.asked = false;
    schedule('hero', 'pos', 'risk', 'policy', 'tax', 'agent', 'compare');
  }));
  // Bucket switch: stop the Agent panel (aborts its in-flight requests), drop the old bucket's figures and
  // reload; loadPerf/loadCompare begin new "latest" requests, which abort the old bucket's.
  cleanups.push(subscribe('bucket', (b, prev) => {
    if (b === prev) return;
    agentPanel.stop();
    perf.history = null; perf.performance = null; perf.error = null; perf.perfError = null; perf.ts = null;
    cmp.data = null; cmp.error = null; cmp.ts = null; cmp.disabled = false;
    perfChart.reset();
    paintBucketSwitch();
    paintPerf();
    if (get('book') === 'practice') { loadPerf(); loadCompare(); }
    schedule('hero', 'pos', 'agent', 'compare');
  }));
  cleanups.push(subscribe('selectedId', () => { schedule('pos'); syncDetail(); }));
  const onVis = () => { if (!document.hidden) schedule('hero', 'since', 'attn', 'pos'); };
  document.addEventListener('visibilitychange', onVis);
  cleanups.push(() => document.removeEventListener('visibilitychange', onVis));

  paintMode(); paintRiskOpen();
  renderHero(); renderSince(); renderAttention(); renderPositions(); renderRisk(); renderPolicy(); renderTax(); renderAgent(); renderForecaster(); renderSeasonHistory(); renderCompare(); paintPerf(); paintRange();
  loadPerf();
  loadCompare();
  if (get('selectedId')) syncDetail();

  return function unmount() {
    cleanups.forEach(fn => { try { fn(); } catch (e) { console.error(e); } });
    if (detail) detail.destroy();
    closeDrawerSilently();
    cancelAnimationFrame(raf);
    clear(root);
  };
}
