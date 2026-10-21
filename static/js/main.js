// Entry point: theme, view routing, shared data polling, shell wiring.
import * as util from './util.js';
import { store, get, set, subscribe } from './store.js';
import { api } from './api.js';
import { ui, usePolling, renderState, toast } from './ui.js';
import { markdown } from './markdown.js';
import * as data from './data.js';
import { initBook, setBook, readHashBook } from './book.js';
import { initStatusBar } from './statusbar.js';
import { mountOverview } from './views/overview.js';
import { trackVisit } from './views/_since.js';

const VIEWS = ['overview', 'digest', 'market', 'aiops'];
const TITLES = { overview: 'Overview', digest: 'Digest', market: 'Market', aiops: 'AI Ops' };
const THEME_KEY = 'pd.theme';

// ------------------------------------------------------------------ theme

const lightQuery = window.matchMedia('(prefers-color-scheme: light)');

function readThemePref() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch (_) { return 'system'; }
}

function applyTheme(pref) {
  const root = document.documentElement;
  if (pref === 'light' || pref === 'dark') root.setAttribute('data-theme', pref);
  else root.removeAttribute('data-theme');
  set('theme', pref === 'system' ? (lightQuery.matches ? 'light' : 'dark') : pref);
}

subscribe('themePref', pref => {
  try {
    if (pref === 'system') localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, pref);
  } catch (_) { /* storage blocked: the choice still applies for this session */ }
  applyTheme(pref);
});
lightQuery.addEventListener('change', () => { if (get('themePref') === 'system') applyTheme('system'); });
set('themePref', readThemePref());

// ------------------------------------------------------------------ views

const ctx = { store, api, util, ui, markdown };
const sections = Object.fromEntries(VIEWS.map(v => [v, document.getElementById('view-' + v)]));
const SECONDARY = { digest: './views/digest.js', market: './views/market.js', aiops: './views/aiops.js' };
let secondary = null; // {view, unmount, token}
let mountToken = 0;

async function unmountSecondary() {
  const cur = secondary;
  secondary = null;
  mountToken++;
  if (cur && cur.unmount) {
    try { await cur.unmount(); } catch (e) { console.error('unmount failed', cur.view, e); }
  }
}

async function mountSecondary(view) {
  const root = sections[view];
  const token = ++mountToken;
  const load = async () => {
    renderState(root, { loading: `Loading ${TITLES[view]}` });
    try {
      const mod = await import(SECONDARY[view]);
      if (token !== mountToken) return;
      root.replaceChildren();
      const unmount = await mod.mount(root, ctx);
      if (token !== mountToken) { if (typeof unmount === 'function') unmount(); return; }
      secondary = { view, unmount: typeof unmount === 'function' ? unmount : null };
    } catch (e) {
      console.error('view failed', view, e);
      if (token === mountToken) renderState(root, { error: `${TITLES[view]} could not start: ${e.message || e}`, retry: load });
    }
  };
  await load();
}

function titleFor(view) {
  const prefix = get('book') === 'practice' ? '[Practice] ' : '';
  return `${prefix}${TITLES[view]} \u00b7 Portfolio Watch`;
}

async function applyView(view) {
  const prev = get('view');
  if (prev === view && (view === 'overview' || secondary)) return;
  await unmountSecondary();
  for (const v of VIEWS) sections[v].hidden = v !== view;
  set('view', view);
  document.title = titleFor(view);
  if (view !== 'overview') await mountSecondary(view);
}

// Switching books never changes the trading mode (plan §4.3); it only resets the resource slots
// fed by /api/portfolio* so the previous book's data is never shown under the new label, then
// refetches. Each loader's own latestRequest() aborts any in-flight request for the old book.
function switchBook(next) {
  if (next === get('book')) return;
  setBook(next);
  set('portfolio', undefined);
  set('risk', undefined);
  data.refreshPortfolio();
  data.refreshRisk();
  data.refreshPaperStatus();
}
subscribe('book', () => { const v = get('view'); if (v) document.title = titleFor(v); });
// Practice bucket switch (Daily | Intraday): drop the old bucket's figures and reload; every loader is
// "latest wins", so the old bucket's in-flight requests are aborted/discarded.
subscribe('bucket', (b, prev) => {
  if (prev === undefined || b === prev) return;   // boot (initBook) is not a switch
  if (get('book') !== 'practice') return;
  set('portfolio', undefined);
  set('paperStatus', undefined);
  data.refreshPortfolio();
  data.refreshPaperStatus();
});

// ------------------------------------------------------------------ routes (C10)
//   #overview | #digest | #market | #aiops      views
//   #detail/<ID>                               overview with that holding selected
//   #alerts | #alerts/<id>                     alert center drawer (optionally focused on a row)
//   anything else                              overview
// Selecting a holding writes #detail/<ID> (push when opening, replace when switching); closing restores
// #overview (replace); browser Back from a pushed detail entry closes it via hashchange.

