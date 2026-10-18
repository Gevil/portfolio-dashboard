// "Since your last visit" strip at the top of the overview.
//
//   trackVisit() -> previous visit (ms epoch | null). Reads localStorage `pd.lastVisit` once and writes
//       `now` only after the page has been VISIBLE for 5 s in total (a tab opened in the background
//       or closed within seconds never counts as a visit).
//   computeSince(inputs) -> model (pure; unit-testable).
//   buildSince(model, opts) -> <section id="since-strip">.
//
// Sections: today's moves with |z| >= 2 (z = changePct / sigmaPct, sigma from /api/alert-rules),
// alerts created since the last visit (by severity, linking to #alerts), confirmed advice changes
// since then, events in the next 7 days, and the portfolio's data warnings. On a first visit there is
// no "since": only the date-independent sections are shown.
import { el, fmtDateTime24, isNum } from '../util.js';
import { localIsoDate, daysFromToday, eventText, normSeverity, shortDate, deltaNode, SEV_TEXT } from './_common.js';

const VISIT_KEY = 'pd.lastVisit';
const COLLAPSE_KEY = 'pd.sinceCollapsed';
const DISMISS_KEY = 'pd.sinceDismissed';
const VISIBLE_MS = 5000;
const Z_MIN = 2;
const EVENT_DAYS = 7;
const EVENTS_SHOWN = 5;

function readKey(storage, key) {
  try { return storage.getItem(key); } catch (_) { return null; }
}
function writeKey(storage, key, value) {
  try { storage.setItem(key, value); } catch (_) { /* storage blocked: the strip just forgets */ }
}

export function trackVisit() {
  const raw = Number(readKey(localStorage, VISIT_KEY));
  const previous = Number.isFinite(raw) && raw > 0 ? raw : null;
  let visibleMs = 0;
  let since = document.hidden ? 0 : Date.now();
  let timer = null;
  let done = false;
  const finish = () => {
    done = true;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVis);
    writeKey(localStorage, VISIT_KEY, String(Date.now()));
  };
  const arm = () => {
    clearTimeout(timer);
    timer = setTimeout(finish, Math.max(0, VISIBLE_MS - visibleMs));
  };
  function onVis() {
    if (done) return;
    if (document.hidden) {
      visibleMs += Date.now() - since;
      clearTimeout(timer);
    } else {
      since = Date.now();
      arm();
    }
  }
  document.addEventListener('visibilitychange', onVis);
  if (!document.hidden) arm();
  return previous;
}

export function dismissKey(lastVisit) {
  return String(lastVisit || 'first');
}
export function isDismissed(lastVisit) {
  return readKey(sessionStorage, DISMISS_KEY) === dismissKey(lastVisit);
}
export function markDismissed(lastVisit) {
  writeKey(sessionStorage, DISMISS_KEY, dismissKey(lastVisit));
}
export function isCollapsed() {
  return readKey(localStorage, COLLAPSE_KEY) === '1';
}
export function setCollapsed(on) {
  writeKey(localStorage, COLLAPSE_KEY, on ? '1' : '0');
}

/**
 * inputs: {portfolio, rules, alerts, advice, events, lastVisit, now}  (each the raw API payload or null)
 * -> {first, moves:[{id,dayPct,z}], alerts:{total, counts:{urgent,warn,info}},
 *     advice:[{id,rating,date}], events:[{date,text,days}], eventsMore, warnings:[string]}
 */
export function computeSince({ portfolio, rules, alerts, advice, events, lastVisit, now = Date.now() }) {
  const first = !lastVisit;
  const sigma = (rules && rules.sigma) || {};
  const moves = [];
  for (const p of (portfolio && portfolio.positions) || []) {
    const s = sigma[p.id] && sigma[p.id].sigmaPct;
    if (!isNum(p.dayPct) || !isNum(s) || s <= 0) continue;
    const z = p.dayPct / s;
    if (Math.abs(z) >= Z_MIN) moves.push({ id: p.id, dayPct: p.dayPct, z });
  }
  moves.sort((a, b) => Math.abs(b.z) - Math.abs(a.z));

  const counts = { urgent: 0, warn: 0, info: 0 };
  let total = 0;
  if (!first) {
    for (const a of (alerts && alerts.items) || []) {
      const t = Number(a.time);
      if (Number.isFinite(t) && t * 1000 > lastVisit) { counts[normSeverity(a.severity)]++; total++; }
    }
  }

  const changes = [];
  if (!first && advice && typeof advice === 'object') {
    const lastDate = localIsoDate(new Date(lastVisit));
    for (const [id, a] of Object.entries(advice)) {
      if (!a || (a.state !== 'confirmed' && a.state !== 'first')) continue;
      const date = a.published && a.published.date;
      if (!date) continue;
      const after = date > lastDate || (date === lastDate && Number(a.ts) * 1000 > lastVisit);
      if (after) changes.push({ id, rating: a.published.rating || a.rating, date });
    }
    changes.sort((a, b) => (a.date < b.date ? 1 : -1));
  }

  const ahead = [];
  const today = new Date(now);
  for (const e of (events && events.events) || []) {
    const days = daysFromToday(e.date, today);
    if (days != null && days >= 0 && days <= EVENT_DAYS) ahead.push({ date: e.date, days, e });
  }
  ahead.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));

  const warnings = ((portfolio && portfolio.warnings) || []).map(w => w.message || w.code).filter(Boolean);
  return { first, moves, alerts: { total, counts }, advice: changes, events: ahead, warnings };
}

