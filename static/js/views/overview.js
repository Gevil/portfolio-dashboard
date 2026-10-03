// Overview: hero strip, attention list, positions table / allocation, portfolio-vs-benchmark chart,
// and the host for the selected-holding detail (docked pane on wide screens, drawer otherwise).
import { el, clear, fmtEur, fmtPct, fmtNum, fmtDateTime24, signCls, isNum } from '../util.js';
import { get, slot, subscribe } from '../store.js';
import { apiGet, latestRequest } from '../api.js';
import { renderState, stamp, openDialog, usePolling } from '../ui.js';
import * as data from '../data.js';
import { drawSparkline, createDonut, createIndexedChart } from '../charts.js';

const PERF_RANGES = ['1M', '3M', '6M', '1Y'];
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
const SEV_ORDER = { urgent: 0, warn: 1, info: 2 };
const SEV_TEXT = { urgent: 'Urgent', warn: 'Warning', info: 'Info' };

const wideQuery = window.matchMedia('(min-width: 1200px)');

function openSettingsTab(tab) {
  import('./settings.js').then(m => m.openSettings({ tab })).catch(e => console.error('settings failed', e));
}
function openAlerts() {
  import('./alertcenter.js').then(m => m.openAlertCenter()).catch(e => console.error('alert center failed', e));
}

