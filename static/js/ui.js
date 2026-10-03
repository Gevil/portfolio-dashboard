// UI primitives: toasts, dialogs/drawers (focus trap, Escape, focus restore),
// panel states, relative-time stamps, visibility-aware polling.
import { el, clear, ago } from './util.js';
import { renderMarkdown } from './markdown.js';

// ---------------------------------------------------------------- markdown

/** Set sanitised markdown as the content of `node` (the ONLY innerHTML path). */
export function setMarkdown(node, text) {
  node.innerHTML = renderMarkdown(text); // renderMarkdown sanitises (DOMPurify) or escapes
  return node;
}

// ---------------------------------------------------------------- toasts

const TOAST_MS = { info: 6000, ok: 5000, warn: 9000, error: 12000 };

function toastRegion() {
  let r = document.getElementById('toast-region');
  if (!r) {
    r = el('div', { id: 'toast-region', class: 'toast-region', 'aria-live': 'polite', 'aria-atomic': 'false' });
    document.body.append(r);
  }
  return r;
}

/**
 * toast(message, {type:'info'|'ok'|'warn'|'error', title, timeoutMs, onClick}) -> {close}
 * Timeouts pause on hover/focus; warn/error are announced assertively.
 * Live alerts must ALSO be stored in the alert center - a toast is not the record.
 */
export function toast(message, { type = 'info', title = '', timeoutMs, onClick } = {}) {
  const region = toastRegion();
  const urgent = type === 'error' || type === 'warn';
  let timer = null;
  let remaining = timeoutMs != null ? timeoutMs : (TOAST_MS[type] || TOAST_MS.info);
  let started = 0;
  const node = el('div', { class: `toast toast-${type}`, role: urgent ? 'alert' : 'status' },
    el('div', { class: 'toast-main' },
      title ? el('div', { class: 'toast-title', text: title }) : null,
      el('div', { class: 'toast-msg', text: String(message == null ? '' : message) })),
    el('button', { class: 'toast-close', type: 'button', 'aria-label': 'Dismiss notification', text: '\u00d7', onclick: () => close() }));
  if (onClick) {
    node.classList.add('is-clickable');
    node.querySelector('.toast-main').addEventListener('click', () => { onClick(); close(); });
  }
  function arm() {
    if (remaining <= 0) return;
    started = Date.now();
    timer = setTimeout(close, remaining);
  }
  function pause() {
    if (timer == null) return;
    clearTimeout(timer); timer = null;
    remaining -= Date.now() - started;
  }
  function close() {
    if (!node.isConnected) return;
    clearTimeout(timer); timer = null;
    node.remove();
  }
  node.addEventListener('mouseenter', pause);
  node.addEventListener('mouseleave', () => { if (timer == null) arm(); });
  node.addEventListener('focusin', pause);
  node.addEventListener('focusout', () => { if (timer == null) arm(); });
  region.append(node);
  while (region.children.length > 5) region.firstChild.remove();
  arm();
  return { close };
}

// ---------------------------------------------------------------- dialogs

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
const stack = []; // open dialogs, topmost last

function overlayRoot() {
  let r = document.getElementById('overlay-root');
  if (!r) {
    r = el('div', { id: 'overlay-root' });
    document.body.append(r);
  }
  return r;
}

function visibleFocusables(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(n => n.offsetParent !== null || n === document.activeElement);
}

function setBackgroundInert(on) {
  const app = document.getElementById('app');
  if (app) app.inert = on;
  // lower dialogs stay inert while a higher one is open
  stack.forEach((d, i) => { d.root.inert = on ? i < stack.length - 1 : false; });
}

let keyBound = false;
function onKeydown(e) {
  const top = stack[stack.length - 1];
  if (!top) return;
  if (e.key === 'Escape' && !e.defaultPrevented) {
    e.preventDefault();
    top.close();
    return;
  }
  if (e.key === 'Tab') {
    const f = visibleFocusables(top.panel);
    if (!f.length) { e.preventDefault(); top.panel.focus(); return; }
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === top.panel)) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }
}