export function summaryText(m) {
  const bits = [];
  if (m.moves.length) bits.push(`${m.moves.length} big move${m.moves.length > 1 ? 's' : ''}`);
  if (m.alerts.total) bits.push(`${m.alerts.total} alert${m.alerts.total > 1 ? 's' : ''}`);
  if (m.advice.length) bits.push(`${m.advice.length} advice change${m.advice.length > 1 ? 's' : ''}`);
  if (m.events.length) bits.push(`${m.events.length} event${m.events.length > 1 ? 's' : ''} this week`);
  if (m.warnings.length) bits.push(`${m.warnings.length} data warning${m.warnings.length > 1 ? 's' : ''}`);
  return bits.length ? bits.join(' \u00b7 ') : 'Nothing new';
}

function row(label, ...content) {
  return el('li', { class: 'since-item' }, el('span', { class: 'lbl since-lbl', text: label }), el('span', { class: 'since-text' }, ...content));
}

function link(href, text, title) {
  return el('a', { class: 'since-link', href, title: title || null, text });
}

/** opts: {lastVisit, collapsed, labelOf, onToggle(), onDismiss()} -> <section id="since-strip"> */
export function buildSince(m, { lastVisit, collapsed, labelOf, onToggle, onDismiss }) {
  const name = id => (labelOf ? labelOf(id) : id);
  const list = el('ul', { class: 'since-list', id: 'since-body', hidden: collapsed });

  if (m.moves.length) {
    const nodes = [];
    m.moves.forEach((mv, i) => {
      if (i) nodes.push(', ');
      nodes.push(link(`#detail/${encodeURIComponent(mv.id)}`, name(mv.id)), ' ', deltaNode(mv.dayPct), el('span', { class: 'muted num', text: ` (z ${mv.z >= 0 ? '+' : '\u2212'}${Math.abs(mv.z).toFixed(1)})` }));
    });
    list.append(row('Big moves today', ...nodes));
  }
  if (!m.first) {
    if (m.alerts.total) {
      const parts = ['urgent', 'warn', 'info'].filter(k => m.alerts.counts[k]).map(k => `${m.alerts.counts[k]} ${SEV_TEXT[k].toLowerCase()}`);
      list.append(row('New alerts', link('#alerts', `${m.alerts.total} new`, 'Open the alert center'), ` (${parts.join(', ')})`));
    }
    if (m.advice.length) {
      const nodes = [];
      m.advice.forEach((c, i) => {
        if (i) nodes.push(', ');
        nodes.push(link(`#detail/${encodeURIComponent(c.id)}`, name(c.id)), ` \u2192 ${c.rating || '?'}`, el('span', { class: 'muted num', text: ` (${shortDate(c.date)})` }));
      });
      list.append(row('Advice changed', ...nodes));
    }
  }
  if (m.events.length) {
    const shown = m.events.slice(0, EVENTS_SHOWN);
    const text = shown.map(x => `${shortDate(x.date)} ${eventText(x.e, labelOf)}`).join(' \u00b7 ');
    list.append(row('Next 7 days', text, m.events.length > shown.length ? el('span', { class: 'muted', text: ` +${m.events.length - shown.length} more` }) : null));
  }
  if (m.warnings.length) {
    list.append(row('Data warnings', el('ul', { class: 'since-warns' }, m.warnings.map(w => el('li', { text: w })))));
  }
  if (!list.children.length) list.append(el('li', { class: 'since-item since-empty muted', text: 'Nothing new.' }));

  const title = el('h2', { class: 'card-title', id: 'since-title' }, 'Since your last visit',
    m.first ? el('span', { class: 'muted since-when', text: ' \u00b7 first visit on this browser' }) : el('span', { class: 'muted since-when num', text: ` \u00b7 ${fmtDateTime24(lastVisit)}` }));
  const toggle = el('button', {
    type: 'button', class: 'btn btn-ghost btn-sm', id: 'since-toggle', 'aria-expanded': collapsed ? 'false' : 'true', 'aria-controls': 'since-body',
    text: collapsed ? 'Expand' : 'Collapse', onclick: onToggle,
  });
  const dismiss = el('button', { type: 'button', class: 'btn btn-ghost btn-sm', id: 'since-dismiss', 'aria-label': 'Dismiss this summary', text: 'Dismiss', onclick: onDismiss });
  return el('section', { class: 'card since' + (collapsed ? ' is-collapsed' : ''), id: 'since-strip', 'aria-labelledby': 'since-title' },
    el('div', { class: 'since-head' }, title, el('span', { class: 'since-summary muted', text: summaryText(m) }), el('div', { class: 'card-tools' }, toggle, dismiss)),
    list);
}
