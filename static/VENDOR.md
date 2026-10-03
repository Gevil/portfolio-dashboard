# Vendored browser libraries

Plain `<script>` UMD builds, no build step. Loaded by `index.html` BEFORE the app module
(`dompurify.min.js`, then `marked.min.js`, then `js/main.js`). `js/markdown.js` fails closed
(escapes the text) when either global is missing.

| File | Package | Version | Upstream file | sha256 of the file as shipped |
|---|---|---|---|---|
| `marked.min.js` | marked | 18.0.14 | `lib/marked.umd.js` in https://registry.npmjs.org/marked/-/marked-18.0.14.tgz | `21568877a938d2c4e7d74e27f18e60da96bb73a68809610ca39216e1efebae62` |
| `dompurify.min.js` | dompurify | 3.4.16 | `dist/purify.min.js` in https://registry.npmjs.org/dompurify/-/dompurify-3.4.16.tgz | `2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2` |

Fetched 2026-10-02 with curl from the npm registry (tarball integrity from the registry:
marked `sha512-mBHK6FBHuBAlhgRe88w9F0O1AbwwXJUcQibUbC/QcdTbVGAD7aWza+xt3N6oT/jCZx3/OMeS+8rnuiHZcQ9s7A==`,
dompurify `sha512-sqo+pNp3qRhCIpbgRi1y8Tgk27Bo2Ry7w0dC1NBeNTdZChWjz9Xb/KOoZbRP/R6pQZ80Qw8YhXw13hWWBbMRnQ==`).
Files are unmodified (the trailing `sourceMappingURL` comment is only used by devtools).

Previous versions: marked 12.0.2, DOMPurify 3.2.4 (DOMPurify was never loaded by index.html).

To update: re-download the same two files from the npm tarball of the new version, replace
the files, update this table, and smoke-test `renderMarkdown` (links get `rel=noopener`, `<script>`/`<img>`/event handlers are stripped).
