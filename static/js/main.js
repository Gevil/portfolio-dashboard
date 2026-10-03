// Entry point: theme, view routing, shared data polling, shell wiring.
import * as util from './util.js';
import { store, get, set, subscribe } from './store.js';
import { api } from './api.js';
import { ui, usePolling, renderState, toast } from './ui.js';
import { markdown } from './markdown.js';
import * as data from './data.js';
import { initStatusBar } from './statusbar.js';
import { mountOverview } from './views/overview.js';

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

async function applyView(view) {
  const prev = get('view');
  if (prev === view && (view === 'overview' || secondary)) return;
  await unmountSecondary();
  for (const v of VIEWS) sections[v].hidden = v !== view;
  set('view', view);
  document.title = `${TITLES[view]} \u00b7 Portfolio Watch`;
  if (view !== 'overview') await mountSecondary(view);
}

function viewFromHash() {
  const h = location.hash.replace(/^#\/?/, '');
  return VIEWS.includes(h) ? h : 'overview';
}

function goView(view) {
  if (location.hash.replace(/^#\/?/, '') !== view) location.hash = view; // hashchange applies it
  else applyView(view);
}

window.addEventListener('hashchange', () => applyView(viewFromHash()));
subscribe('selectedId', id => { if (id && get('view') !== 'overview') goView('overview'); });

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

async function lazyCall(path, fn, what) {
  try {
    const mod = await import(path);
    return mod[fn]();
  } catch (e) {
    console.error(what + ' failed', e);
    toast(`${what} could not be opened: ${e.message || e}`, { type: 'error' });
    return null;
  }
}

initStatusBar({
  onView: goView,
  onSettings: () => lazyCall('./views/settings.js', 'openSettings', 'Settings'),
  onAlerts: () => lazyCall('./views/alertcenter.js', 'openAlertCenter', 'Alert center'),
  onChat: async () => { const c = await ensureChat(); if (c) c.toggle(); },
  onLane: () => goView('aiops'),
});

// ------------------------------------------------------------------ data

mountOverview(sections.overview);
applyView(viewFromHash());
data.refreshAll();

usePolling(data.refreshPortfolio, 30000);
usePolling(data.refreshPrices, 60000);
usePolling(data.refreshAlerts, 60000);
usePolling(data.refreshLane, 60000);
usePolling(data.refreshMarketLight, 300000);

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
