// Tax (NL box 3) card body from GET /api/tax when the residency is NL: per year the value on the peildatum,
// forfait vs tegenbewijs and which one applies, the dividend tax credit and the net estimate; status chips for
// provisional / proposal parameters; the next-year projection (law vs proposal); the aangifte checklist and the
// disclaimer. Informational, never tax advice. Status is always text, never colour alone. Pure DOM builders:
// no store access, no network; links from the payload only as http(s).
import { el, fmtEur, fmtNum, isNum, safeUrl } from '../util.js';
import { chip, tableWrap } from './_common.js';

const isObj = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const PARAM_LABEL = {
  pBank: 'bank return', pOther: 'other assets return', pDebt: 'debt rate',
  taxFree: 'tax-free amount', debtThreshold: 'debt threshold', rate: 'rate',
};
const text = v => (v == null ? '' : String(v));

const STATUS = {
  final: { text: '\u2713 final', cls: 'chip-ok', tip: 'The percentages are final.' },
  provisional: { text: 'provisional', cls: 'chip-warn', tip: 'Some percentages are replaced after year end: the figure can still change.' },
  proposal: { text: 'proposal, not law', cls: 'chip-info', tip: 'Proposed parameters, not in force.' },
};
const APPLIES = {
  forfait: { text: 'forfait applies', tip: 'The fictitious-return (forfait) result is the lower one.' },
  tegenbewijs: { text: 'tegenbewijs applies', tip: 'The actual-return rebuttal is the lower one.' },
};
const SCENARIO = {
  law: { text: 'law', cls: 'chip-ok', tip: 'Rules in force' },
  proposal: { text: 'proposal, not law', cls: 'chip-info', tip: 'Proposed rules, not in force' },
};

/** A rate as '1.28%'. Fractions (0.0128) are scaled; values of 1 and up are already percent. */
function pctText(v) {
  if (!isNum(v)) return '\u2013';
  return `${fmtNum(v < 1 ? v * 100 : v, 2)}%`;
}

