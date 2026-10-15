// Alert center: a drawer listing every stored alert (store slot 'alerts', filled by
// data.refreshAlerts()), plus the live-alert toast. The toast is NOT the record - every
// alert that reaches handleLiveAlert is also persisted server-side and shows up here.
// openAlertCenter({focusId}) opens (or re-focuses) the drawer; with a focusId it loads the
// last 200 alerts, scrolls that row into view, highlights it and moves focus to it.
import { el, clear, fmtDateTime24, ago, safeUrl } from '../util.js';
import { apiGet, apiSend, latestRequest } from '../api.js';
import * as store from '../store.js';
import { toast, openDialog, renderState, stamp, usePolling } from '../ui.js';
import { refreshAlerts, selectHolding, entryOf } from '../data.js';

const POLL_MS = 30000;
const DEFAULT_LIMIT = 100; // what data.refreshAlerts() loads
const WIDE_LIMIT = 200;    // loaded while a focus request needs older rows
const TOAST_SEEN_MAX = 200;
const TOAST_MSG_MAX = 220;

const SEVERITIES = [
  { id: 'all', label: 'All' },
  { id: 'urgent', label: 'Urgent' },
  { id: 'warn', label: 'Warn' },
  { id: 'info', label: 'Info' },
];
const SEV_META = {
  urgent: { label: 'Urgent', icon: '\u203C', chip: 'chip-err', toast: 'error' },
  warn: { label: 'Warn', icon: '\u25B2', chip: 'chip-warn', toast: 'warn' },
  info: { label: 'Info', icon: '\u2139', chip: 'chip-info', toast: 'info' },
};

/** Any backend severity spelling -> 'urgent' | 'warn' | 'info'. */
export function normalizeSeverity(s) {
  const v = String(s == null ? '' : s).toLowerCase();
  if (v === 'urgent' || v === 'critical' || v === 'error') return 'urgent';
  if (v === 'warn' || v === 'warning') return 'warn';
  return 'info';
}

/** Unread count from the shared alerts slot (0 while unknown). */
export function unreadCount() {
  const n = store.slot('alerts').data?.unread;
  return Number.isFinite(n) && n > 0 ? n : 0;
}

// ------------------------------------------------------------------ live toast

const seenToasts = new Set(); // insertion-ordered, bounded

function rememberToast(id) {
  seenToasts.add(id);
  if (seenToasts.size > TOAST_SEEN_MAX) seenToasts.delete(seenToasts.values().next().value);
}

/**
 * Toast for an alert pushed over SSE. De-duplicated by alert id. Clicking the toast opens
 * the center. Never throws.
 */
export function handleLiveAlert(alert) {
  try {
    if (!alert || typeof alert !== 'object') return;
    if (alert.id != null) {
      const id = String(alert.id);
      if (seenToasts.has(id)) return;
      rememberToast(id);
    }
    const meta = SEV_META[normalizeSeverity(alert.severity)];
    const ticker = alert.ticker ? String(alert.ticker) + ' \u00B7 ' : '';
    let msg = alert.message == null ? '' : String(alert.message);
    if (msg.length > TOAST_MSG_MAX) msg = msg.slice(0, TOAST_MSG_MAX - 1) + '\u2026';
    toast(msg, {
      type: meta.toast,
      title: ticker + (alert.title ? String(alert.title) : 'Alert'),
      onClick: () => { openAlertCenter({ focusId: alert.id != null ? String(alert.id) : null }); },
    });
  } catch (e) {
    console.error('handleLiveAlert failed', e);
  }
}

// ------------------------------------------------------------------ center

let current = null; // the open drawer handle (singleton)
let focusRequest = null; // set while the drawer is open: (id) => void

/**
 * Open (or re-focus) the alert center drawer. Returns the dialog handle.
 * focusId: load enough rows to include that alert (limit 200), scroll it into view, give it
 * the `is-focus` class and move keyboard focus to it. Not found -> an inline note.
 */
