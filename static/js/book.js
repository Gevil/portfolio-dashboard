// Account context ('real' | 'practice', 'live' reserved for Phase 5). Real is the default and the
// fallback whenever storage/hash hold something unrecognised. Persistence: localStorage 'pd.book'
// (mirrors theme-boot.js, which sets data-book on <html> before first paint from the same key);
// `#book=<id>` in the URL hash overrides storage for the current load but is never written back.
import { get, set, subscribe, slot } from './store.js';

export const STORAGE_KEY = 'pd.book';
const PERSISTED = new Set(['real', 'practice']); // ids that may be stored/hash-overridden in Phase 1
const KNOWN = new Set(['real', 'practice', 'live']);

function readStorage() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return PERSISTED.has(v) ? v : 'real';
  } catch (_) { return 'real'; }
}

export function readHashBook() {
  const m = /(?:^|[&#])book=([a-z]+)/.exec(location.hash);
  return m && KNOWN.has(m[1]) ? m[1] : null;
}

function applyAttr(book) {
  const root = document.documentElement;
  if (book === 'real') root.removeAttribute('data-book');
  else root.setAttribute('data-book', book);
}

/** Read the persisted/deep-linked book (and the remembered bucket) into the store at boot. Call once, before data.refreshAll(). */
export function initBook() {
  const book = readHashBook() || readStorage();
  set('book', book);
  set('bucket', readBucketStorage());
  applyAttr(book);
  return book;
}

/** Current book ('real' when nothing has been set yet). */
export function getBook() {
  return get('book') || 'real';
}

/** Switch context. Unknown ids fall back to 'real'. Persists real/practice only (no cookie, no
 * server-side "current book": two open tabs can never mix contexts, per plan §4.3). */
export function setBook(book) {
  const b = KNOWN.has(book) ? book : 'real';
  applyAttr(b);
  try {
    if (b === 'real') localStorage.removeItem(STORAGE_KEY);
    else if (PERSISTED.has(b)) localStorage.setItem(STORAGE_KEY, b);
  } catch (_) { /* storage blocked: the choice still applies for this session */ }
  set('book', b);
  return b;
}

export function subscribeBook(fn) {
  return subscribe('book', fn);
}

/** Add `?book=`/`&book=` to a same-origin relative API url, only when the current book isn't 'real'
 * (real runs today's code path untouched - no key added to the query string). Practice also carries
 * the selected `bucket`. */
export function withBook(url) {
  const b = getBook();
  if (b === 'real') return url;
  const sep = url.indexOf('?') >= 0 ? '&' : '?';
  return url + sep + 'book=' + encodeURIComponent(b) + (b === 'practice' ? '&bucket=' + encodeURIComponent(getBucket()) : '');
}

/** Capabilities of the current /api/portfolio payload (contract §4). A missing `capabilities` key (Real, or
 * nothing loaded yet) means every capability is on; only an explicit `false` turns one off. */
export function capsOf() {
  const d = slot('portfolio').data;
  const c = d && d.capabilities;
  const has = (k) => !c || c[k] !== false;
  return { lots: has('lots'), policy: has('policy'), tax: has('tax'), editPositions: has('editPositions'), alertActions: has('alertActions'), attribution: has('attribution'), risk: has('risk'), analysis: has('analysis') };
}

// ------------------------------------------------------------------ bucket (Practice only)
// Which paper bucket the Practice context shows: 'daily' (default) | 'intraday'. Remembered in
// localStorage 'pd.bucket'. Real never carries it; withBook() adds it to every Practice request.
export const BUCKET_KEY = 'pd.bucket';
export const BUCKETS = ['daily', 'intraday'];
export const BUCKET_LABEL = { daily: 'Daily', intraday: 'Intraday' };

function readBucketStorage() {
  try {
    const v = localStorage.getItem(BUCKET_KEY);
    return BUCKETS.includes(v) ? v : 'daily';
  } catch (_) { return 'daily'; }
}

/** Current bucket id ('daily' when nothing valid is set). */
export function getBucket() {
  const b = get('bucket');
  return BUCKETS.includes(b) ? b : 'daily';
}

/** Switch bucket (unknown ids fall back to 'daily'); persists to localStorage 'pd.bucket'. */
export function setBucket(bucket) {
  const b = BUCKETS.includes(bucket) ? bucket : 'daily';
  try { localStorage.setItem(BUCKET_KEY, b); } catch (_) { /* storage blocked: applies for this session */ }
  set('bucket', b);
  return b;
}

/** Query fragment for Practice requests that do not go through withBook(): 'book=practice&bucket=<id>'. */
export function practiceQuery() {
  return 'book=practice&bucket=' + encodeURIComponent(getBucket());
}