function parseRoute(hash = location.hash) {
  const raw = hash.replace(/^#\/?/, '');
  if (VIEWS.includes(raw)) return { name: raw, view: raw };
  const slash = raw.indexOf('/');
  const head = slash < 0 ? raw : raw.slice(0, slash);
  let arg = slash < 0 ? '' : raw.slice(slash + 1);
  try { arg = decodeURIComponent(arg); } catch (_) { /* malformed escape: use the raw text */ }
  if (head === 'detail' && arg) return { name: 'detail', view: 'overview', id: arg };
  if (head === 'alerts') return { name: 'alerts', view: null, focusId: arg || null };
  return { name: 'unknown', view: 'overview' };
}

let routing = false; // true while a route is being applied: the selectedId subscriber must not write the hash back
function setSelected(id) {
  routing = true;
  try { set('selectedId', id); } finally { routing = false; }
}

async function applyRoute(route) {
  if (route.name === 'alerts') {
    if (!get('view')) await applyView('overview');
    openAlerts(route.focusId);
    return;
  }
  await applyView(route.view);
  setSelected(route.name === 'detail' ? route.id : null);
  if (route.name === 'unknown' && location.hash && location.hash !== '#overview') history.replaceState(null, '', '#overview');
}

function goHash(hash) {
  if (location.hash.replace(/^#\/?/, '') !== hash) location.hash = hash; // hashchange applies it
  else applyRoute(parseRoute());
}
const goView = goHash;

window.addEventListener('hashchange', () => {
  const hb = readHashBook(); // a same-page #book= link switches the context too
  if (hb && hb !== 'live') switchBook(hb);
  applyRoute(parseRoute());
});

subscribe('selectedId', id => {
  if (routing) return;
  const cur = parseRoute();
  if (id) {
    const target = '#detail/' + encodeURIComponent(id);
    if (location.hash !== target) {
      if (cur.name === 'detail') history.replaceState(null, '', target);
      else history.pushState(null, '', target);
    }
    if (get('view') !== 'overview') applyView('overview');
  } else if (cur.name === 'detail') {
    history.replaceState(null, '', '#overview');
  }
});

// Closing the alert drawer restores #overview (and the view that goes with it).
subscribe('alertCenterOpen', open => {
  if (open || parseRoute().name !== 'alerts') return;
  const sel = get('selectedId');
  history.replaceState(null, '', sel ? '#detail/' + sel : '#overview');
  applyRoute(parseRoute());
});

// ------------------------------------------------------------------ lazy shell modules

let chat = null;
let chatLoading = null;
async function ensureChat() {
  if (chat) return chat;
  if (!chatLoading) {
    chatLoading = import('./chat.js').then(m => {
      chat = m.mountChat(document.getElementById('chat-host'));
      return chat;
    }).catch(e => {
      chatLoading = null;
      console.error('chat failed to load', e);
      toast('The assistant could not be loaded.', { type: 'error' });
      return null;
    });
  }
  return chatLoading;
}

async function lazyCall(path, fn, what, ...args) {
  try {
    const mod = await import(path);
    return mod[fn](...args);
  } catch (e) {
    console.error(what + ' failed', e);
    toast(`${what} could not be opened: ${e.message || e}`, { type: 'error' });
    return null;
  }
}

function openAlerts(focusId) {
  return lazyCall('./views/alertcenter.js', 'openAlertCenter', 'Alert center', { focusId: focusId || null });
}

initBook(); // store 'book' from localStorage/#book= deep link (data-book attr already set pre-paint)
initStatusBar({
  onView: goView,
  onSettings: () => lazyCall('./views/settings.js', 'openSettings', 'Settings'),
  onAlerts: () => goHash('alerts'),
  onChat: async () => { const c = await ensureChat(); if (c) c.toggle(); },
  onLane: () => goView('aiops'),
  onBook: switchBook,
});

// ------------------------------------------------------------------ data

set('lastVisit', trackVisit()); // previous visit; the new one is written after 5 s of visible time
mountOverview(sections.overview);
applyRoute(parseRoute());
data.refreshAll();

usePolling(data.refreshPortfolio, 30000);
usePolling(data.refreshPrices, 60000);
usePolling(data.refreshAlerts, 60000);
usePolling(data.refreshLane, 60000);
usePolling(data.refreshMarketLight, 300000);
usePolling(data.refreshAdvice, 300000);
usePolling(data.refreshPolicy, 300000);
usePolling(data.refreshEvents, 300000);
usePolling(data.refreshAlertRules, 300000);
usePolling(data.refreshRisk, 600000);
usePolling(data.refreshPaperStatus, 60000);

// Live price events re-derive the portfolio server-side; throttle so a burst of ticks costs one request.
let lastTickSeen = 0;
let tickTimer = null;
subscribe('sse', s => {
  if (!s || !s.lastTickTs || s.lastTickTs === lastTickSeen) return;
  lastTickSeen = s.lastTickTs;
  if (tickTimer || document.hidden) return;
  tickTimer = setTimeout(() => { tickTimer = null; data.refreshPortfolio(); }, 20000);
});

import('./sse.js').then(m => m.startSSE()).catch(e => console.error('live stream unavailable', e));
// Warm the assistant module when the browser is idle so the first click is instant.
(window.requestIdleCallback || (fn => setTimeout(fn, 2500)))(() => { import('./chat.js').catch(() => {}); });

window.addEventListener('unhandledrejection', e => console.error('unhandled rejection', e.reason));
