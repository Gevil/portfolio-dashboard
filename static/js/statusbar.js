// Status bar behaviour: data-age chip, EU/US session pills, alerts bell + unread badge,
// neutral lane dot, chat/settings buttons, view tabs.
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

/** Newest real price tick in unix seconds: max(position priceAsOf, last SSE price event). */
export function lastTickSec() {
  let best = 0;
  const p = slot('portfolio').data;
  if (p && Array.isArray(p.positions)) {
    for (const pos of p.positions) {
      const d = toDate(pos.priceAsOf);
      if (d) best = Math.max(best, d.getTime() / 1000);
    }
  }
  const sse = get('sse');
  if (sse && isNum(sse.lastTickTs)) best = Math.max(best, sse.lastTickTs / 1000);
  return best || null;
}

function setChip(node, cls, text, title) {
  node.className = node.className.replace(/\bchip-(ok|warn|err|info)\b/g, '').trim() + (cls ? ' ' + cls : '');
  const dot = node.querySelector('.dot');
  if (dot) dot.className = 'dot' + (cls ? ' dot-' + cls.replace('chip-', '') : '');
  node.querySelector('.sb-data-text, .sb-session-text').textContent = text;
  node.title = title || '';
}

function paintDataChip() {
  const node = document.getElementById('sb-data');
  const pf = slot('portfolio');
  const tick = lastTickSec();
  if (pf.error && !pf.data) return setChip(node, 'chip-err', 'Data unavailable', pf.error);
  if (!tick) return setChip(node, '', pf.loading ? 'Loading\u2026' : 'No ticks yet', '');
  const age = ageSeconds(tick);
  const euOpen = isSessionOpen('eu');
  const when = age > 86400 ? fmtDateTime24(tick) : fmtTime24(tick);
  const rel = age < 90 ? `${Math.round(age)} s` : age < 5400 ? `${Math.round(age / 60)} min` : `${Math.round(age / 3600)} h`;
  if (pf.error) return setChip(node, 'chip-warn', `Refresh failing \u00b7 ${rel}`, `Last tick ${when}. ${pf.error}`);
  if (!euOpen) return setChip(node, '', `Closed \u00b7 last ${when}`, 'EU session closed; showing the last tick');
  if (age < 180) return setChip(node, 'chip-ok', `Live \u00b7 ${rel}`, `Last tick ${when}`);
  if (age < 900) return setChip(node, 'chip-warn', `Delayed \u00b7 ${rel}`, `Last tick ${when}`);
  return setChip(node, 'chip-err', `Stale \u00b7 ${rel}`, `Last tick ${when}; the EU session is open`);
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
  unsub.push(subscribe('portfolio', paintDataChip), subscribe('sse', paintDataChip));
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
  repaint(); paintBell(); paintLane(); paintTabs(get('view'));
  const timer = setInterval(() => { if (!document.hidden) repaint(); }, 15000);
  const onVis = () => { if (!document.hidden) repaint(); };
  document.addEventListener('visibilitychange', onVis);
  return () => {
    unsub.forEach(u => u());
    clearInterval(timer);
    document.removeEventListener('visibilitychange', onVis);
  };
}