/**
 * openDialog({title, body:Node, kind:'modal'|'drawer', size:'md'|'lg'|'xl', closeLabel,
 *             onClose, initialFocus:Element|selector, headerExtra:Node})
 *   -> {root, panel, body, titleEl, close(), setTitle(t)}
 * role=dialog + aria-modal + aria-labelledby, Tab trapped, Escape/backdrop/close button close,
 * focus returns to the element focused at open time. Background (#app) is inert while open.
 */
export function openDialog({ title = '', body, kind = 'modal', size = 'md', closeLabel = 'Close', onClose, initialFocus, headerExtra } = {}) {
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const titleId = 'dlg-title-' + Math.random().toString(36).slice(2, 8);
  const titleEl = el('h2', { class: 'dialog-title', id: titleId, text: title });
  const bodyEl = el('div', { class: 'dialog-body' }, body);
  const closeBtn = el('button', { class: 'dialog-close', type: 'button', 'aria-label': closeLabel, text: '\u00d7' });
  const panel = el('div', {
    class: `dialog-panel dialog-${kind} dialog-${size}`, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1',
  }, el('header', { class: 'dialog-header' }, titleEl, headerExtra || null, closeBtn), bodyEl);
  const backdrop = el('div', { class: 'dialog-backdrop' });
  const root = el('div', { class: `dialog-root dialog-root-${kind}` }, backdrop, panel);
  let closed = false;
  const handle = {
    root, panel, body: bodyEl, titleEl,
    setTitle(t) { titleEl.textContent = t; },
    close() {
      if (closed) return;
      closed = true;
      const i = stack.indexOf(handle);
      if (i >= 0) stack.splice(i, 1);
      root.remove();
      setBackgroundInert(stack.length > 0);
      if (!stack.length) document.documentElement.classList.remove('has-dialog');
      if (opener && opener.isConnected) opener.focus();
      if (onClose) onClose();
    },
  };
  closeBtn.addEventListener('click', () => handle.close());
  backdrop.addEventListener('click', () => handle.close());
  if (!keyBound) { document.addEventListener('keydown', onKeydown); keyBound = true; }
  stack.push(handle);
  overlayRoot().append(root);
  document.documentElement.classList.add('has-dialog');
  setBackgroundInert(true);
  let target = null;
  if (typeof initialFocus === 'string') target = panel.querySelector(initialFocus);
  else if (initialFocus instanceof HTMLElement) target = initialFocus;
  (target || closeBtn).focus();
  return handle;
}

/** confirmDialog(message, {title, confirmText, danger}) -> Promise<boolean>. */
export function confirmDialog(message, { title = 'Please confirm', confirmText = 'Confirm', cancelText = 'Cancel', danger = false } = {}) {
  return new Promise(resolve => {
    let answer = false;
    const ok = el('button', { type: 'button', class: danger ? 'btn btn-danger' : 'btn btn-primary', text: confirmText, onclick: () => { answer = true; d.close(); } });
    const cancel = el('button', { type: 'button', class: 'btn', text: cancelText, onclick: () => d.close() });
    const d = openDialog({
      title, size: 'sm', initialFocus: cancel, onClose: () => resolve(answer),
      body: el('div', { class: 'confirm-body' }, el('p', { text: message }), el('div', { class: 'dialog-actions' }, cancel, ok)),
    });
  });
}

// ---------------------------------------------------------------- panel states

/** Skeleton lines. */
export function skeleton(lines = 3) {
  const box = el('div', { class: 'skeleton-box', 'aria-hidden': 'true' });
  for (let i = 0; i < lines; i++) box.append(el('span', { class: 'skeleton-line' + (i === lines - 1 ? ' is-short' : '') }));
  return box;
}

/**
 * renderState(container, {loading, error, empty, retry}) - clears `container` and renders ONE
 * non-content state (error wins over loading wins over empty). Returns nothing.
 *  loading: true | 'text'   error: string | Error   empty: 'text'   retry: fn (adds Retry button on error)
 * Errors are never rendered as empty states; pass the api `error` string straight in.
 */
