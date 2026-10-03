// Markdown -> sanitised HTML. Fail closed: if marked or DOMPurify is missing (script
// failed to load) the text is HTML-escaped instead - raw marked output is never returned.
import { escapeHtml } from './util.js';

let hooked = false;

function installHooks(DOMPurify) {
  if (hooked) return;
  hooked = true;
  DOMPurify.addHook('afterSanitizeAttributes', node => {
    if (node.tagName === 'A') {
      const href = node.getAttribute('href') || '';
      if (!/^(https?:|mailto:|#)/i.test(href)) node.removeAttribute('href');
      else if (!href.startsWith('#')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer nofollow');
      }
    }
  });
}

function escapedFallback(text) {
  return '<p>' + escapeHtml(text).replace(/\n/g, '<br>') + '</p>';
}

/** renderMarkdown(text) -> sanitised HTML string (safe for innerHTML). */
export function renderMarkdown(text) {
  if (text == null || text === '') return '';
  const src = String(text);
  const DOMPurify = globalThis.DOMPurify;
  const marked = globalThis.marked;
  if (!DOMPurify || typeof DOMPurify.sanitize !== 'function' ||
      !marked || typeof marked.parse !== 'function') {
    return escapedFallback(src);
  }
  try {
    installHooks(DOMPurify);
    const raw = marked.parse(src, { gfm: true, breaks: true, async: false });
    if (typeof raw !== 'string') return escapedFallback(src);
    return DOMPurify.sanitize(raw, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['style', 'img', 'picture', 'svg', 'math', 'form', 'input', 'button', 'iframe', 'object', 'embed'],
      FORBID_ATTR: ['style', 'srcset'],
    });
  } catch (e) {
    console.error('renderMarkdown failed', e);
    return escapedFallback(src);
  }
}

export const markdown = { renderMarkdown };
