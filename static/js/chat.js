// AI chat: a non-modal docked panel (bottom-right card; full-screen sheet on phones).
// POST /api/chat streams `data: {"delta":"..."}` / `data: {"error":"..."}` frames ending with
// `data: [DONE]` (app/api/chat.py). Replies are rendered as sanitised markdown; the user's own
// bubbles are plain text.
import { el, clear, safeJson, debounce } from './util.js';
import { apiGet, apiSend, latestRequest } from './api.js';
import * as store from './store.js';
import { toast, setMarkdown } from './ui.js';
import { refreshConfig } from './data.js';

export const CHAT_IDLE_MS = 60000;   // no bytes for this long -> abort with a timeout error
export const MAX_BUBBLES = 60;       // DOM trim (history is trimmed separately)
export const MAX_HISTORY = 30;       // messages sent as conversation memory (old behaviour)
const RENDER_MS = 60;                // markdown re-render throttle while streaming

const PRESETS = [
  { label: '5-gate investment check', prompt: 'Run a 5-gate investment check on the current thesis: 1) business quality, 2) growth durability, 3) valuation vs history and peers, 4) balance sheet and cash-flow resilience, 5) catalyst and risk asymmetry. Give PASS or FAIL per gate, the evidence behind each verdict, and the one thing you would verify next.' },
  { label: 'DCF-style valuation', prompt: 'Build a DCF-style valuation: state your assumptions for revenue growth, margin trajectory, capex, terminal growth and discount rate, then show bear, base and bull fair value and say which single assumption moves the answer most.' },
  { label: 'Risk audit: what breaks this thesis', prompt: 'Risk audit: what breaks this thesis? List the failure modes ordered by probability times impact, the leading indicator for each, and the level or event that would invalidate the thesis outright.' },
  { label: 'Catalyst map next 4 weeks', prompt: 'Map the catalysts for the next 4 weeks: earnings, guidance, product, regulatory and macro events. For each give the expected direction, how price-sensitive the name is to it, and how you would position ahead of it.' },
];

// ------------------------------------------------------------------ stream framing

const DONE = Symbol('done');

/** One SSE frame (lines up to a blank line) -> event | DONE | null. */
function parseFrame(frame) {
  const data = [];
  for (const line of frame.split('\n')) {
    if (!line.startsWith('data:')) continue; // comments (':') and other SSE fields are ignored
    data.push(line.slice(line.charAt(5) === ' ' ? 6 : 5));
  }
  if (!data.length) return null;
  const payload = data.join('\n');
  if (payload.trim() === '[DONE]') return DONE;
  const obj = safeJson(payload, null);
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null; // malformed frame: skip
  if (obj.error) return { type: 'error', message: String(obj.error) };
  const ev = {};
  if (typeof obj.model === 'string' && obj.model) ev.model = obj.model;
  if (typeof obj.lane === 'string' && obj.lane) ev.lane = obj.lane;
  if (typeof obj.delta === 'string' && obj.delta) { ev.type = 'delta'; ev.text = obj.delta; return ev; }
  if (ev.model || ev.lane) { ev.type = 'meta'; return ev; }
  return null;
}

/**
 * Decode a fetch() body of the chat framing into events (async generator):
 *   {type:'delta', text, model?, lane?} | {type:'meta', model?, lane?} | {type:'error', message}
 *   and exactly one final {type:'end', complete} (complete=false: stream ended without [DONE]).
 * Frames may be split across chunks; malformed JSON frames are skipped. `onRead` runs after every
 * read (idle watchdog). Breaking out of the loop cancels the reader.
 */
export async function* parseChatStream(stream, { onRead } = {}) {
  const reader = stream.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let complete = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (onRead) onRead();
      if (done) { buf += dec.decode(); break; }
      buf = (buf + dec.decode(value, { stream: true })).replace(/\r\n/g, '\n');
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const r = parseFrame(buf.slice(0, i));
        buf = buf.slice(i + 2);
        if (r === DONE) { complete = true; break; }
        if (r) yield r;
      }
      if (complete) break;
    }
    if (!complete && buf.trim()) { // final frame without its blank-line terminator
      const r = parseFrame(buf.replace(/\r\n/g, '\n'));
      if (r === DONE) complete = true;
      else if (r) yield r;
    }
    yield { type: 'end', complete };
  } finally {
    reader.cancel().catch(() => {});
  }
}

/** Conversation memory: last MAX_HISTORY messages, always starting on a user turn. */
export function trimHistory(history, max = MAX_HISTORY) {
  const out = history.slice(-max);
  while (out.length && out[0].role !== 'user') out.shift();
  return out;
}