export function mountOverview(root) {
  const cleanups = [];
  const view = { sort: 'weightPct', dir: 'desc', mode: 'table', perfRange: '3M', attnAll: false };
  const prevValues = new Map();
  const perf = { res: null, ts: null, error: null, data: null, loading: false };

  // ------------------------------------------------------------ skeleton DOM
  const heroHost = el('section', { class: 'hero', 'aria-label': 'Portfolio summary' });
  const attnHost = el('section', { class: 'card attn', 'aria-labelledby': 'attn-title' });
  const posCard = el('section', { class: 'card pos-card', 'aria-labelledby': 'pos-title' });
  const perfCard = el('section', { class: 'card perf-card', 'aria-labelledby': 'perf-title' });
  const dock = el('aside', { class: 'ov-dock', 'aria-label': 'Holding detail', hidden: true });
  const main = el('div', { class: 'ov-main' }, el('div', { class: 'ov-left' }, posCard, perfCard), dock);
  root.replaceChildren(el('div', { class: 'ov' }, heroHost, attnHost, main));

  // ------------------------------------------------------------ helpers
  const bench = () => {
    const d = slot('portfolio').data;
    return (d && d.benchmark) || null;
  };
  const benchLabel = () => (bench() && bench().label) || 'Benchmark';
  const labelsOf = ids => (ids || []).map(id => data.labelOf(id)).join(', ');

  function vsBenchmark() {
    const ix = perf.data && perf.data.indexed;
    if (!ix || !Array.isArray(ix.portfolio) || !Array.isArray(ix.benchmark)) return null;
    const p = ix.portfolio[ix.portfolio.length - 1];
    const b = ix.benchmark[ix.benchmark.length - 1];
    if (!p || !b || !isNum(p.pct) || !isNum(b.pct)) return null;
    return { portfolio: p.pct, benchmark: b.pct, diff: p.pct - b.pct };
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
    const vb = vsBenchmark();
    const stale = !!pf.error || !!pf.stale;
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
    if (!isNum(t.pnlEur)) {
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
      value: vb ? `${vb.diff >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(vb.diff), 1)} pp` : '\u2013',
      valueCls: vb ? signCls(vb.diff) : 'flat',
      sub: `${benchLabel()} today ${fmtPct(b.dayPct)}`,
      subCls: 'flat',
      note: vb ? `portfolio ${fmtPct(vb.portfolio)} vs ${fmtPct(vb.benchmark)}` : (perf.error ? 'comparison unavailable' : ''),
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

  // ------------------------------------------------------------ attention
  function attentionItems() {
    const items = [];
    const pf = slot('portfolio');
    const d = pf.data;
    const cfgRules = (slot('config').data || {}).alertRules;
    const def = (cfgRules && cfgRules.default) || {};
    const thr = isNum(def.thresholdPct) ? def.thresholdPct : 3;
    const hot = isNum(def.hotPct) ? def.hotPct : 5;
    const open = id => ({ label: 'Open', fn: () => data.selectHolding(id) });

    const al = slot('alerts');
    if (al.error && !al.data) items.push({ sev: 'info', text: `Alerts could not be loaded: ${al.error}`, action: { label: 'Retry', fn: () => data.refreshAlerts() } });
    const unacked = al.data && Array.isArray(al.data.items) ? al.data.items.filter(a => !a.acked) : [];
    unacked.slice(0, 3).forEach(a => items.push({
      sev: a.severity === 'urgent' || a.severity === 'warn' ? a.severity : 'info',
      text: a.title + (a.message && a.message !== a.title ? ` \u2014 ${a.message}` : ''),
      meta: fmtDateTime24(a.time),
      action: a.ticker && data.positionOf(a.ticker) ? open(a.ticker) : { label: 'Alerts', fn: openAlerts },
    }));
    if (unacked.length > 3) items.push({ sev: 'info', text: `${unacked.length - 3} more unread alerts`, action: { label: 'Alerts', fn: openAlerts } });

    if (pf.error) {
      items.push({ sev: d ? 'warn' : 'urgent', text: `Portfolio could not be refreshed: ${pf.error}${d ? ` (showing data from ${fmtDateTime24(pf.ts)})` : ''}`, action: { label: 'Retry', fn: () => data.refreshPortfolio() } });
    }
    if (d) {
      (d.warnings || []).forEach(w => items.push({ sev: 'warn', text: w.message || w.code, action: Array.isArray(w.ids) && w.ids.length === 1 && data.positionOf(w.ids[0]) ? open(w.ids[0]) : null }));
      if (d.fx && d.fx.stale) items.push({ sev: 'warn', text: `FX rate is stale (${d.fx.source || 'unknown source'}, as of ${fmtDateTime24(d.fx.asOf)}) \u2013 EUR values may be off.` });
      const missing = (d.totals && d.totals.costMissing) || [];
      const warned = (d.warnings || []).some(w => /cost/i.test(w.code || ''));
      if (missing.length && !warned) items.push({ sev: 'info', text: `Cost basis missing for ${labelsOf(missing)}; total P&L excludes it.`, action: { label: 'Settings', fn: () => openSettingsTab('positions') } });
      for (const p of d.positions || []) {
        const name = p.label || p.id;
        if (isNum(p.dayPct) && Math.abs(p.dayPct) >= thr) {
          items.push({ sev: Math.abs(p.dayPct) >= hot ? 'urgent' : 'warn', text: `${name} ${fmtPct(p.dayPct)} today (alert threshold \u00b1${thr}%)`, action: open(p.id) });
        }
        if (isNum(p.mddPct) && Math.abs(p.mddPct) >= 15) {
          items.push({ sev: 'warn', text: `${name} is ${fmtPct(-Math.abs(p.mddPct), { dec: 1 })} from its 52-week high`, action: open(p.id) });
        }
        if (p.stale) items.push({ sev: 'info', text: `${name}: price is stale (last tick ${fmtDateTime24(p.priceAsOf)})`, action: open(p.id) });
      }
    }
    const lane = slot('lane').data;
    if (lane && lane.role === 'fallback') {
      items.push({ sev: 'info', text: `AI answers are coming from the fallback model${lane.model ? ` (${lane.model})` : ''}.`, action: { label: 'AI Ops', fn: () => { location.hash = 'aiops'; } } });
    }
    return items.sort((a, b) => SEV_ORDER[a.sev] - SEV_ORDER[b.sev]);
  }

  function renderAttention() {
    const items = attentionItems();
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
    const list = el('ul', { class: 'attn-list' });
    for (const it of shown) {
      list.append(el('li', { class: `attn-item sev-${it.sev}` },
        el('span', { class: 'attn-sev' }, SEV_TEXT[it.sev]),
        el('span', { class: 'attn-text' }, it.text, it.meta ? el('span', { class: 'attn-meta num', text: ` \u00b7 ${it.meta}` }) : null),
        it.action ? el('button', { type: 'button', class: 'btn btn-sm', onclick: it.action.fn, text: it.action.label }) : null));
    }
    const more = items.length > 3
      ? el('button', { type: 'button', class: 'link-btn', 'aria-expanded': view.attnAll ? 'true' : 'false', onclick: () => { view.attnAll = !view.attnAll; renderAttention(); }, text: view.attnAll ? 'Show fewer' : `Show all ${items.length}` })
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
        el('span', { class: 'pos-meta' }, p.kind ? p.kind.toUpperCase() : '', p.stale ? el('span', { class: 'chip chip-warn pos-stale', title: `Price as of ${fmtDateTime24(p.priceAsOf)}`, text: 'stale' }) : null))),
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
      renderState(posBody, { empty: 'No positions yet. Add shares in Settings \u2192 Positions.' });
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

  // ------------------------------------------------------------ performance chart
  const perfChartHost = el('div', { class: 'perf-chart' });
  const perfSummary = el('p', { class: 'perf-summary num' });
  const perfStamp = el('span', { class: 'perf-stamp' });
  const rangeBtns = PERF_RANGES.map(r => el('button', {
    type: 'button', 'aria-pressed': r === view.perfRange ? 'true' : 'false', dataset: { range: r }, text: r,
    onclick: () => { view.perfRange = r; paintRange(); loadPerf(); renderHero(); },
  }));
  function paintRange() { rangeBtns.forEach(b => b.setAttribute('aria-pressed', b.dataset.range === view.perfRange ? 'true' : 'false')); }
  perfCard.append(
    el('div', { class: 'card-head' },
      el('h2', { class: 'card-title', id: 'perf-title' }, 'Portfolio vs benchmark (indexed %)'),
      el('div', { class: 'card-tools' }, perfStamp, el('div', { class: 'seg', role: 'group', 'aria-label': 'Chart range' }, rangeBtns))),
    perfChartHost, perfSummary);
  const perfChart = createIndexedChart(perfChartHost);
  cleanups.push(() => perfChart.destroy());
  const perfReq = latestRequest();
  cleanups.push(() => perfReq.abort());

  function paintPerf() {
    perfStamp.replaceChildren(perf.ts ? stamp(perf.ts, { stale: !!perf.error }) : '');
    const ix = perf.data && perf.data.indexed;
    const n = ix && Array.isArray(ix.portfolio) ? ix.portfolio.length : 0;
    if (!perf.data) {
      perfChart.setState(perf.error ? { error: perf.error } : { loading: true });
      perfSummary.textContent = '';
      return;
    }
    if (n < 2) {
      perfChart.setState({ empty: 'Not enough history to compare yet.' });
      perfSummary.textContent = '';
      return;
    }
    perfChart.setState(null);
    perfChart.setData({ portfolio: ix.portfolio, benchmark: ix.benchmark || [], portfolioLabel: 'Portfolio', benchmarkLabel: (perf.data.benchmark && perf.data.benchmark.label) || benchLabel() });
    const vb = vsBenchmark();
    perfSummary.textContent = vb
      ? `Portfolio ${fmtPct(vb.portfolio)} \u00b7 ${benchLabel()} ${fmtPct(vb.benchmark)} \u00b7 difference ${vb.diff >= 0 ? '+' : '\u2212'}${fmtNum(Math.abs(vb.diff), 1)} pp over ${view.perfRange}`
      : '';
    if (perf.error) perfSummary.textContent += ` \u00b7 refresh failed: ${perf.error}`;
  }

  async function loadPerf() {
    const range = view.perfRange;
    const t = perfReq.begin();
    perf.loading = true;
    if (!perf.data || perf.data.range !== range) { perf.data = null; paintPerf(); }
    const res = await apiGet(`/api/portfolio/history?range=${encodeURIComponent(range)}`, { signal: t.signal, timeoutMs: 30000 });
    if (!t.current()) return;
    perf.loading = false;
    if (res.ok) { perf.data = res.data; perf.ts = Date.now(); perf.error = null; } else { perf.error = res.error; }
    paintPerf();
    renderHero();
  }
  cleanups.push(usePolling(loadPerf, 120000));

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
      if (todo.includes('attn')) renderAttention();
      if (todo.includes('pos')) renderPositions();
    });
  }
  const sub = (key, ...parts) => cleanups.push(subscribe(key, () => schedule(...parts)));
  sub('portfolio', 'hero', 'attn', 'pos');
  sub('prices', 'pos');
  sub('alerts', 'attn');
  sub('config', 'attn');
  sub('lane', 'attn');
  sub('marketLight', 'hero');
  sub('watchlist', 'pos');
  cleanups.push(subscribe('selectedId', () => { schedule('pos'); syncDetail(); }));
  const onVis = () => { if (!document.hidden) schedule('hero', 'attn', 'pos'); };
  document.addEventListener('visibilitychange', onVis);
  cleanups.push(() => document.removeEventListener('visibilitychange', onVis));

  paintMode();
  renderHero(); renderAttention(); renderPositions(); paintPerf(); paintRange();
  loadPerf();
  if (get('selectedId')) syncDetail();

  return function unmount() {
    cleanups.forEach(fn => { try { fn(); } catch (e) { console.error(e); } });
    if (detail) detail.destroy();
    closeDrawerSilently();
    cancelAnimationFrame(raf);
    clear(root);
  };
}
