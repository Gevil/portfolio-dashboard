// Status bar behaviour: quote-age chip (oldest held quote), SSE liveness dot, EU/US session pills,
// alerts bell + unread badge, neutral lane dot, chat/settings buttons, view tabs.
import { slot, subscribe, get } from './store.js';
import { fmtTime24, fmtDateTime24, ageSeconds, isNum, toDate } from './util.js';

const SESSIONS = {
  eu: { tz: 'Europe/Amsterdam', open: [9, 0], close: [17, 30] },
  us: { tz: 'America/New_York', open: [9, 30], close: [16, 0] },
};
const WEEKDAYS = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 };
const fmtCache = new Map();

/** Is the exchange session open at `date`? Weekday/hour read in the exchange's own timezone (DST-safe). */
export function isSessionOpen(key, date = new Date()) {
  const s = SESSIONS[key];
  let f = fmtCache.get(s.tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', { timeZone: s.tz, weekday: 'short', hour: 'numeric', minute: 'numeric', hourCycle: 'h23' });
    fmtCache.set(s.tz, f);
  }
  const parts = Object.fromEntries(f.formatToParts(date).map(p => [p.type, p.value]));
  const dow = WEEKDAYS[parts.weekday];
  if (!dow || dow > 5) return false;
  const mins = Number(parts.hour) * 60 + Number(parts.minute);
  return mins >= s.open[0] * 60 + s.open[1] && mins < s.close[0] * 60 + s.close[1];
}

/** Held positions' quote ages, oldest first: [{id, label, ts (unix s), age (s), source}]. */
export function quoteAges() {
  const p = slot('portfolio').data;
  const out = [];
  if (!p || !Array.isArray(p.positions)) return out;
  for (const pos of p.positions) {
    if (isNum(pos.shares) && pos.shares <= 0) continue;
    const d = toDate(pos.priceAsOf);
    if (!d) continue;
    out.push({ id: pos.id, label: pos.label || pos.id, ts: d.getTime() / 1000, age: ageSeconds(d), source: pos.priceSource || 'unknown' });
  }
  return out.sort((a, b) => b.age - a.age);
}

/** EU session open per the clock, unless every held quote's venue reports itself closed (holidays). */
function euOpenNow(ages) {
  if (!isSessionOpen('eu')) return false;
  const prices = slot('prices').data;
  if (!Array.isArray(prices)) return true;
  const held = new Set(ages.map(a => a.id));
  const known = prices.filter(p => p && held.has(p.ticker) && typeof p.marketOpen === 'boolean');
  return !(known.length && known.every(p => p.marketOpen === false));
}

function ageText(sec) {
  return sec < 90 ? `${Math.round(sec)} s` : sec < 5400 ? `${Math.round(sec / 60)} min` : sec < 172800 ? `${Math.round(sec / 3600)} h` : `${Math.round(sec / 86400)} d`;
}

function setChip(node, cls, text, title) {
  node.className = node.className.replace(/\bchip-(ok|warn|err|info)\b/g, '').trim() + (cls ? ' ' + cls : '');
  const dot = node.querySelector('.dot');
  if (dot) dot.className = 'dot' + (cls ? ' dot-' + cls.replace('chip-', '') : '');
  node.querySelector('.sb-data-text, .sb-session-text').textContent = text;
  node.title = title || '';
}

const SSE_TEXT = {
  live: 'Live stream: connected',
  connecting: 'Live stream: connecting',
  down: 'Live stream: down (prices refresh by polling)',
};

/** SSE liveness is its own signal: it never feeds the quote-age chip. */
function sseLine() {
  const s = get('sse');
  return SSE_TEXT[s && s.state] || 'Live stream: unknown';
}

function paintSse() {
  const node = document.getElementById('sb-sse');
  const s = get('sse');
  const state = s && s.state;
  node.className = 'dot sb-sse' + (state === 'live' ? ' dot-ok' : state === 'down' ? ' dot-err' : state === 'connecting' ? ' dot-warn' : '');
  node.title = sseLine();
  node.setAttribute('aria-label', sseLine());
}

