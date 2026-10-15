// Risk panel body from GET /api/portfolio/risk (C4): direct vs look-through exposure bars, currency
// split, annual volatility (portfolio vs benchmark) and beta, drawdown from peak + max drawdown,
// the correlation table, concentration and the notes. Every block degrades when its key is missing.
import { el, fmtNum, fmtPct, isNum, signCls } from '../util.js';
import { bar, shortDate, tableWrap } from './_common.js';

function block(title, ...children) {
  return el('section', { class: 'rk-block' }, el('h3', { class: 'lbl rk-title', text: title }), ...children);
}

function kv(rows) {
  const dl = el('dl', { class: 'kv rk-kv' });
  for (const [label, value, cls] of rows) dl.append(el('div', null, el('dt', { text: label }), el('dd', { class: cls || null }, value)));
  return dl;
}

function exposureTable(ex, labelOf) {
  const direct = ex.direct || {};
  const look = ex.lookThrough || {};
  const ids = [...new Set([...Object.keys(direct), ...Object.keys(look)])]
    .sort((a, b) => (look[b] ?? direct[b] ?? 0) - (look[a] ?? direct[a] ?? 0));
  if (!ids.length) return el('p', { class: 'muted', text: 'No exposure data.' });
  const cell = (pct, cls) => el('td', { class: 'rk-cell' },
    el('span', { class: 'num', text: isNum(pct) ? fmtPct(pct, { sign: false, dec: 1 }) : '\u2013' }), bar(pct, cls));
  const body = el('tbody');
  for (const id of ids) {
    body.append(el('tr', { dataset: { symbol: id } }, el('th', { scope: 'row', text: labelOf(id) }), cell(direct[id], 'bar-direct'), cell(look[id], 'bar-look')));
  }
  return tableWrap(el('table', { class: 'rk-table rk-exposure' },
    el('caption', { class: 'visually-hidden', text: 'Direct and look-through exposure by holding' }),
    el('thead', null, el('tr', null, el('th', { scope: 'col', text: 'Holding' }), el('th', { scope: 'col', text: 'Direct' }), el('th', { scope: 'col', text: 'Look-through' }))),
    body));
}

function corrClass(v, diag) {
  if (diag) return 'corr-self';
  const a = Math.abs(v);
  return a >= 0.8 ? 'corr-hi' : a >= 0.5 ? 'corr-mid' : 'corr-lo';
}

function correlationTable(c, labelOf) {
  const ids = (c && c.ids) || [];
  const m = (c && c.matrix) || [];
  if (ids.length < 2 || m.length !== ids.length) return el('p', { class: 'muted', text: 'Not enough history for correlations.' });
  const body = el('tbody');
  ids.forEach((rid, i) => {
    body.append(el('tr', null, el('th', { scope: 'row', text: labelOf(rid) }),
      ...ids.map((_, j) => {
        const v = m[i] && m[i][j];
        return el('td', { class: `num rk-corr ${isNum(v) ? corrClass(v, i === j) : ''}`.trim(), text: isNum(v) ? fmtNum(v, 2) : '\u2013' });
      })));
  });
  return tableWrap(el('table', { class: 'rk-table rk-correlation' },
    el('caption', { class: 'visually-hidden', text: 'Correlation of daily returns between holdings' }),
    el('thead', null, el('tr', null, el('td'), ...ids.map(id => el('th', { scope: 'col', text: labelOf(id) })))),
    body));
}

/** -> HTMLElement (div.rk-grid). `r` = the /api/portfolio/risk payload, `labelOf(id)` -> display label. */
export function buildRisk(r, labelOf) {
  const ex = r.exposures || {};
  const vol = r.volatility || {};
  const dd = r.drawdown || {};
  const con = r.concentration || {};
  const grid = el('div', { class: 'rk-grid' });

  grid.append(block('Exposure: direct vs look-through', exposureTable(ex, labelOf)));

  const ccy = Object.entries(ex.byCurrency || {}).sort((a, b) => b[1] - a[1]);
  grid.append(block('Currency split', ccy.length
    ? el('ul', { class: 'rk-chips' }, ccy.map(([c, pct]) => el('li', { class: 'chip', text: `${c} ${fmtPct(pct, { sign: false, dec: 1 })}` })))
    : el('p', { class: 'muted', text: 'No currency data.' })));

  grid.append(block(`Volatility (annual${vol.window ? `, ${vol.window}` : ''}) and beta`, kv([
    ['Portfolio', isNum(vol.portfolioAnnPct) ? fmtPct(vol.portfolioAnnPct, { sign: false, dec: 1 }) : '\u2013', 'num'],
    ['Benchmark', isNum(vol.perfBenchmarkAnnPct) ? fmtPct(vol.perfBenchmarkAnnPct, { sign: false, dec: 1 }) : '\u2013', 'num'],
    ['Beta', isNum(r.beta) ? fmtNum(r.beta, 2) : '\u2013', 'num'],
  ])));

  grid.append(block('Drawdown', kv([
    ['From peak', el('span', { class: signCls(dd.fromPeakPct) }, isNum(dd.fromPeakPct) ? `\u25bc ${fmtPct(dd.fromPeakPct, { dec: 1 })}` : '\u2013', dd.peakDate ? el('span', { class: 'muted', text: ` (peak ${shortDate(dd.peakDate)})` }) : null), 'num'],
    ['Max drawdown', el('span', { class: signCls(dd.maxDrawdownPct) }, isNum(dd.maxDrawdownPct) ? `\u25bc ${fmtPct(dd.maxDrawdownPct, { dec: 1 })}` : '\u2013'), 'num'],
  ])));

  grid.append(block('Concentration', kv([
    ['Top 2 holdings', isNum(con.top2Pct) ? fmtPct(con.top2Pct, { sign: false, dec: 1 }) : '\u2013', 'num'],
    ['Largest holding', con.largestId ? `${labelOf(con.largestId)} ${isNum(con.largestPct) ? fmtPct(con.largestPct, { sign: false, dec: 1 }) : ''}`.trim() : '\u2013', 'num'],
  ])));

  const wide = block('Correlation', correlationTable(r.correlation, labelOf));
  wide.classList.add('rk-wide');
  grid.append(wide);

  const notes = Array.isArray(ex.notes) ? ex.notes.filter(Boolean) : [];
  if (notes.length) {
    const nb = block('Notes', el('ul', { class: 'rk-notes' }, notes.map(n => el('li', { text: String(n) }))));
    nb.classList.add('rk-wide');
    grid.append(nb);
  }
  return grid;
}
