// Data layer: loads shared resources into store slots (see store.js) and exposes lookups.
// Every loader is "latest wins" (a slow older response never overwrites a newer one) and
// keeps the last good data when a refresh fails (slot.error is set, slot.data survives).
import { apiGet, latestRequest } from './api.js';
import { slot, slotApply, slotLoading, set as setStore } from './store.js';

function loader(key, url, { timeoutMs = 25000 } = {}) {
  const lr = latestRequest();
  return async function load() {
    const t = lr.begin();
    slotLoading(key);
    const res = await apiGet(typeof url === 'function' ? url() : url, { signal: t.signal, timeoutMs });
    if (!t.current()) return null;
    slotApply(key, res);
    return res;
  };
}

/** GET /api/portfolio -> slot 'portfolio' */
export const refreshPortfolio = loader('portfolio', '/api/portfolio');
/** GET /api/prices -> slot 'prices' (array of price items incl. sparkline; usLive optional) */
export const refreshPrices = loader('prices', '/api/prices');
/** GET /api/config -> slot 'config' */
export const refreshConfig = loader('config', '/api/config');
/** GET /api/watchlist -> slot 'watchlist' (normalised entries: id,symbol,label,kind,role,listing,...) */
export const refreshWatchlist = loader('watchlist', '/api/watchlist');
/** GET /api/alerts -> slot 'alerts' ({items, unread}) */
export const refreshAlerts = loader('alerts', '/api/alerts?limit=100');
/** GET /api/lane-status -> slot 'lane' */
export const refreshLane = loader('lane', '/api/lane-status');
/** GET /api/market-light -> slot 'marketLight' */
export const refreshMarketLight = loader('marketLight', '/api/market-light');
/** GET /api/advice -> slot 'advice' ({ID: {published, pending, draft, p20, expExcess20Pct, benchmark, excerpt, ...}}) */
export const refreshAdvice = loader('advice', '/api/advice');
/** GET /api/policy -> slot 'policy' (C9: configured, targets, drift, maxSingleStock, contribution, trend) */
export const refreshPolicy = loader('policy', '/api/policy');
/** GET /api/events?days=30 -> slot 'events' ({events:[{date, kind, ticker, title, ...}]}) */
export const refreshEvents = loader('events', '/api/events?days=30');
/** GET /api/alert-rules -> slot 'alertRules' (effective v3 rules + sigma {id: {sigmaPct}}) */
export const refreshAlertRules = loader('alertRules', '/api/alert-rules');
/** GET /api/portfolio/risk -> slot 'risk' (C4: exposures, volatility, beta, drawdown, correlation, concentration) */
export const refreshRisk = loader('risk', '/api/portfolio/risk', { timeoutMs: 30000 });

/** Reload everything the shell shows (after a settings save, on manual refresh). */
export function refreshAll() {
  return Promise.all([
    refreshPortfolio(), refreshPrices(), refreshConfig(), refreshWatchlist(),
    refreshAlerts(), refreshLane(), refreshMarketLight(),
    refreshAdvice(), refreshPolicy(), refreshEvents(), refreshAlertRules(), refreshRisk(),
  ]);
}

// ------------------------------------------------------------------ lookups

/** Watchlist entry for an id (or undefined). Falls back to config.watchlist when /api/watchlist failed. */
export function entryOf(id) {
  const wl = slot('watchlist').data;
  const list = Array.isArray(wl) ? wl : (slot('config').data && slot('config').data.watchlist) || [];
  return list.find(e => e && e.id === id);
}

export function labelOf(id) {
  const e = entryOf(id);
  const p = positionOf(id);
  return (e && e.label) || (p && p.label) || id;
}

/** Position row from /api/portfolio for an id (or undefined). */
export function positionOf(id) {
  const d = slot('portfolio').data;
  return d && Array.isArray(d.positions) ? d.positions.find(p => p.id === id) : undefined;
}

/** Item from /api/prices for an id (the payload keys items by `ticker`; `id` accepted too). */
export function priceItemOf(id) {
  const d = slot('prices').data;
  return Array.isArray(d) ? d.find(p => p && (p.ticker === id || p.id === id)) : undefined;
}

/** Config portfolio entry {shares, investedAmount} for an id. */
export function configPositionOf(id) {
  const c = slot('config').data;
  return c && c.portfolio ? c.portfolio[id] : undefined;
}

/** Select a holding (null = back to overview). */
export function selectHolding(id) {
  setStore('selectedId', id || null);
}