function paintDataChip() {
  const node = document.getElementById('sb-data');
  const pf = slot('portfolio');
  const ages = quoteAges();
  if (pf.error && !pf.data) return setChip(node, 'chip-err', 'Data unavailable', pf.error);
  if (!ages.length) return setChip(node, '', pf.loading ? 'Loading\u2026' : 'No quotes yet', '');
  const oldest = ages[0];
  const newest = ages[ages.length - 1];
  const age = oldest.age;
  const lines = ages.map(a => `${a.label}: ${ageText(a.age)} old \u00b7 ${a.source}`);
  lines.push(sseLine());
  const tip = lines.join('\n');
  if (pf.error) return setChip(node, 'chip-warn', `Refresh failing \u00b7 ${ageText(age)}`, `${pf.error}\n${tip}`);
  if (!euOpenNow(ages)) {
    const sameDay = new Date(newest.ts * 1000).toDateString() === new Date().toDateString();
    return setChip(node, '', `Closed \u00b7 last ${sameDay ? fmtTime24(newest.ts) : fmtDateTime24(newest.ts)}`, `EU session closed; showing the last quote\n${tip}`);
  }
  if (age < 600) return setChip(node, 'chip-ok', `Live \u00b7 ${Math.round(age)}s`, `Oldest held quote\n${tip}`);
  if (age <= 1800) return setChip(node, 'chip-warn', 'Delayed ~15 min (Yahoo EU)', `Oldest held quote is ${ageText(age)} old\n${tip}`);
  return setChip(node, 'chip-err', `Stale \u00b7 ${Math.round(age / 60)} min`, `The EU session is open but the oldest held quote is ${ageText(age)} old\n${tip}`);
}

function paintSessions() {
  for (const key of ['eu', 'us']) {
    const open = isSessionOpen(key);
    const node = document.getElementById('sb-session-' + key);
    setChip(node, open ? 'chip-ok' : '', `${key.toUpperCase()} ${open ? 'open' : 'closed'}`, node.title.split(' \u2014 ')[0]);
  }
}

function paintBell() {
  const badge = document.getElementById('alerts-badge');
  const btn = document.getElementById('btn-alerts');
  const a = slot('alerts');
  const unread = a.data && isNum(a.data.unread) ? a.data.unread : 0;
  badge.hidden = unread <= 0;
  badge.textContent = unread > 99 ? '99+' : String(unread);
  btn.setAttribute('aria-label', unread > 0 ? `Alerts, ${unread} unread` : (a.error && !a.data ? 'Alerts (could not load)' : 'Alerts'));
}

/** Neutral by design: the lane is a diagnostic, not a money number. Never a red pill. */
function paintLane() {
  const dot = document.getElementById('lane-dot');
  const btn = document.getElementById('btn-lane');
  const l = slot('lane');
  const d = l.data;
  let cls = '';
  let text = 'AI lane: status unknown';
  if (d && d.serving_model) {
    const fallback = d.role === 'fallback';
    cls = fallback ? ' dot-warn' : ' dot-ok';
    text = `AI lane: ${d.model || d.lane || 'serving'}${d.role ? ` (${d.role})` : ''}`;
  } else if (d) {
    text = 'AI lane: not serving right now';
  } else if (l.error) {
    text = 'AI lane: status unavailable';
  }
  dot.className = 'dot' + cls;
  btn.title = text;
  btn.setAttribute('aria-label', text + ' \u2013 open AI Ops');
}

function paintTabs(view) {
  document.querySelectorAll('#tabs .tab').forEach(b => {
    if (b.dataset.view === view) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
}

/** handlers: {onView(view), onSettings(), onAlerts(), onChat(), onLane()} -> dispose() */
export function initStatusBar(handlers) {
  const unsub = [];
  const repaint = () => { paintDataChip(); paintSessions(); };
  unsub.push(subscribe('portfolio', paintDataChip), subscribe('prices', paintDataChip), subscribe('sse', () => { paintSse(); paintDataChip(); }));
  unsub.push(subscribe('alerts', paintBell), subscribe('lane', paintLane));
  unsub.push(subscribe('view', paintTabs));
  unsub.push(subscribe('chatOpen', open => {
    document.getElementById('btn-chat').setAttribute('aria-expanded', open ? 'true' : 'false');
  }));
  const tabs = document.getElementById('tabs');
  tabs.addEventListener('click', e => {
    const b = e.target.closest('.tab');
    if (b) handlers.onView(b.dataset.view);
  });
  document.getElementById('btn-settings').addEventListener('click', () => handlers.onSettings());
  document.getElementById('btn-alerts').addEventListener('click', () => handlers.onAlerts());
  document.getElementById('btn-chat').addEventListener('click', () => handlers.onChat());
  document.getElementById('btn-lane').addEventListener('click', () => handlers.onLane());
  repaint(); paintSse(); paintBell(); paintLane(); paintTabs(get('view'));
  const timer = setInterval(() => { if (!document.hidden) repaint(); }, 15000);
  const onVis = () => { if (!document.hidden) repaint(); };
  document.addEventListener('visibilitychange', onVis);
  return () => {
    unsub.forEach(u => u());
    clearInterval(timer);
    document.removeEventListener('visibilitychange', onVis);
  };
}
