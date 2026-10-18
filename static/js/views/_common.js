// Small shared helpers for the overview panels (_since, _attn, _perf, _risk, _policy). Pure DOM
// builders and formatters; no store access, no network. Dynamic sizes go through CSSOM (CSP).
import { el, fmtPct, isNum, signCls } from '../util.js';

export const SEV_TEXT = { urgent: 'Urgent', warn: 'Warning', info: 'Info' };
export const SEV_CHIP = { urgent: 'chip-err', warn: 'chip-warn', info: 'chip-info' };
export const SEV_ORDER = { urgent: 0, warn: 1, info: 2 };

/** Any backend severity spelling -> 'urgent' | 'warn' | 'info'. */
export function normSeverity(s) {
  const v = String(s == null ? '' : s).toLowerCase();
  if (v === 'urgent' || v === 'critical' || v === 'error') return 'urgent';
  if (v === 'warn' || v === 'warning') return 'warn';
  return 'info';
}

export function chip(text, cls = '', title = '') {
  return el('span', { class: 'chip' + (cls ? ' ' + cls : ''), text, title: title || null });
}

/** Local calendar date as YYYY-MM-DD. */
export function localIsoDate(d = new Date()) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 'YYYY-MM-DD...' -> 'dd.mm.' (string slicing: no timezone shift). '' when malformed. */
export function shortDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  return m ? `${m[3]}.${m[2]}.` : '';
}

/** Whole days from the local calendar date `iso` to today (negative = in the past). */
export function daysFromToday(iso, now = new Date()) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  if (!m) return null;
  const a = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const b = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((a - b) / 86400000);
}

const EVENT_KIND = {
  earnings: 'earnings', ex_dividend: 'ex-dividend', dividend_pay: 'dividend paid', fomc: 'FOMC',
  ecb: 'ECB', cpi_us: 'US CPI', market_closed: 'market closed',
};

/** 'earnings ASML' / 'FOMC' for an /api/events row. */
export function eventText(e, labelOf) {
  const kind = EVENT_KIND[e.kind] || String(e.kind || 'event').replace(/_/g, ' ');
  const who = e.ticker ? (labelOf ? labelOf(e.ticker) : e.ticker) : '';
  return who ? `${kind} ${who}` : (e.kind === 'fomc' || e.kind === 'ecb' || e.kind === 'cpi_us' ? kind : (e.title || kind));
}

export function arrowOf(n) {
  return !isNum(n) || n === 0 ? '' : n > 0 ? '\u25b2' : '\u25bc';
}

/** '▼ −4.78%': the arrow keeps the direction readable without colour. */
export function deltaNode(pct, { dec = 2, cls = '' } = {}) {
  if (!isNum(pct)) return el('span', { class: 'num flat ' + cls, text: '\u2013' });
  return el('span', { class: `num ${signCls(pct)} ${cls}`.trim(), text: `${arrowOf(pct)} ${fmtPct(pct, { dec })}`.trim() });
}

/** Percentage bar; width via CSSOM (strict CSP forbids style attributes). */
export function bar(pct, cls = '') {
  const fill = el('span', { class: 'bar-fill' });
  fill.style.width = `${Math.max(0, Math.min(100, isNum(pct) ? pct : 0))}%`;
  return el('span', { class: 'bar' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true' }, fill);
}

/** Wrap a table so a wide one scrolls inside its card instead of widening the page. */
export function tableWrap(table) {
  return el('div', { class: 'tbl-wrap' }, table);
}

/** Drop markdown punctuation from model-written text shown as plain text. */
export function plain(text) {
  return String(text == null ? '' : text).replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim();
}

/** Split text at about `max` chars on a word boundary: {head, rest}. rest is '' when it fits. */
export function splitAt(text, max = 140) {
  const t = String(text || '');
  if (t.length <= max) return { head: t, rest: '' };
  const cut = t.slice(0, max).replace(/\s+\S*$/, '') || t.slice(0, max);
  return { head: cut + '\u2026', rest: t.slice(cut.length).trim() };
}
