// Tiny pub/sub store. Keys used by the app:
//   portfolio, prices, watchlist, config, alerts, lane, marketLight, history:
//        resource slots {data, error, status, ts, loading, stale}
//        - data: last good payload (kept while a refresh fails; error then set too)
//        - ts:   ms epoch of last successful load (null if never)
//        - error: string|null of the LAST attempt
//   selectedId  string|null   selected holding id (never auto-selected)
//   view        'overview'|'digest'|'market'|'aiops'
//   theme       'light'|'dark' effective; themePref 'system'|'light'|'dark'
//   sse         {state:'connecting'|'live'|'down', lastTickTs:ms|null, lastEventTs:ms|null}
//   chatOpen, settingsOpen, alertCenterOpen  booleans

const values = new Map();
const subs = new Map();

export function get(key) {
  return values.get(key);
}

export function set(key, value) {
  const prev = values.get(key);
  if (Object.is(prev, value)) return value;
  values.set(key, value);
  const list = subs.get(key);
  if (list) for (const fn of [...list]) {
    try { fn(value, prev, key); } catch (e) { console.error('store subscriber failed for', key, e); }
  }
  return value;
}

/** Merge into an object slot: store.patch('sse', {state:'live'}). */
export function patch(key, partial) {
  return set(key, { ...(values.get(key) || {}), ...partial });
}

/** subscribe(key, fn(value, prev, key)) -> unsubscribe(). */
export function subscribe(key, fn) {
  let list = subs.get(key);
  if (!list) { list = new Set(); subs.set(key, list); }
  list.add(fn);
  return () => list.delete(fn);
}

// ---- resource slots ----------------------------------------------------

const EMPTY_SLOT = Object.freeze({ data: null, error: null, status: 0, ts: null, loading: false, stale: false });

export function slot(key) {
  return values.get(key) || EMPTY_SLOT;
}

/** Mark a resource as loading, keeping its previous data. */
export function slotLoading(key) {
  const s = slot(key);
  set(key, { ...s, loading: true });
}

/** Apply an api result {ok,data,error,status,stale} to a slot. */
export function slotApply(key, res) {
  const s = slot(key);
  if (res.aborted) { set(key, { ...s, loading: false }); return; }
  if (res.ok) {
    set(key, { data: res.data, error: null, status: res.status, ts: Date.now(), loading: false, stale: !!res.stale });
  } else {
    set(key, { ...s, error: res.error || 'request failed', status: res.status, loading: false });
  }
}

export const store = { get, set, patch, subscribe, slot, slotLoading, slotApply };
