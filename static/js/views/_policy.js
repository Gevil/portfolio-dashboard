// Policy card body from GET /api/policy (C9): drift table (current vs target, band, status chip),
// max-single-stock breaches, "Next EUR X goes to ..." from the contribution allocation, and the
// 10-month SMA trend flags with a "changed" marker. `configured:false` -> a "Set targets" call to
// action. Status is always text + arrow, never colour alone.
import { el, fmtEur, fmtNum, fmtPct, isNum } from '../util.js';
import { chip, shortDate, tableWrap } from './_common.js';

const STATUS = {
  ok: { text: 'in band', cls: 'chip-ok', mark: '\u2713' },
  over: { text: 'over', cls: 'chip-warn', mark: '\u25b2' },
  under: { text: 'under', cls: 'chip-warn', mark: '\u25bc' },
};

function pct(n, dec = 1) {
  return isNum(n) ? fmtPct(n, { sign: false, dec }) : '\u2013';
}

function driftTable(rows, labelOf) {
  const body = el('tbody');
  for (const r of rows) {
    const st = STATUS[r.status] || { text: String(r.status || '?'), cls: '', mark: '' };
    body.append(el('tr', { dataset: { symbol: r.id, status: r.status || '' } },
      el('th', { scope: 'row', text: labelOf(r.id) }),
      el('td', { class: 'num', text: pct(r.currentPct) }),
      el('td', { class: 'num', text: pct(r.targetPct, 0) }),
      el('td', { class: 'num pl-band', text: `${pct(r.bandLoPct)}\u2013${pct(r.bandHiPct)}` }),
      el('td', null, chip(`${st.mark} ${st.text}`.trim(), st.cls))));
  }
  return tableWrap(el('table', { class: 'rk-table pl-drift' },
    el('caption', { class: 'visually-hidden', text: 'Allocation drift: current weight against target and band' }),
    el('thead', null, el('tr', null, ...['Holding', 'Current', 'Target', 'Band', 'Status'].map(h => el('th', { scope: 'col', text: h })))),
    body));
}

function contributionLine(c, labelOf) {
  if (!c || !isNum(c.amountEur) || c.amountEur <= 0) return null;
  const parts = Object.entries(c.allocation || {}).filter(([, v]) => isNum(v) && v > 0).sort((a, b) => b[1] - a[1]);
  if (!parts.length) return null;
  return el('p', { class: 'pl-next', id: 'policy-contribution' },
    el('strong', { text: `Next ${fmtEur(c.amountEur, { dec: 0 })} goes to ` }),
    parts.map(([id, v]) => `${labelOf(id)} ${fmtEur(v, { dec: 0 })}`).join(', '), '.');
}

function trendList(rows, labelOf) {
  return el('ul', { class: 'pl-trend' }, rows.map(t => {
    const above = t.state === 'above';
    const tip = `${isNum(t.close) ? `month-end close ${fmtNum(t.close)}` : ''}${isNum(t.sma10m) ? ` vs 10-month SMA ${fmtNum(t.sma10m)}` : ''}${t.monthEnd ? ` (${shortDate(t.monthEnd)})` : ''}`.trim();
    return el('li', { class: 'pl-trend-item', dataset: { symbol: t.id, state: t.state || '' } },
      el('span', { class: 'pl-trend-name', text: labelOf(t.id) }),
      chip(`${above ? '\u25b2 above' : '\u25bc below'} 10-mo SMA`, above ? 'chip-ok' : 'chip-warn', tip),
      t.changed ? chip('changed', 'chip-info', 'Crossed the 10-month SMA at the last month end') : null);
  }));
}

/** -> array of nodes. opts: {labelOf(id), onSetTargets()} */
export function buildPolicy(p, { labelOf, onSetTargets }) {
  if (!p.configured) {
    return [el('div', { class: 'pl-unset' },
      el('p', { class: 'muted', text: 'No target allocation yet. Set targets to see drift, the next contribution and trend flags.' }),
      el('button', { type: 'button', class: 'btn btn-primary btn-sm', id: 'policy-set-targets', onclick: onSetTargets, text: 'Set targets' }))];
  }
  const nodes = [];
  const rows = Array.isArray(p.drift) ? p.drift : [];
  nodes.push(rows.length ? driftTable(rows, labelOf) : el('p', { class: 'muted', text: 'No drift data yet.' }));

  const ms = p.maxSingleStock || {};
  const breaches = Array.isArray(ms.breaches) ? ms.breaches : [];
  if (isNum(ms.limitPct)) {
    nodes.push(el('p', { class: 'pl-max', id: 'policy-max-single' },
      el('span', { class: 'lbl', text: 'Max single stock' }), ` ${pct(ms.limitPct, 0)}: `,
      breaches.length
        ? el('span', { class: 'pl-breaches' }, breaches.map(id => chip(`\u25b2 ${labelOf(id)} ${pct(p.current && p.current[id])}`, 'chip-err')))
        : el('span', { class: 'up', text: '\u2713 no breaches' })));
  }
  const line = contributionLine(p.contribution, labelOf);
  if (line) nodes.push(line);

  const trend = Array.isArray(p.trend) ? p.trend : [];
  if (trend.length) {
    nodes.push(el('h3', { class: 'lbl rk-title', text: 'Trend (10-month SMA)' }), trendList(trend, labelOf));
  }
  return nodes;
}