export function openAlertCenter({ focusId } = {}) {
  const wanted = focusId == null || focusId === '' ? null : String(focusId);
  if (current) {
    if (wanted !== null && focusRequest) focusRequest(wanted);
    else current.panel.focus();
    return current;
  }

  let sev = 'all';
  let unreadOnly = false;
  let busy = false;
  let lastSig = null;
  let limit = wanted !== null ? WIDE_LIMIT : DEFAULT_LIMIT;
  let pendingFocus = wanted; // id waiting for the wide load to settle
  let highlightId = null;    // row carrying the highlight class
  let missingId = null;      // requested id that is not in the loaded rows
  let wideSettled = false;   // the limit=200 request has answered (ok or not)
  let wideOk = false;
  let wideData = null;       // the payload object of our own last wide load
  let selfWrite = false;     // true while this drawer itself writes the slot
  const lrWide = latestRequest();

  const badge = el('span', { class: 'badge ac-badge', hidden: true });
  const stampEl = stamp(null, { prefix: 'updated ' });

  const sevBtns = new Map();
  const sevGroup = el('div', { class: 'seg ac-sev', role: 'group', 'aria-label': 'Filter by severity' },
    SEVERITIES.map(s => {
      const b = el('button', {
        type: 'button', 'aria-pressed': s.id === sev ? 'true' : 'false', dataset: { sev: s.id }, text: s.label,
        onclick: () => { sev = s.id; syncControls(); render(true); },
      });
      sevBtns.set(s.id, b);
      return b;
    }));

  const unreadBtn = el('button', {
    type: 'button', class: 'btn btn-sm ac-unread-toggle', 'aria-pressed': 'false', text: 'Unread only',
    onclick: () => { unreadOnly = !unreadOnly; syncControls(); render(true); },
  });
  const markAllBtn = el('button', {
    type: 'button', class: 'btn btn-sm ac-markall', text: 'Mark all read',
    onclick: () => ack({ all: true }),
  });
  const summary = el('span', { class: 'ac-summary muted', role: 'status', 'aria-live': 'polite' });
  const banner = el('div', { class: 'ac-banner' });
  const list = el('ul', { class: 'ac-list', 'aria-label': 'Alerts' });
  const stateBox = el('div', { class: 'ac-state' });

  const root = el('div', { class: 'ac' },
    el('div', { class: 'ac-toolbar' }, sevGroup, unreadBtn, markAllBtn),
    el('div', { class: 'ac-statusrow' }, summary, stampEl),
    banner, stateBox, list);

  function syncControls() {
    for (const [id, b] of sevBtns) b.setAttribute('aria-pressed', id === sev ? 'true' : 'false');
    unreadBtn.setAttribute('aria-pressed', unreadOnly ? 'true' : 'false');
  }

  const handle = openDialog({
    title: 'Alerts', body: root, kind: 'drawer', size: 'md', initialFocus: sevBtns.get('all'),
    headerExtra: badge,
    onClose: () => {
      unsub();
      stopPoll();
      current = null;
      store.set('alertCenterOpen', false);
    },
  });
  current = handle;
  store.set('alertCenterOpen', true);

  const unsub = store.subscribe('alerts', () => {
    const s = store.slot('alerts');
    // another loader (SSE refresh) replaced our wide payload with the 100-row one: ask again
    if (limit > DEFAULT_LIMIT && wideSettled && !selfWrite && !s.loading && s.data && s.data !== wideData) load();
    render(false);
  });
  const stopPoll = usePolling(() => load(), POLL_MS);
  focusRequest = (id) => {
    pendingFocus = id;
    missingId = null;
    if (limit < WIDE_LIMIT) { limit = WIDE_LIMIT; wideSettled = false; }
    render(true);
    load();
  };

  const setSlot = (v) => {
    selfWrite = true;
    try { store.set('alerts', v); } finally { selfWrite = false; }
  };

  /** Load the alerts slot at the current limit (data.refreshAlerts for 100, our own request for 200). */
  async function load() {
    if (limit <= DEFAULT_LIMIT) return refreshAlerts();
    const t = lrWide.begin();
    selfWrite = true;
    try { store.slotLoading('alerts'); } finally { selfWrite = false; }
    const res = await apiGet(`/api/alerts?limit=${limit}`, { signal: t.signal, timeoutMs: 25000 });
    if (!t.current()) return null;
    selfWrite = true;
    try { store.slotApply('alerts', res); } finally { selfWrite = false; }
    if (!res.aborted) {
      wideSettled = true;
      wideOk = !!res.ok;
      if (res.ok) wideData = store.slot('alerts').data;
      render(true);
    }
    return res;
  }

  function retry() { load(); }

  function visibleItems(items) {
    return items.filter(a => (sev === 'all' || normalizeSeverity(a.severity) === sev) && (!unreadOnly || !a.acked));
  }

  /** Remember which action button had focus so a re-render does not drop it. */
  function focusKey() {
    const a = document.activeElement;
    if (!a || !list.contains(a)) return null;
    const li = a.closest('li[data-id]');
    return li ? { id: li.dataset.id, act: a.dataset.act || '' } : null;
  }

  function restoreFocus(key, shownIds) {
    if (!key) return;
    let target = null;
    if (shownIds.includes(key.id)) {
      const li = [...list.children].find(n => n.dataset.id === key.id);
      target = li && (li.querySelector(`[data-act="${key.act}"]`) || li.querySelector('[data-act]'));
    }
    if (!target) {
      // item vanished (e.g. marked read under "Unread only"): move to a stable control
      target = list.querySelector('[data-act="ack"]') || unreadBtn;
    }
    if (target) target.focus();
  }

  function focusItem(id) {
    const li = [...list.children].find(n => n.dataset.id === id);
    if (!li) return;
    const calm = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    li.scrollIntoView({ block: 'center', behavior: calm ? 'auto' : 'smooth' });
    li.focus({ preventScroll: true });
  }

  function render(force) {
    if (!handle.root.isConnected) return;
    const s = store.slot('alerts');
    const data = s.data;
    const items = data && Array.isArray(data.items) ? data.items : null;
    const unread = unreadCount();

    // resolve a pending focus request once the wide load has settled
    let focusNow = null;
    if (pendingFocus !== null && wideSettled) {
      if (items) {
        const id = pendingFocus;
        const hit = items.find(a => String(a.id) === id);
        pendingFocus = null;
        force = true;
        if (hit) {
          highlightId = id;
          missingId = null;
          focusNow = id;
          if (!visibleItems([hit]).length) { sev = 'all'; unreadOnly = false; syncControls(); } // a filter must not hide the row
        } else if (wideOk) {
          highlightId = null;
          missingId = id;
        }
      } else if (!s.loading) {
        pendingFocus = null; // the load failed and nothing is shown: the error state says so
      }
    }

    // cheap signature: skip DOM work on polls that changed nothing visible
    const sig = JSON.stringify([
      sev, unreadOnly, s.error, s.loading && !items, unread, Math.floor(Date.now() / 60000), // minute bucket keeps "x min ago" fresh
      highlightId, missingId,
      items ? items.map(a => [a.id, a.acked ? 1 : 0, a.title, a.message]) : null,
    ]);
    stampEl.setTs(s.ts, !!s.error || !!s.stale);
    const hadMarkAllFocus = document.activeElement === markAllBtn;
    badge.hidden = unread === 0;
    badge.textContent = String(unread);
    badge.setAttribute('aria-label', `${unread} unread`);
    markAllBtn.disabled = unread === 0;
    if (hadMarkAllFocus && markAllBtn.disabled) unreadBtn.focus();
    if (!force && sig === lastSig) return;
    lastSig = sig;

    const fk = focusKey();
    clear(banner);
    clear(stateBox);

    if (!items) {
      clear(list);
      summary.textContent = '';
      if (s.error) renderState(stateBox, { error: s.error, retry });
      else renderState(stateBox, { loading: 'Loading alerts' });
      return;
    }
    // stale data + failed refresh: keep the list, say so
    if (s.error) renderState(banner, { error: s.error, retry });
    if (missingId !== null) {
      banner.append(el('div', { class: 'ac-notfound', id: 'ac-notfound', role: 'status', dataset: { id: missingId }, text: 'alert not found (older than the last 200)' }));
    }

    const shown = visibleItems(items);
    summary.textContent = `${shown.length} of ${items.length} shown \u00B7 ${unread} unread`;
    clear(list);
    if (!items.length) {
      renderState(stateBox, { empty: 'No alerts yet.' });
      if (fk) unreadBtn.focus();
      return;
    }
    if (!shown.length) {
      if (fk) unreadBtn.focus();
      renderState(stateBox, { empty: unreadOnly ? 'No unread alerts match this filter.' : 'No alerts match this filter.' });
      return;
    }
    for (const a of shown) list.append(renderItem(a));
    if (focusNow) focusItem(focusNow);
    else restoreFocus(fk, shown.map(a => String(a.id)));
  }

  function renderItem(a) {
    const level = normalizeSeverity(a.severity);
    const meta = SEV_META[level];
    const id = String(a.id);
    const href = a.url ? safeUrl(a.url) : '';
    const ticker = a.ticker ? String(a.ticker) : '';
    const entry = ticker ? entryOf(ticker) : null;
    const selectable = !!entry && entry.role !== 'benchmark';
    const when = fmtDateTime24(a.time);
    const rel = ago(a.time);

    return el('li', {
      class: `ac-item ac-sev-${level}${a.acked ? ' is-acked' : ''}${id === highlightId ? ' is-focus' : ''}`, dataset: { id, sev: level }, tabindex: '-1',
    },
      el('div', { class: 'ac-item-meta' },
        el('span', { class: `chip ${meta.chip} ac-sev-chip` },
          el('span', { 'aria-hidden': 'true', text: meta.icon }), meta.label),
        ticker ? (selectable
          ? el('button', {
            type: 'button', class: 'chip ac-ticker', dataset: { act: 'ticker', ticker },
            'aria-label': `Show ${ticker}`, text: ticker,
          })
          : el('span', { class: 'chip ac-ticker', text: ticker })) : null,
        el('time', { class: 'ac-time num muted', text: rel ? `${when} \u00B7 ${rel}` : when }),
        a.source ? el('span', { class: 'ac-source muted', text: String(a.source) }) : null,
        a.acked ? null : el('span', { class: 'ac-unread-dot' },
          el('span', { class: 'dot dot-info', 'aria-hidden': 'true' }),
          el('span', { class: 'visually-hidden', text: 'Unread' }))),
      el('h3', { class: 'ac-title', text: a.title ? String(a.title) : '(no title)' }),
      a.message ? el('p', { class: 'ac-msg', text: String(a.message) }) : null,
      el('div', { class: 'ac-actions' },
        href ? el('a', {
          class: 'btn btn-ghost btn-sm ac-link', href, target: '_blank', rel: 'noopener noreferrer',
          text: 'Open source \u2197',
        }) : null,
        a.acked ? null : el('button', {
          type: 'button', class: 'btn btn-sm ac-ack', dataset: { act: 'ack' }, text: 'Mark read',
        })));
  }

  list.addEventListener('click', e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn || busy) return;
    const li = btn.closest('li[data-id]');
    if (!li) return;
    if (btn.dataset.act === 'ack') ack({ ids: [li.dataset.id] });
    else if (btn.dataset.act === 'ticker') {
      selectHolding(btn.dataset.ticker);
      handle.close();
    }
  });

  /** Optimistic ack with rollback + toast on failure; then reload from the server. */
  async function ack(body) {
    if (busy) return;
    const prev = store.slot('alerts');
    if (!prev.data || !Array.isArray(prev.data.items)) return;
    const wanted = body.all ? null : new Set(body.ids.map(String));
    const changed = prev.data.items.filter(a => !a.acked && (wanted === null || wanted.has(String(a.id))));
    if (!changed.length && !body.all) return;
    const changedIds = new Set(changed.map(a => String(a.id)));

    const optimistic = {
      ...prev.data,
      items: prev.data.items.map(a => (changedIds.has(String(a.id)) ? { ...a, acked: true } : a)),
      unread: body.all ? 0 : Math.max(0, (prev.data.unread || 0) - changed.length),
    };
    setSlot({ ...prev, data: optimistic });
    busy = true;
    root.classList.add('is-busy');
    root.setAttribute('aria-busy', 'true');
    let res;
    try {
      res = await apiSend('POST', '/api/alerts/ack', body);
    } finally {
      busy = false;
      root.classList.remove('is-busy');
      root.removeAttribute('aria-busy');
    }
    if (!res.ok) {
      // only roll back when no fresher server snapshot has landed in the meantime
      const now = store.slot('alerts');
      if (now.data === optimistic) setSlot({ ...now, data: prev.data });
      toast(res.error || 'Could not mark alerts as read', { type: 'error', title: 'Alerts' });
      return;
    }
    await load();
  }

  render(true);
  load();
  return handle;
}
