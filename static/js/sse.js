// Live stream (EventSource on same-origin /stream; the browser replays Basic auth).
//   store 'sse'        {state:'connecting'|'live'|'down', lastTickTs, lastEventTs}
//                      lastEventTs = any SSE message (price/alert/ping/onopen), lastTickTs = last 'price'.
//                      Only this module writes them, so REST polling can never mask a dead stream.
//   store 'livePrices' {[ticker]: item}  latest streamed price item per ticker
// Server pings roughly every 2 s, so silence on a "live" stream means it is dead even when
// the browser still reports OPEN (proxy buffering / half-open TCP).
import { safeJson } from './util.js';
import * as store from './store.js';
import { refreshAlerts, refreshPortfolio, refreshPrices } from './data.js';
import { handleLiveAlert } from './views/alertcenter.js';

export const SSE_URL = '/stream';
export const BACKOFF_MIN_MS = 1000;
export const BACKOFF_MAX_MS = 30000;
export const SILENCE_MS = 30000;          // live but no event (not even a ping) for this long -> reconnect
export const CONNECT_TIMEOUT_MS = 20000;  // stuck connecting for this long -> fresh EventSource
const WATCHDOG_MS = 5000;
const HIDDEN_CLOSE_MS = 120000;           // hidden this long -> close the stream until visible again

// ------------------------------------------------------------------ pure helpers

/** Parse an SSE `data` payload. Returns a plain object or null (never throws). */
export function parseSseData(text) {
  const v = safeJson(text, null);
  return v && typeof v === 'object' && !Array.isArray(v) ? v : null;
}

/** Next reconnect delay: doubles, capped. */
export function nextBackoff(ms) {
  return Math.min(BACKOFF_MAX_MS, Math.max(BACKOFF_MIN_MS, ms) * 2);
}

/**
 * Watchdog decision from SSE-only timestamps.
 *  state: 'connecting'|'live'|'down'; now: ms; lastEventTs: ms|null (SSE messages only);
 *  stateSince: ms when `state` was entered.
 * -> 'reconnect' | 'ok'. 'down' is owned by the backoff timer, never by the watchdog.
 */
export function watchdogVerdict({ state, now, lastEventTs, stateSince, silenceMs = SILENCE_MS, connectTimeoutMs = CONNECT_TIMEOUT_MS }) {
  if (state === 'live') {
    const ref = Math.max(Number.isFinite(lastEventTs) ? lastEventTs : 0, Number.isFinite(stateSince) ? stateSince : 0);
    return now - ref > silenceMs ? 'reconnect' : 'ok';
  }
  if (state === 'connecting') {
    return Number.isFinite(stateSince) && now - stateSince > connectTimeoutMs ? 'reconnect' : 'ok';
  }
  return 'ok';
}

/** Merge one price item into a livePrices map (new object, input untouched). null when unusable. */
export function mergeLivePrice(map, item) {
  if (!item || typeof item.ticker !== 'string' || !item.ticker) return null;
  return { ...(map || {}), [item.ticker]: item };
}

// ------------------------------------------------------------------ connection

let running = false;
let es = null;
let state = 'down';
let stateSince = 0;
let backoff = BACKOFF_MIN_MS;
let reconnectTimer = null;
let watchdogTimer = null;
let hiddenTimer = null;
let suspended = false; // closed on purpose because the tab was hidden (or reconnect deferred while hidden)

function setState(next, extra) {
  if (state !== next) { state = next; stateSince = Date.now(); }
  const cur = store.get('sse');
  if (!cur || cur.state !== next || extra) store.patch('sse', { state: next, ...extra });
}

function closeHandle() {
  if (!es) return;
  const h = es;
  es = null;
  h.onopen = h.onerror = null;
  try { h.close(); } catch (_) { /* already closed */ }
}

function clearReconnect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
}

function scheduleReconnect() {
  if (!running || reconnectTimer) return;
  if (document.hidden) { suspended = true; return; } // resumed on visibilitychange
  const delay = backoff;
  backoff = nextBackoff(backoff);
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
}