export function renderState(container, { loading, error, empty, retry } = {}) {
  clear(container);
  container.removeAttribute('aria-busy');
  if (error) {
    const msg = error instanceof Error ? error.message : String(error);
    container.append(el('div', { class: 'state state-error', role: 'alert' },
      el('span', { class: 'state-icon', 'aria-hidden': 'true', text: '!' }),
      el('div', { class: 'state-text' },
        el('div', { class: 'state-title', text: 'Could not load' }),
        el('div', { class: 'state-detail', text: msg })),
      retry ? el('button', { class: 'btn btn-sm', type: 'button', text: 'Retry', onclick: () => retry() }) : null));
  } else if (loading) {
    container.setAttribute('aria-busy', 'true');
    container.append(el('div', { class: 'state state-loading', role: 'status' },
      skeleton(3),
      el('span', { class: 'visually-hidden', text: typeof loading === 'string' ? loading : 'Loading' })));
  } else if (empty) {
    container.append(el('div', { class: 'state state-empty' }, el('div', { class: 'state-text', text: String(empty) })));
  }
}

// ---------------------------------------------------------------- stamps

const stamps = new Set();
let stampTimer = null;

function paintStamp(node) {
  const ts = node._ts;
  const text = ts ? ago(ts) : '';
  const stale = node.classList.contains('is-stale');
  node.textContent = text ? `${node._prefix}${text}` : '';
  node.title = stale ? 'Data may be out of date' : '';
}

/**
 * stamp(ts, {stale, prefix='updated '}) -> <span class="stamp"> that re-renders itself
 * ("updated 12 s ago"). ts: unix seconds/ms/Date. Update with node.setTs(ts, stale).
 */
export function stamp(ts, { stale = false, prefix = 'updated ' } = {}) {
  const node = el('span', { class: 'stamp' + (stale ? ' is-stale' : '') });
  node._ts = ts;
  node._prefix = prefix;
  node.setTs = (t, s) => {
    node._ts = t;
    if (s !== undefined) node.classList.toggle('is-stale', !!s);
    paintStamp(node);
  };
  paintStamp(node);
  stamps.add(node);
  if (!stampTimer) {
    stampTimer = setInterval(() => {
      if (document.hidden) return;
      for (const n of stamps) {
        if (!n.isConnected) { stamps.delete(n); continue; }
        paintStamp(n);
      }
      if (!stamps.size) { clearInterval(stampTimer); stampTimer = null; }
    }, 5000);
  }
  return node;
}

// ---------------------------------------------------------------- polling

/**
 * usePolling(fn, ms, {visibilityAware=true, immediate=false}) -> stop()
 * Runs fn every ms without overlapping runs. While the tab is hidden nothing runs; on becoming
 * visible fn runs immediately if the last run is older than ms. fn may be async.
 */
export function usePolling(fn, ms, { visibilityAware = true, immediate = false } = {}) {
  let timer = null;
  let stopped = false;
  let running = false;
  let lastRun = 0;
  async function run() {
    if (stopped || running) return;
    running = true;
    lastRun = Date.now();
    try { await fn(); } catch (e) { console.error('poll failed', e); } finally { running = false; schedule(); }
  }
  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (stopped || (visibilityAware && document.hidden)) return;
    const wait = Math.max(0, ms - (Date.now() - lastRun));
    timer = setTimeout(run, wait);
  }
  function onVis() {
    if (stopped) return;
    if (document.hidden) { clearTimeout(timer); timer = null; return; }
    if (Date.now() - lastRun >= ms) run(); else schedule();
  }
  if (visibilityAware) document.addEventListener('visibilitychange', onVis);
  if (immediate) run(); else { lastRun = Date.now(); schedule(); }
  return function stop() {
    stopped = true;
    clearTimeout(timer);
    timer = null;
    document.removeEventListener('visibilitychange', onVis);
  };
}

export const ui = { toast, openDialog, confirmDialog, renderState, skeleton, stamp, usePolling, setMarkdown };
