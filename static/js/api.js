// Same-origin fetch wrapper. Never throws: every call resolves to
//   { ok, data, error, status, stale, aborted }
// `error` is a human string (server {detail|error} when present). HTTP errors are
// never turned into empty data - callers render ok:false as an error state.

const DEFAULT_TIMEOUT_MS = 20000;

function errorText(body, status, statusText) {
  if (body && typeof body === 'object') {
    const d = body.detail !== undefined ? body.detail : body.error;
    if (typeof d === 'string' && d) return d;
    if (Array.isArray(d)) {
      const msgs = d.map(x => (x && (x.msg || x.message)) || (typeof x === 'string' ? x : '')).filter(Boolean);
      if (msgs.length) return msgs.join('; ');
    }
    if (d && typeof d === 'object') return JSON.stringify(d).slice(0, 200);
    if (typeof body.message === 'string' && body.message) return body.message;
  }
  if (status === 401) return 'Not authorised (401)';
  if (status === 404) return 'Not found (404)';
  if (status === 503) return 'Service unavailable (503)';
  return `HTTP ${status}${statusText ? ' ' + statusText : ''}`;
}

async function request(method, url, body, { signal, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeoutMs);
  const onAbort = () => ctl.abort();
  if (signal) {
    if (signal.aborted) ctl.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  try {
    const init = { method, signal: ctl.signal, headers: { Accept: 'application/json' }, cache: 'no-store' };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    const text = await res.text();
    let data = null;
    let parseFailed = false;
    if (text) {
      try { data = JSON.parse(text); } catch (_) { parseFailed = true; }
    }
    if (!res.ok) {
      return { ok: false, data, error: errorText(data, res.status, res.statusText), status: res.status, stale: false, aborted: false };
    }
    if (parseFailed) {
      return { ok: false, data: null, error: 'Server returned invalid JSON', status: res.status, stale: false, aborted: false };
    }
    return { ok: true, data, error: null, status: res.status, stale: !!(data && typeof data === 'object' && data.stale === true), aborted: false };
  } catch (e) {
    const aborted = !!(signal && signal.aborted);
    let error = e && e.message ? e.message : 'network error';
    if (timedOut) error = `timed out after ${Math.round(timeoutMs / 1000)} s`;
    else if (aborted) error = 'aborted';
    return { ok: false, data: null, error, status: 0, stale: false, aborted };
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
}

/** GET url -> {ok,data,error,status,stale,aborted}. opts: {signal, timeoutMs}. */
export function apiGet(url, opts) {
  return request('GET', url, undefined, opts);
}

/** apiSend('POST'|'PUT'|'DELETE', url, jsonBody, opts) -> same shape as apiGet. */
export function apiSend(method, url, body, opts) {
  return request(method.toUpperCase(), url, body === undefined ? {} : body, opts);
}

/**
 * Stale-response guard for "latest request wins" flows (ticker switch, range switch).
 *   const lr = latestRequest();
 *   const t = lr.begin();               // aborts the previous one
 *   const r = await apiGet(url, {signal: t.signal});
 *   if (!t.current()) return;           // superseded: drop the response
 *   lr.abort() on teardown.
 */
export function latestRequest() {
  let ctl = null;
  let seq = 0;
  return {
    begin() {
      if (ctl) ctl.abort();
      ctl = new AbortController();
      const mine = ++seq;
      const c = ctl;
      return { signal: c.signal, current: () => mine === seq && !c.signal.aborted };
    },
    abort() {
      seq++;
      if (ctl) ctl.abort();
      ctl = null;
    },
  };
}

export const api = { apiGet, apiSend, latestRequest };