/** camelCase key -> 'camel case' for a label. */
const words = key => text(key).replace(/(Eur|Pct)$/, '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();

function valueText(key, v) {
  if (isNum(v)) {
    if (/Eur$/.test(key)) return fmtEur(v, { dec: 0 });
    if (/Pct$/.test(key)) return `${fmtNum(v, 2)}%`;
    return fmtNum(v, Number.isInteger(v) ? 0 : 2);
  }
  return v == null || isObj(v) || Array.isArray(v) ? '\u2013' : text(v);
}

function eur(v) {
  return isNum(v) ? fmtEur(v, { dec: 0 }) : '\u2013';
}

function kvList(pairs, id) {
  const rows = pairs.filter(([, v]) => v != null && v !== '');
  if (!rows.length) return null;
  return el('dl', { class: 'tx-kv', id: id || null }, rows.map(([k, v]) => el('div', { class: 'tx-kv-row' }, el('dt', { class: 'lbl', text: k }), el('dd', { class: 'num', text: v }))));
}

function warningList(warnings) {
  const rows = (Array.isArray(warnings) ? warnings : [])
    .map(w => (isObj(w) ? { code: text(w.code), message: text(w.message || w.code) } : { code: '', message: text(w) }))
    .filter(w => w.message);
  return rows;
}

function statusChips(params) {
  const years = isObj(params) ? Object.keys(params).sort() : [];
  const chips = years.map(y => {
    const p = isObj(params[y]) ? params[y] : {};
    const st = STATUS[text(p.status).toLowerCase()] || { text: text(p.status) || 'unknown', cls: '', tip: '' };
    return el('li', { dataset: { year: y, status: text(p.status) } }, chip(`${y} ${st.text}`, st.cls, st.tip));
  });
  return chips.length ? el('ul', { class: 'rk-chips tx-status', id: 'taxnl-status', 'aria-label': 'Status of the parameters per year' }, chips) : null;
}

function yearTable(years) {
  const body = el('tbody');
  for (const y of years) {
    const fo = isObj(y.forfait) ? y.forfait : null;
    const tb = isObj(y.tegenbewijs) ? y.tegenbewijs : null;
    const ap = APPLIES[text(y.applies)] || null;
    const flags = [
      y.estimate ? chip('estimate', 'chip-warn', 'Based on provisional, proposed or year-to-date figures') : null,
      y.scope === 'investments_only' || y.otherAssets == null ? chip('investments only', 'chip-warn', 'Other assets are not entered: only the holdings tracked here are counted') : null,
      y.complete === false ? chip('incomplete', 'chip-warn', 'Some positions were left out (see warnings)') : null,
    ].filter(Boolean);
    body.append(el('tr', { dataset: { year: text(y.year), applies: text(y.applies) } },
      el('th', { scope: 'row' }, el('span', { class: 'num', text: text(y.year) }), flags.length ? el('span', { class: 'tx-flags' }, flags) : null),
      el('td', { class: 'num' }, eur(y.portfolioJan1Eur), y.peildatum ? el('span', { class: 'muted tx-sub', text: `on ${text(y.peildatum)}` }) : null),
      el('td', { class: 'num', text: fo ? eur(fo.taxEur) : '\u2013' }),
      el('td', { class: 'num', title: tb ? null : 'Not available' }, tb ? eur(tb.taxEur) : 'n/a',
        tb && tb.partial ? el('span', { class: 'muted tx-sub', text: 'partial: investments only' }) : null),
      el('td', null, ap ? chip(ap.text, '', ap.tip) : '\u2013'),
      el('td', { class: 'num' }, eur(y.dividendTaxCreditEur), y.dividendTaxCreditEstimate ? el('span', { class: 'muted tx-sub', text: 'estimate' }) : null),
      el('td', { class: 'num tx-net', text: eur(y.netEur) })));
  }
  return tableWrap(el('table', { class: 'rk-table tx-table', id: 'taxnl-table' },
    el('caption', { class: 'visually-hidden', text: 'Box 3 estimate per year: value on 1 January, forfait and tegenbewijs tax, which applies, dividend tax credit and net estimate' }),
    el('thead', null, el('tr', null, ['Year', 'Value 1 Jan', 'Forfait', 'Tegenbewijs', 'Applies', 'Dividend tax credit', 'Net estimate'].map(h => el('th', { scope: 'col', text: h })))),
    body));
}

function sourceLinks(p) {
  const urls = (isObj(p) && Array.isArray(p.sources) ? p.sources : []).map(u => safeUrl(u)).filter(u => /^https?:/i.test(u));
  if (!urls.length) return null;
  return el('p', { class: 'muted tx-sources' }, 'Sources: ', urls.map((u, i) => [i ? ' \u00b7 ' : '', el('a', { href: u, rel: 'noopener noreferrer', target: '_blank', text: new URL(u).hostname })]));
}

function yearDetails(y, params) {
  const p = isObj(params) && isObj(params[text(y.year)]) ? params[text(y.year)] : null;
  const fo = isObj(y.forfait) ? y.forfait : null;
  const tb = isObj(y.tegenbewijs) ? y.tegenbewijs : null;
  const oa = isObj(y.otherAssets) ? y.otherAssets : null;
  const notes = (Array.isArray(y.notes) ? y.notes : []).map(text).filter(Boolean);
  const unverified = p && Array.isArray(p.unverified) ? p.unverified.map(k => PARAM_LABEL[k] || k) : [];
  const parts = [
    p ? kvList([
      ['bank return', pctText(p.pBank)], ['other assets return', pctText(p.pOther)], ['debt rate', pctText(p.pDebt)],
      ['tax-free amount', isNum(p.taxFree) ? eur(p.taxFree) : null], ['debt threshold', isNum(p.debtThreshold) ? eur(p.debtThreshold) : null],
      ['rate', pctText(p.rate)],
    ]) : null,
    unverified.length ? el('p', { class: 'tx-unverified', text: `Unverified parameters: ${unverified.join(', ')}.` }) : null,
    fo ? [el('h4', { class: 'lbl tx-h', text: 'Forfait' }), kvList([
      ['base', eur(fo.base)], ['tax-free', eur(fo.taxFree)], ['effective return', isNum(fo.effectiveReturnPct) ? `${fmtNum(fo.effectiveReturnPct, 2)}%` : null], ['tax', eur(fo.taxEur)],
    ])] : null,
    tb ? [el('h4', { class: 'lbl tx-h', text: 'Tegenbewijs (actual return)' }), kvList([
      ['value 1 Jan', eur(tb.vStart)], ['value 31 Dec', eur(tb.vEnd)], ['deposits', eur(tb.deposits)], ['withdrawals', eur(tb.withdrawals)],
      ['dividends', eur(tb.dividends)], ['actual return', eur(tb.actualReturnEur)], ['tax', eur(tb.taxEur)],
    ])] : null,
    oa ? [el('h4', { class: 'lbl tx-h', text: 'Other assets entered' }), kvList(Object.entries(oa).map(([k, v]) => [words(k), valueText(k, v)]))] : null,
    notes.length ? el('ul', { class: 'rk-notes', 'aria-label': `Notes ${text(y.year)}` }, notes.map(n => el('li', { text: n }))) : null,
    sourceLinks(p),
  ].flat().filter(Boolean);
  if (!parts.length) return null;
  return el('details', { class: 'tx-details', dataset: { year: text(y.year) } },
    el('summary', { text: `${text(y.year)}: parameters, inputs and notes` }), parts);
}

function scenarioRow(s) {
  const sc = SCENARIO[text(s.label).toLowerCase()] || { text: text(s.label) || 'scenario', cls: '', tip: '' };
  const pr = isObj(s.params) ? s.params : {};
  const parts = [
    s.status ? `status ${text(s.status)}` : '',
    isNum(s.baseEur) ? `base ${eur(s.baseEur)}` : '',
    isNum(s.taxFreeEur) ? `tax-free ${eur(s.taxFreeEur)}` : '',
    isNum(pr.pOther) ? `other assets return ${pctText(pr.pOther)}` : '',
    isNum(s.effectiveReturnPct) ? `effective return ${fmtNum(s.effectiveReturnPct, 2)}%` : '',
  ].filter(Boolean);
  const unv = Array.isArray(pr.unverified) ? pr.unverified.map(k => PARAM_LABEL[k] || k) : [];
  return el('tr', { dataset: { scenario: text(s.label) } },
    el('th', { scope: 'row' }, chip(sc.text, sc.cls, sc.tip)),
    el('td', { class: 'num tx-net', text: eur(s.taxEur) }),
    el('td', { class: 'tx-extra' }, parts.join(' \u00b7 ') || '\u2013', unv.length ? el('span', { class: 'tx-unverified', text: ` Unverified: ${unv.join(', ')}.` }) : null));
}

function projectionBlock(p) {
  const scenarios = isObj(p) && Array.isArray(p.scenarios) ? p.scenarios.filter(isObj) : [];
  if (!scenarios.length) return null;
  return el('div', { class: 'tx-block', id: 'taxnl-projection' },
    el('h3', { class: 'lbl rk-title', text: `Projection ${text(p.year)}: law vs proposal (estimate)` }),
    el('p', { class: 'muted tx-sub-line', text: `${isNum(p.valueEur) ? `Based on today\u2019s value of the tracked investments (${eur(p.valueEur)})` : 'Based on today\u2019s value of the tracked investments'}. The proposal is not law and may change.` }),
    tableWrap(el('table', { class: 'rk-table tx-table' },
      el('caption', { class: 'visually-hidden', text: `Projection for ${text(p.year)} under the law and under the proposal` }),
      el('thead', null, el('tr', null, ['Scenario', 'Tax (forfait)', 'Parameters'].map(h => el('th', { scope: 'col', text: h })))),
      el('tbody', null, scenarios.map(scenarioRow)))));
}

/**
 * -> array of nodes. `p` is the NL payload of GET /api/tax. opts: {onOpenSettings()}
 */
export function buildTaxNl(p, { onOpenSettings } = {}) {
  const nodes = [];
  const years = (Array.isArray(p.years) ? p.years : []).filter(isObj);
  const warnings = warningList(p.warnings);
  const investmentsOnly = warnings.some(w => w.code === 'other_assets_missing');
  if (investmentsOnly) {
    nodes.push(el('div', { class: 'tx-notice', id: 'taxnl-investments-only', role: 'note' },
      el('p', { class: 'tx-notice-text' }, el('strong', { text: 'Investments only. ' }),
        'Other assets (bank balances, other assets, debts) are not entered, so this estimate covers only the holdings tracked here.'),
      onOpenSettings ? el('button', { type: 'button', class: 'btn btn-sm', id: 'taxnl-open-settings', onclick: onOpenSettings, text: 'Enter other assets' }) : null));
  }
  const others = warnings.filter(w => w.code !== 'other_assets_missing');
  if (others.length) nodes.push(el('ul', { class: 'rk-notes tx-warnings', id: 'taxnl-warnings', 'aria-label': 'Tax warnings' }, others.map(w => el('li', { text: w.message }))));
  const chips = statusChips(p.params);
  if (chips) nodes.push(chips);
  if (years.length) {
    nodes.push(yearTable(years));
    years.map(y => yearDetails(y, p.params)).filter(Boolean).forEach(d => nodes.push(d));
  } else {
    nodes.push(el('p', { class: 'muted', id: 'taxnl-empty', text: 'No box 3 years to show yet.' }));
  }
  const proj = projectionBlock(p.projection);
  if (proj) nodes.push(proj);
  const items = (Array.isArray(p.checklist) ? p.checklist : []).map(i => (isObj(i)
    ? { label: text(i.label || i.text || i.title), value: i.valueEur, source: text(i.source), group: text(i.group), year: i.year }
    : { label: text(i), value: null, source: '', group: '', year: null })).filter(i => i.label);
  if (items.length) {
    const yr = items.find(i => i.year != null);
    nodes.push(el('div', { class: 'tx-block', id: 'taxnl-checklist' },
      el('h3', { class: 'lbl rk-title', text: yr ? `Aangifte checklist ${text(yr.year)}` : 'Aangifte checklist' }),
      el('ul', { class: 'tx-checklist' }, items.map(i => el('li', { dataset: { group: i.group } },
        `${i.label}: `, el('span', { class: 'num', text: isNum(i.value) ? eur(i.value) : 'to provide' }),
        i.source ? el('span', { class: 'muted', text: ` (${i.source})` }) : null,
        i.group === 'tegenbewijs' ? el('span', { class: 'muted', text: ' [tegenbewijs only]' }) : null)))));
  }
  nodes.push(el('p', { class: 'muted tx-disclaimer', id: 'taxnl-disclaimer', text: text(p.disclaimer) || 'Informational estimate from your own figures, not tax advice.' }));
  return nodes;
}