// ------------------------------------------------------------------ the panel

let panelSeq = 0;

/** mountChat(host) -> {open, close, toggle, isOpen, destroy} */
export function mountChat(host) {
  const uid = ++panelSeq;
  const titleId = `chat-title-${uid}`;
  let opened = false;
  let opener = null;
  let busy = false;
  let ctl = null;              // AbortController of the in-flight request
  let history = [];            // completed turns [{role, content}]
  let modelsLoaded = false;
  let modelIds = [];
  let destroyed = false;
  const modelsReq = latestRequest();

  // ---- DOM
  const titleEl = el('h2', { class: 'chat-title', id: titleId, text: 'AI Assistant' });
  const laneEl = el('p', { class: 'chat-lane muted', hidden: true });
  const modelSel = el('select', { class: 'chat-model', 'aria-label': 'Chat model', disabled: true },
    el('option', { value: '', text: 'Loading models\u2026' }));
  const clearBtn = el('button', {
    type: 'button', class: 'icon-btn chat-clear', title: 'Clear conversation', 'aria-label': 'Clear conversation',
    onclick: () => resetConversation(),
  }, el('span', { 'aria-hidden': 'true', text: '\u21BA' }));
  const closeBtn = el('button', {
    type: 'button', class: 'icon-btn chat-close', title: 'Close chat (Esc)', 'aria-label': 'Close chat',
    onclick: () => close(),
  }, el('span', { 'aria-hidden': 'true', text: '\u00D7' }));

  const hint = el('p', { class: 'chat-hint muted', text: 'Ask about your portfolio, a holding, or pick a prompt below. Answers come from the local AI lane.' });
  const log = el('div', {
    class: 'chat-log', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions text',
    'aria-label': 'Conversation', tabindex: '0',
  });

  const chips = el('div', { class: 'chat-chips', role: 'group', 'aria-label': 'Prompt ideas' },
    PRESETS.map(p => el('button', {
      type: 'button', class: 'chip chat-chip', dataset: { prompt: p.prompt }, text: p.label,
      onclick: () => insertPrompt(p.prompt),
    })));

  const input = el('textarea', {
    class: 'chat-input', rows: '1', placeholder: 'Ask about your portfolio\u2026', 'aria-label': 'Message',
    autocomplete: 'off', enterkeyhint: 'send',
  });
  const sendBtn = el('button', { type: 'submit', class: 'btn btn-primary chat-send', text: 'Send' });
  const stopBtn = el('button', {
    type: 'button', class: 'btn btn-danger chat-stop', hidden: true, text: 'Stop',
    onclick: () => stopStream(),
  });
  const form = el('form', { class: 'chat-form' }, input, el('div', { class: 'chat-form-btns' }, stopBtn, sendBtn));

  const panel = el('section', {
    class: 'chat-panel', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': titleId, tabindex: '-1', hidden: true,
  },
    el('header', { class: 'chat-head' },
      el('div', { class: 'chat-head-main' }, titleEl, laneEl),
      el('div', { class: 'chat-head-tools' }, modelSel, clearBtn, closeBtn)),
    el('div', { class: 'chat-body' }, hint, log),
    chips, form);
  host.append(panel);

  // ---- helpers

  function nearBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  }
  function scrollDown() { log.scrollTop = log.scrollHeight; }

  function updateHint() { hint.hidden = log.children.length > 0; }

  function pushNode(node) {
    const stick = nearBottom();
    log.append(node);
    while (log.children.length > MAX_BUBBLES && log.firstElementChild !== node) log.firstElementChild.remove();
    updateHint();
    if (stick) scrollDown();
  }

  function setBusy(v) {
    busy = v;
    sendBtn.disabled = v;
    stopBtn.hidden = !v;
    clearBtn.disabled = v;
    if (v) log.setAttribute('aria-busy', 'true'); else log.removeAttribute('aria-busy');
  }

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight + 2, 128) + 'px';
  }

  function insertPrompt(text) {
    input.value = text; // never auto-send
    autoGrow();
    input.focus();
    try { input.setSelectionRange(text.length, text.length); } catch (_) { /* not selectable */ }
  }

  function userBubble(text) {
    pushNode(el('div', { class: 'chat-msg chat-user' }, el('div', { class: 'chat-bubble', text })));
    scrollDown();
  }

  function typingNode() {
    return el('div', { class: 'chat-typing', role: 'status' },
      el('span', { 'aria-hidden': 'true' }), el('span', { 'aria-hidden': 'true' }), el('span', { 'aria-hidden': 'true' }),
      el('span', { class: 'visually-hidden', text: 'AI is thinking' }));
  }

  function resetConversation() {
    if (busy) return;
    history = [];
    clear(log);
    updateHint();
    input.focus();
  }

  // ---- lane line (what is serving the chat) from the shared lane slot
  function renderLane() {
    const s = store.slot('lane');
    const d = s.data;
    if (d && (d.lane || d.model)) {
      const model = d.serving_model === false ? 'not serving' : (d.model || '');
      laneEl.textContent = ['Lane ' + (d.lane || '?'), model].filter(Boolean).join(' \u00B7 ');
      laneEl.hidden = false;
    } else if (s.error) {
      laneEl.textContent = 'Lane status unavailable';
      laneEl.hidden = false;
    } else {
      laneEl.hidden = true;
    }
  }

  // ---- models
  function configuredModel() {
    const m = store.slot('config').data?.chatModel;
    return typeof m === 'string' ? m.trim() : '';
  }

  function syncModelSelect() {
    if (!modelsLoaded || !modelIds.length) return;
    const cur = configuredModel();
    for (const o of [...modelSel.options]) if (o.dataset.extra) o.remove();
    let pick = null;
    if (cur && modelIds.includes(cur)) pick = cur;
    else if (!cur || cur === 'lane') pick = modelIds.includes('lane') ? 'lane' : modelIds[0];
    else {
      modelSel.append(el('option', { value: cur, text: `${cur} (configured, not listed)`, dataset: { extra: '1' } }));
      pick = cur;
    }
    modelSel.value = pick;
  }

  async function loadModels() {
    if (modelsLoaded || destroyed) return;
    const t = modelsReq.begin();
    const res = await apiGet('/api/models', { signal: t.signal });
    if (!t.current() || destroyed) return;
    clear(modelSel);
    const models = res.ok && Array.isArray(res.data?.models) ? res.data.models.filter(m => m && m.id) : null;
    if (!models || !models.length) {
      modelSel.append(el('option', { value: '', text: res.ok ? 'No models listed' : 'Models unavailable' }));
      modelSel.disabled = true;
      modelSel.title = res.ok ? 'The server listed no chat models' : (res.error || 'Could not load the model list');
      return; // retried the next time the panel opens
    }
    modelsLoaded = true;
    modelIds = models.map(m => String(m.id));
    for (const m of models) modelSel.append(el('option', { value: String(m.id), text: String(m.name || m.id) }));
    modelSel.disabled = false;
    modelSel.title = '';
    syncModelSelect();
  }

  modelSel.addEventListener('change', async () => {
    const model = modelSel.value;
    if (!model) return;
    const before = configuredModel();
    modelSel.disabled = true;
    const res = await apiSend('PUT', '/api/config', { chatModel: model });
    modelSel.disabled = false;
    if (destroyed) return;
    if (!res.ok) {
      toast(res.error || 'Could not change the chat model', { type: 'error', title: 'Chat model' });
      if (before && modelIds.includes(before)) modelSel.value = before; else syncModelSelect();
      return;
    }
    toast(modelSel.selectedOptions[0]?.textContent || model, { type: 'ok', title: 'Chat model changed' });
    refreshConfig();
  });

  // ---- sending
  function stopStream() {
    if (ctl) ctl.abort('stop');
  }

  function errorBubble(message, onRetry, related) {
    const node = el('div', { class: 'chat-msg chat-error', role: 'alert' },
      el('div', { class: 'chat-bubble' },
        el('span', { class: 'chat-error-icon', 'aria-hidden': 'true', text: '!' }),
        el('span', { class: 'chat-error-text', text: message }),
        el('button', {
          type: 'button', class: 'btn btn-sm chat-retry', text: 'Retry',
          onclick: () => { node.remove(); related.forEach(n => n.remove()); updateHint(); onRetry(); },
        })));
    pushNode(node);
    return node;
  }

  async function send(text, { retry = false } = {}) {
    if (busy || !text) return;
    if (!retry) userBubble(text);
    setBusy(true);

    const typing = typingNode();
    const body = el('div', { class: 'chat-bubble md' }, typing);
    const metaEl = el('div', { class: 'chat-meta muted', hidden: true });
    const botNode = el('div', { class: 'chat-msg chat-bot' }, body, metaEl);
    pushNode(botNode);

    let answer = '';
    let renderTimer = null;
    let model = '';
    let lane = '';
    let failure = null;      // human string
    let ended = false;       // saw [DONE]
    let timedOut = false;
    let stopped = false;
    let idleTimer = null;

    const render = () => {
      renderTimer = null;
      const stick = nearBottom();
      setMarkdown(body, answer);
      if (stick) scrollDown();
    };
    const arm = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => { timedOut = true; ctl.abort('timeout'); }, CHAT_IDLE_MS);
    };

    ctl = new AbortController();
    arm();
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ messages: [...history, { role: 'user', content: text }] }),
        signal: ctl.signal,
      });
      if (!res.ok) {
        let detail = '';
        try {
          const j = safeJson(await res.text(), null);
          detail = j && (j.error || j.detail) ? String(j.error || j.detail) : '';
        } catch (_) { /* body unreadable */ }
        throw Object.assign(new Error(detail || `AI request failed (HTTP ${res.status}).`), { handled: true });
      }
      if (!res.body) throw Object.assign(new Error('AI response had no body.'), { handled: true });
      model = res.headers.get('X-Chat-Model') || '';
      lane = res.headers.get('X-Chat-Lane') || '';
      for await (const ev of parseChatStream(res.body, { onRead: arm })) {
        if (ev.model) model = ev.model;
        if (ev.lane) lane = ev.lane;
        if (ev.type === 'delta') {
          answer += ev.text;
          if (!renderTimer) renderTimer = setTimeout(render, RENDER_MS);
        } else if (ev.type === 'error') {
          failure = ev.message;
        } else if (ev.type === 'end') {
          ended = ev.complete;
        }
      }
    } catch (e) {
      if (timedOut) failure = `The AI did not answer within ${CHAT_IDLE_MS / 1000} s.`;
      else if (ctl.signal.aborted) stopped = true;
      else failure = e && e.handled ? e.message : 'AI unavailable. Check the connection and try again.';
    } finally {
      clearTimeout(idleTimer);
      clearTimeout(renderTimer);
      ctl = null;
    }
    if (destroyed) return;

    if (answer) setMarkdown(body, answer); else typing.remove();
    const who = [lane && `lane ${lane}`, model].filter(Boolean).join(' \u00B7 ');
    if (who) { metaEl.textContent = who; metaEl.hidden = false; }

    if (answer && !failure && (ended || stopped)) {
      // a finished (or deliberately stopped) answer becomes conversation memory
      history = trimHistory([...history, { role: 'user', content: text }, { role: 'assistant', content: answer }]);
      if (stopped) { metaEl.textContent = (who ? who + ' \u00B7 ' : '') + 'stopped'; metaEl.hidden = false; }
    } else if (stopped && !answer) {
      botNode.remove();
      if (!input.value.trim()) { input.value = text; autoGrow(); }
      updateHint();
    } else {
      if (!answer) botNode.remove(); else botNode.classList.add('is-partial');
      if (!failure) failure = answer ? 'The connection closed before the answer was finished.' : 'No response from AI.';
      errorBubble(failure, () => send(text, { retry: true }), answer ? [botNode] : []);
    }
    setBusy(false);
    if (opened) input.focus({ preventScroll: true });
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    if (busy) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    autoGrow();
    send(text);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener('input', autoGrow);

  // ---- open / close
  panel.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !e.defaultPrevented) {
      e.preventDefault();
      e.stopPropagation(); // a dialog underneath must not also close
      close();
    }
  });

  function setOpen(v) {
    v = !!v;
    if (v === opened || destroyed) return;
    opened = v;
    if (v) {
      const a = document.activeElement;
      opener = a instanceof HTMLElement && !panel.contains(a) && a !== document.body ? a : null;
      panel.hidden = false;
      store.set('chatOpen', true);
      renderLane();
      loadModels();
      scrollDown();
      input.focus({ preventScroll: true });
    } else {
      const hadFocus = panel.contains(document.activeElement);
      panel.hidden = true;
      store.set('chatOpen', false);
      if ((hadFocus || document.activeElement === document.body) && opener && opener.isConnected) opener.focus();
      opener = null;
    }
  }
  function open() { setOpen(true); }
  function close() { setOpen(false); }
  function toggle() { setOpen(!opened); }
  function isOpen() { return opened; }

  const unsubs = [
    store.subscribe('chatOpen', v => { if (!!v !== opened) setOpen(!!v); }),
    store.subscribe('lane', renderLane),
    store.subscribe('config', syncModelSelect),
  ];
  // the sheet height follows the on-screen keyboard poorly on some phones; keep the log pinned
  const onResize = debounce(() => { if (opened && nearBottom()) scrollDown(); }, 150);
  window.addEventListener('resize', onResize);

  store.set('chatOpen', false);
  updateHint();
  renderLane();

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (ctl) ctl.abort('destroy');
    modelsReq.abort();
    unsubs.forEach(fn => fn());
    window.removeEventListener('resize', onResize);
    panel.remove();
    store.set('chatOpen', false);
  }

  return { open, close, toggle, isOpen, destroy };
}
