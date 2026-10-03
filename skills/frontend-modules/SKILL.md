---
name: frontend-modules
description: Conventions for the static/ frontend (ES modules, no build step): module layout, store/api/ui helpers, view mount contract, strict CSP rules, markdown sanitising, themes, accessibility, vendored libs and how to verify UI changes. Use for any edit under static/.
---

# Frontend modules

No bundler, no framework: `static/index.html` loads `dompurify.min.js` → `marked.min.js` → `js/main.js`
(`type="module"`). Served by FastAPI with `Cache-Control: no-cache` (ETag revalidation).

## Layout
```
static/index.html          shell (skip link, status bar, tabs, mount points)
static/js/main.js          entry: boot, view switching, chat/lazy loaders
static/js/api.js           apiGet/apiSend -> {ok,data,error,status,stale,aborted}   (never throw)
static/js/store.js         tiny pub/sub: get/set/patch/subscribe + resource "slots" (slot, slotApply, slotLoading)
static/js/data.js          fetchers/polling for portfolio, prices, alerts, lane, config
static/js/sse.js           /stream client (own timestamps for the watchdog, backoff, safe JSON.parse)
static/js/ui.js            toast, openDialog (focus trap, Escape, focus restore), renderState, stamp, usePolling, setMarkdown
static/js/util.js          el(tag, attrs, ...children), fmtEur/fmtPct/fmtTime24, ago, clear
static/js/markdown.js      renderMarkdown(text) -> sanitised HTML (DOMPurify; ESCAPES if a lib is missing)
static/js/charts.js        canvas/SVG charts + sparklines + donut; colours read from CSS variables
static/js/views/           overview, detail, settings, alertcenter (shell-owned);
                           digest, market, aiops (+ _kit, _workers, _report helpers) — each exports mount(root, ctx) -> unmount()
static/css/                tokens.css (variables, light+dark) base shell overview charts detail settings alerts chat views
```
`ctx = {store, api, util, ui, markdown}`. Secondary views use the `v-` CSS class prefix (`views.css`).

## Hard rules
1. **CSP is strict**: `default-src 'self'`, `script-src 'self' + s3.tradingview.com/www.tradingview.com`,
   `style-src 'self'` (no `'unsafe-inline'`), `connect-src 'self'`, `frame-ancestors 'none'`. So: no inline
   `<script>`, no `style="…"` attributes or `<style>` blocks, no `javascript:` URLs. Dynamic sizes/positions go
   through CSSOM (`el.style.width = …`) or CSS custom properties set via `style.setProperty`.
2. **No `innerHTML` with data.** Build DOM with `el()`/`textContent`. Markdown only via `ui.setMarkdown(node, text)`
   (the single `innerHTML` path, sanitised). Links from API data: allow only `https:`/`http:`.
3. **Every panel has loading / error / empty / stale states** (`ui.renderState`, `ui.stamp`). An HTTP error must
   never render as an empty state; a failed refresh keeps old content and marks it stale.
4. **Responses can arrive out of order**: use `AbortController`/request tokens on ticker switch and discard stale
   results. Key list rows by `data-symbol`, not by displayed text.
5. **Polling is visibility-aware** (`ui.usePolling`) and cleared on `unmount()`; SSE `alert` events trigger a
   refresh. `resize` handlers are unconditional and debounced (not gated on reduced-motion).
6. **Accessibility**: real `<button>`s (never click-only `div`s), `aria-pressed`/`aria-current`, dialogs via
   `ui.openDialog` (`role=dialog`, Escape, focus trap + restore), toasts in an `aria-live` region, tables with
   captions/`scope`, `:focus-visible`, colour never the only signal, honour `prefers-reduced-motion`.
7. **Theme**: CSS variables only (`tokens.css`, light + dark, `prefers-color-scheme` default, manual toggle in
   `localStorage` applied early by `theme-boot.js`). Canvas colours are read from variables at draw time.
8. **EUR only**; 24-hour times; money via `fmtEur`. No USD toggle. The benchmark is never a holding row and
   never shows analysis/alert actions. `usLive` is a small labelled secondary line, never plotted in the chart.
9. **TradingView** symbol map comes from the entry's listing venue (`XETR:`, `EURONEXT:`, index → `SP:SPX`);
   failing to load must degrade gracefully.

## Adding a view or panel
1. New file under `static/js/views/` exporting `mount(root, ctx)` that returns `unmount()`; fetch with `ctx.api`,
   render states with `ctx.ui.renderState`, poll with `ctx.ui.usePolling`.
2. Register the tab in `main.js`/`index.html` (keep `role=tablist` semantics); add CSS to the matching file with
   tokens only; bump the `?v=` on every asset line in `index.html`.
3. If it needs new data, add the endpoint in `app/main.py` first (see `skills/ship-and-verify` for the matrix).

## Vendored libs
`static/VENDOR.md` records versions + sha256 (marked 18.0.14, DOMPurify 3.4.16, UMD builds, unmodified).
Update by downloading the npm tarball file, replacing it, updating the table, then smoke-testing
`renderMarkdown` (links get `rel=noopener`, `<script>`/`<img onerror>` stripped).

## Verify
`node --input-type=module --check < file` for each module; then the browser checks in `skills/ship-and-verify`
(zero console errors under the CSP, no overflow at 390 px, screenshots). The integration suite
`tests/integration/test_dashboard.py` asserts DOM structure — update it when you change ids/roles.
