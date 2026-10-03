/* Report viewer: GET /api/report/{id} -> {content: markdown} | {error}.
   Ids are opaque strings (may contain '/'); markdown only via ctx.markdown. */
import { errText } from './_kit.js';

export function reportUrl(id) {
  return '/api/report/' + String(id).split('/').map(encodeURIComponent).join('/');
}

export function openReport(kit, id, title) {
  kit.dialog(title || 'Report', (body) => loadInto(kit, body, id));
}

function loadInto(kit, body, id) {
  kit.ctx.ui.renderState(body, { loading: 'Loading report…' });
  kit.get(reportUrl(id)).then((res) => {
    if (kit.isDisposed()) return;
    const d = res.data;
    if (!res.ok || !d || typeof d.content !== 'string') {
      kit.ctx.ui.renderState(body, { error: (d && d.error) || errText(res.error) || 'Report unavailable', retry: () => loadInto(kit, body, id) });
      return;
    }
    body.textContent = '';
    const box = kit.h('article', { class: 'md v-md' });
    // setMarkdown = marked -> DOMPurify (fail-closed escape); the shell's single innerHTML path.
    kit.ctx.ui.setMarkdown(box, d.content);
    body.appendChild(box);
  });
}