function connect() {
  if (!running) return;
  clearReconnect();
  closeHandle(); // never leak a previous handle: it would keep streaming into the same listeners
  suspended = false;
  if (typeof EventSource === 'undefined') { setState('down'); return; }
  setState('connecting');
  let handle;
  try {
    handle = new EventSource(SSE_URL);
  } catch (_) {
    setState('down');
    scheduleReconnect();
    return;
  }
  es = handle;
  const alive = () => es === handle;

  handle.onopen = () => {
    if (!alive()) return;
    backoff = BACKOFF_MIN_MS;
    setState('live', { lastEventTs: Date.now() });
  };
  handle.onerror = () => {
    if (!alive()) return;
    if (handle.readyState === 2 /* CLOSED */) {
      // the browser gave up: only a fresh EventSource can recover
      closeHandle();
      setState('down');
      scheduleReconnect();
    } else {
      setState('connecting'); // browser is retrying by itself; the watchdog bounds how long
    }
  };
  handle.addEventListener('ping', () => { if (alive()) store.patch('sse', { lastEventTs: Date.now() }); });
  handle.addEventListener('price', ev => {
    if (!alive()) return;
    const now = Date.now();
    const merged = mergeLivePrice(store.get('livePrices'), parseSseData(ev.data));
    if (merged) store.set('livePrices', merged);
    store.patch('sse', { lastEventTs: now, ...(merged ? { lastTickTs: now } : {}) });
  });
  handle.addEventListener('alert', ev => {
    if (!alive()) return;
    store.patch('sse', { lastEventTs: Date.now() });
    const a = parseSseData(ev.data);
    if (!a) return;
    try { handleLiveAlert(a); } catch (_) { /* a toast must never break the stream */ }
    refreshAlerts();
    refreshPortfolio();
  });
}

// ------------------------------------------------------------------ watchdog + visibility

function watchdogTick() {
  if (!running || document.hidden || suspended) return;
  const sse = store.get('sse') || {};
  const verdict = watchdogVerdict({ state, now: Date.now(), lastEventTs: sse.lastEventTs, stateSince });
  if (verdict === 'reconnect') {
    closeHandle();
    setState('down');
    backoff = BACKOFF_MIN_MS;
    connect();
  }
}

function refreshAfterGap() {
  refreshPrices();
  refreshPortfolio();
  refreshAlerts();
}

function onVisibility() {
  if (!running) return;
  if (document.hidden) {
    if (!hiddenTimer) {
      hiddenTimer = setTimeout(() => {
        hiddenTimer = null;
        clearReconnect();
        closeHandle();
        suspended = true;
        setState('down');
      }, HIDDEN_CLOSE_MS);
    }
    return;
  }
  if (hiddenTimer) { clearTimeout(hiddenTimer); hiddenTimer = null; }
  if (suspended || !es) {
    backoff = BACKOFF_MIN_MS;
    connect();
    refreshAfterGap();
  } else {
    watchdogTick();
  }
}

/** Open the stream (idempotent). */
export function startSSE() {
  if (running) return;
  running = true;
  backoff = BACKOFF_MIN_MS;
  const prior = store.get('sse') || {};
  store.patch('sse', { lastTickTs: prior.lastTickTs ?? null, lastEventTs: prior.lastEventTs ?? null });
  document.addEventListener('visibilitychange', onVisibility);
  watchdogTimer = setInterval(watchdogTick, WATCHDOG_MS);
  if (document.hidden) { suspended = true; setState('down'); } else connect();
}

/** Close the stream and every timer/listener (idempotent). */
export function stopSSE() {
  if (!running) return;
  running = false;
  document.removeEventListener('visibilitychange', onVisibility);
  clearInterval(watchdogTimer); watchdogTimer = null;
  clearTimeout(hiddenTimer); hiddenTimer = null;
  clearReconnect();
  closeHandle();
  suspended = false;
  setState('down');
}
