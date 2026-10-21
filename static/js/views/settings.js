/* Settings drawer: Watchlist | Positions | Policy | Alert rules | Appearance.
   export function openSettings({tab}) -> dialog handle (ui.openDialog drawer).
   Tab keys: watchlist, positions, policy, rules, appearance.

   Pure logic (validateWatchlist, buildWatchlist, validatePositions / fifoFromLots, validatePolicy,
   collectRules, ...) is exported separately from the DOM code so it can be unit-tested in Node.
   Everything stateful lives in the openSettings closure: there is no module-level DOM state.
   Server contracts: app/api/registry.py normalize_entry, app/api/config_edit.py (lots,
   investmentPolicy, lookThrough, exposureCurrency, performanceBenchmark, taxResidency),
   app/api/rules.py (alert rules v3). */
import { el, clear, fmtEur, fmtDateTime24 } from '../util.js';
import { apiGet, apiSend, latestRequest } from '../api.js';
import * as store from '../store.js';
import { openDialog, confirmDialog, renderState, toast, stamp } from '../ui.js';
import { getBook, subscribeBook } from '../book.js';
import { refreshConfig, refreshWatchlist, refreshAll } from '../data.js';

/* ================================================================== helpers */

const isObj = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const str = v => (v == null ? '' : String(v));
const trim = v => str(v).trim();
const upper = v => trim(v).toUpperCase();

/** Id as the server stores it (registry.normalize_entry: stripped + upper-cased). */
export function normId(v) {
  return upper(v);
}

/** Lenient number parser for form strings: '' / garbage -> null; accepts "1,5" and "1'234.5". */
export function parseNumber(s) {
  if (typeof s === 'number') return Number.isFinite(s) ? s : null;
  const t = str(s).replace(/['\u2019\s]/g, '');
  if (!t) return null;
  const norm = /^[+-]?\d+,\d+$/.test(t) ? t.replace(',', '.') : t;
  if (!/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(norm)) return null;
  const n = Number(norm);
  return Number.isFinite(n) ? n : null;
}

/* ============================================================ watchlist model */

export const KINDS = ['equity', 'etf', 'index'];
export const ROLES = ['holding', 'benchmark'];
export const CURRENCIES = ['EUR', 'USD'];
/** MIC -> region (mirrors registry.VENUES). */
export const VENUES = { XAMS: 'EU', XPAR: 'EU', XBRU: 'EU', XETR: 'EU', XMIL: 'EU', XNAS: 'US', XNYS: 'US' };
const KIND_FLAGS = { equity: [true, true], etf: [true, false], index: [false, false] }; // [alertable, analyzeable]
const ID_MAX = 24;
const ID_RE = /^[\p{L}\p{N}._-]+$/u;

let draftSeq = 0; // plain key generator, no DOM

/** A watchlist entry as an editable draft. `orig` = the full stored entry (unknown keys survive). */
export function blankDraft(over = {}) {
  return {
    key: ++draftSeq, orig: null, id: '', label: '', kind: 'equity', role: 'holding',
    venue: '', currency: 'EUR', listingSymbol: '', yahoo: '', twelvedata: '', finnhub: '', ...over,
  };
}

export function draftFromEntry(entry) {
  const e = isObj(entry) ? entry : { id: str(entry) };
  const kind = KINDS.includes(e.kind) ? e.kind : 'equity';
  const role = ROLES.includes(e.role) ? e.role : (kind === 'index' ? 'benchmark' : 'holding');
  const prov = isObj(e.providers) ? e.providers : {};
  const lst = isObj(e.listing) ? e.listing : {};
  const yahoo = trim(prov.yahoo);
  const lsym = trim(lst.symbol);
  const ccy = [upper(lst.currency), upper(e.quoteCurrency)].find(c => CURRENCIES.includes(c)) || 'EUR';
  return blankDraft({
    orig: e, id: normId(e.id || e.symbol), label: trim(e.label), kind, role,
    venue: upper(lst.venue), currency: ccy,
    listingSymbol: lsym && lsym !== yahoo ? lsym : '', yahoo,
    twelvedata: trim(prov.twelvedata), finnhub: trim(prov.finnhub),
  });
}

/** Does this draft get per-ticker alert rule blocks? (no benchmark / index / alertable:false) */
export function isAlertable(d) {
  if (d.role === 'benchmark' || d.kind === 'index' || !normId(d.id)) return false;
  const o = d.orig;
  if (o && o.kind === d.kind && typeof o.alertable === 'boolean') return o.alertable;
  return (KIND_FLAGS[d.kind] || KIND_FLAGS.equity)[0];
}

/**
 * Mirror of registry.normalize_entry / validate_entries for the fields the form edits.
 * Returns [{key, field, message}] (key = draft.key, or null for list-level problems).
 */
export function validateWatchlist(drafts) {
  const errors = [];
  const add = (d, field, message) => errors.push({ key: d ? d.key : null, field, message });
  if (!Array.isArray(drafts) || !drafts.length) {
    add(null, null, 'The watchlist cannot be empty: keep or add at least one entry.');
    return errors;
  }
  const seen = new Set();
  let benchmark = null;
  for (const d of drafts) {
    const id = normId(d.id);
    if (!id) add(d, 'id', 'An entry needs an id.');
    else if (id.length > ID_MAX || !ID_RE.test(id)) add(d, 'id', `Invalid id "${id}" (letters, digits, . - _ only, max ${ID_MAX}).`);
    else if (seen.has(id)) add(d, 'id', `Duplicate id ${id}.`);
    else seen.add(id);

    if (!KINDS.includes(d.kind)) add(d, 'kind', `Kind must be one of ${KINDS.join(', ')}.`);
    if (!ROLES.includes(d.role)) add(d, 'role', `Role must be one of ${ROLES.join(', ')}.`);

    const holding = d.role === 'holding';
    const venue = upper(d.venue);
    if (holding || venue) {
      if (!venue) add(d, 'venue', 'A holding needs a listing: pick its venue.');
      else if (!Object.hasOwn(VENUES, venue)) add(d, 'venue', `Unknown venue ${venue} (known: ${Object.keys(VENUES).join(', ')}).`);
      if (!CURRENCIES.includes(upper(d.currency))) add(d, 'currency', `Listing currency must be one of ${CURRENCIES.join(', ')}.`);
      if (!trim(d.listingSymbol) && !trim(d.yahoo)) add(d, 'yahoo', 'The listing needs a symbol: enter the Yahoo symbol.');
    }
    if (d.role === 'benchmark') {
      if (benchmark !== null) add(d, 'role', `Only one benchmark is allowed (${benchmark || 'another entry'} already is one).`);
      else benchmark = id;
    }
  }
  return errors;
}

/**
 * Drafts -> PUT /api/config watchlist array. The ORIGINAL stored entry is the base so unknown
 * keys survive; explicit alertable/analyzeable flags are re-derived only when the kind changed.
 */
export function buildWatchlist(drafts) {
  return drafts.map(d => {
    const id = normId(d.id);
    const base = isObj(d.orig) ? d.orig : {};
    const out = { ...base, id, label: trim(d.label) || id, kind: d.kind, role: d.role };
    const yahoo = trim(d.yahoo);
    out.providers = {
      ...(isObj(base.providers) ? base.providers : {}),
      yahoo: yahoo || null, twelvedata: trim(d.twelvedata) || null, finnhub: trim(d.finnhub) || null,
    };
    const venue = upper(d.venue);
    const ccy = upper(d.currency);
    if (venue) {
      out.listing = { ...(isObj(base.listing) ? base.listing : {}), symbol: trim(d.listingSymbol) || yahoo, venue, currency: ccy };
    } else {
      out.listing = null;
    }
    if (d.role === 'holding' && venue) out.quoteCurrency = ccy; // held listing's currency IS the quote currency
    else if (base.quoteCurrency) out.quoteCurrency = base.quoteCurrency;
    else if (venue) out.quoteCurrency = ccy;
    else delete out.quoteCurrency;
    if (base.kind !== undefined && base.kind !== d.kind) {
      const f = KIND_FLAGS[d.kind] || KIND_FLAGS.equity;
      out.alertable = f[0];
      out.analyzeable = f[1];
    }
    return out;
  });
}

/* ============================================================== positions */

const SHARE_EPS = 1e-9;
export const LOT_SIDES = ['buy', 'sell'];

/** Local calendar date, YYYY-MM-DD (a lot may not be dated in the future). */
export function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** A real calendar date in YYYY-MM-DD form (2026-02-30 is not one). */
export function isIsoDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str(s));
  if (!m) return false;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return d.getUTCFullYear() === Number(m[1]) && d.getUTCMonth() === Number(m[2]) - 1 && d.getUTCDate() === Number(m[3]);
}

let lotSeq = 0;
/** One editable lot row; values are strings, like the inputs that edit them. */
export function newLot(over = {}) {
  return { key: ++lotSeq, date: '', shares: '', priceEur: '', feesEur: '', side: 'buy', ...over };
}

/** Trade-date order; on one day the buys count before the sells; ties keep entry order. */
function cmpLot(a, b) {
  if (a.date !== b.date) return a.date < b.date ? -1 : 1;
  if (a.side !== b.side) return a.side === 'buy' ? -1 : 1;
  return (a._i || 0) - (b._i || 0);
}

/**
 * FIFO over numeric lots [{date, shares, priceEur, feesEur, side}] -> {shares, cost, oversold}.
 * A buy's fees are spread over its shares (unit cost = (shares x price + fees) / shares); a sell consumes the
 * oldest open shares. cost = FIFO cost of the shares still held. Sell fees do not enter the cost basis.
 * Mirrors config_edit.py (the server stays authoritative; this is the live preview).
 */
export function fifoFromLots(lots) {
  const open = [];
  let oversold = 0;
  for (const l of [...lots].sort(cmpLot)) {
    if (l.side === 'buy') {
      open.push({ shares: l.shares, unit: (l.shares * l.priceEur + (l.feesEur || 0)) / l.shares });
      continue;
    }
    let need = l.shares;
    while (need > SHARE_EPS && open.length) {
      const o = open[0];
      const take = Math.min(o.shares, need);
      o.shares -= take;
      need -= take;
      if (o.shares <= SHARE_EPS) open.shift();
    }
    if (need > SHARE_EPS) oversold += need;
  }
  const shares = open.reduce((s, o) => s + o.shares, 0);
  const cost = open.reduce((s, o) => s + o.shares * o.unit, 0);
  return { shares: Math.round(shares * 1e8) / 1e8, cost, oversold };
}

/** Lenient preview of editable rows: rows that do not parse yet are skipped (and counted). -> {shares, cost, oversold, skipped} */
export function deriveFromLots(rows) {
  const ok = [];
  let skipped = 0;
  (rows || []).forEach((r, i) => {
    const shares = parseNumber(r.shares);
    const price = parseNumber(r.priceEur);
    const fees = trim(r.feesEur) === '' ? 0 : parseNumber(r.feesEur);
    if (isIsoDate(trim(r.date)) && shares !== null && shares > 0 && price !== null && price > 0 && fees !== null && fees >= 0 && LOT_SIDES.includes(r.side)) {
      ok.push({ date: trim(r.date), shares, priceEur: price, feesEur: fees, side: r.side, _i: i });
    } else {
      skipped++;
    }
  });
  return { ...fifoFromLots(ok), skipped };
}

/**
 * Mirror of the server's lot rules: ISO date not in the future, shares > 0, priceEur > 0, feesEur >= 0 (blank = 0),
 * side buy|sell, a sell may not exceed the shares held on its date. -> {errors:[{index, field, message}], lots (sorted by date)}
 */
export function validateLots(rows, today = todayIso()) {
  const errors = [];
  const parsed = [];
  (rows || []).forEach((r, i) => {
    const before = errors.length;
    const bad = (field, message) => errors.push({ index: i, field, message });
    const date = trim(r.date);
    if (!isIsoDate(date)) bad('date', 'Date must be a valid date (YYYY-MM-DD).');
    else if (date > today) bad('date', 'The date cannot be in the future.');
    const shares = parseNumber(r.shares);
    if (shares === null || shares <= 0) bad('shares', 'Shares must be greater than 0.');
    const price = parseNumber(r.priceEur);
    if (price === null || price <= 0) bad('priceEur', 'Price per share (EUR) must be greater than 0.');
    const fees = trim(r.feesEur) === '' ? 0 : parseNumber(r.feesEur);
    if (fees === null || fees < 0) bad('feesEur', 'Fees must be 0 EUR or more.');
    if (!LOT_SIDES.includes(r.side)) bad('side', 'Side must be buy or sell.');
    if (errors.length === before) parsed.push({ date, shares, priceEur: price, feesEur: fees, side: r.side, _i: i });
  });
  const sorted = [...parsed].sort(cmpLot);
  if (!errors.length) {
    let held = 0;
    for (const l of sorted) {
      if (l.side === 'buy') { held += l.shares; continue; }
      if (l.shares > held + SHARE_EPS) {
        errors.push({ index: l._i, field: 'shares', message: `Sells ${Math.round(l.shares * 1e6) / 1e6} shares on ${l.date} but only ${Math.round(held * 1e6) / 1e6} are held.` });
      } else {
        held -= l.shares;
      }
    }
  }
  return { errors, lots: sorted.map(({ _i, ...l }) => l) };
}

/**
 * positions: [{id, shares:string, invested:string, unknown:boolean, lots:[row], hadLots:boolean}]; holdingIds: Set|Array of ids.
 * -> {errors:[{id, field:'shares'|'invested'|'id'|'lots'|'lot:<i>:<field>', message}], portfolio}
 * portfolio[id] is {lots:[...]} when the position has lots (the server derives shares + investedAmount: they are not sent),
 * {shares, investedAmount|null, lots: []} when its stored lots were removed, else {shares, investedAmount|null}.
 */
export function validatePositions(positions, holdingIds, today = todayIso()) {
  const ids = holdingIds instanceof Set ? holdingIds : new Set((holdingIds || []).map(normId));
  const errors = [];
  const portfolio = {};
  for (const p of positions || []) {
    const id = normId(p.id);
    if (!ids.has(id)) {
      errors.push({ id, field: 'id', message: `${id} is not a holding: remove this position or make the entry a holding again.` });
      continue;
    }
    const before = errors.length;
    if (Array.isArray(p.lots) && p.lots.length) {
      const lv = validateLots(p.lots, today);
      lv.errors.forEach(e => errors.push({ id, field: `lot:${e.index}:${e.field}`, message: e.message }));
      if (errors.length === before) portfolio[id] = { lots: lv.lots };
      continue;
    }
    const shares = parseNumber(p.shares);
    if (shares === null || shares <= 0) errors.push({ id, field: 'shares', message: 'Shares must be a number greater than 0.' });
    let invested = null;
    if (!p.unknown) {
      invested = parseNumber(p.invested);
      if (invested === null || invested < 0) {
        errors.push({ id, field: 'invested', message: 'Cost basis must be a number of at least 0 EUR - or tick "Cost unknown".' });
      }
    }
    if (errors.length === before) {
      portfolio[id] = { shares, investedAmount: invested };
      if (p.hadLots) portfolio[id].lots = []; // the stored lots are dropped explicitly
    }
  }
  return { errors, portfolio };
}

/** Average cost per share in EUR, or null (plain positions). */
export function avgCost(p) {
  if (!p || p.unknown) return null;
  const s = parseNumber(p.shares);
  const c = parseNumber(p.invested);
  return s !== null && s > 0 && c !== null && c >= 0 ? c / s : null;
}

/* ============================================================== alert rules */

const AR_DIRECTIONS = ['both', 'up', 'down'];
const AR_CROSS_DIRECTIONS = ['bullish_cross', 'bearish_cross'];
const AR_LEVEL_CONDITIONS = ['ABOVE', 'BELOW'];
const AR_RSI_CONDITIONS = ['above', 'below'];
const AR_LIGHT_STATES = ['red', 'yellow', 'green'];
export const AR_KINDS = ['pct-move', 'absolute', 'ema_cross', 'rsi_threshold', 'volume-spike',
  'earnings-day', 'short-ratio-spike', 'market-light-drop'];
/** Mirrors rules.py _NUM_SPECS: the server rejects a value outside a field's bounds. */
export const AR_LIMITS = {
  thresholdPct: [0.1, 50], hotPct: [0.1, 50], cooldownMin: [1, 1440], targetPrice: [0.0001, 1000000000],
  factor: [1.1, 100], sigma: [0.5, 10], threshold: [1, 99], period: [2, 200], fast: [2, 200], slow: [3, 400],
  leadDays: [0, 45], zAlert: [0.5, 10], zHot: [0.5, 20], rearmZ: [0, 10], minPct: [0, 50], gapWindowMin: [1, 240],
};
const AR_INT_KEYS = ['cooldownMin', 'leadDays', 'period', 'fast', 'slow', 'gapWindowMin'];
export const AR_MODES = ['sigma', 'pct'];
/** rules.py defaults: a blank field in an old (v1/v2) block reads as these. */
export const AR_DEFAULTS = { mode: 'sigma', zAlert: 2, zHot: 3, rearmZ: 1, minPct: 1, gapWindowMin: 30, thresholdPct: 3, hotPct: 5, cooldownMin: 120, direction: 'both' };
const AR_DEFAULT_NUM = [['zAlert', 'zAlert (\u03c3)'], ['zHot', 'zHot (\u03c3)'], ['rearmZ', 'rearmZ (\u03c3)'], ['minPct', 'Min move %'], ['gapWindowMin', 'Gap window (min)'], ['cooldownMin', 'Cooldown (min)']];
const AR_DEFAULT_PCT = [['thresholdPct', 'Threshold %'], ['hotPct', 'Hot %']]; // used in pct mode only
const AR_COOL = { key: 'cooldownMin', label: 'Cooldown (min)' };
/* kind -> fields. `options` renders a select (inherit: blank = inherit the default), `bool` a
   checkbox, `date` a date input, anything else a number bounded by AR_LIMITS[key]. */
export const AR_KIND_FIELDS = {
  'pct-move': [
    { key: 'mode', label: 'Mode', options: AR_MODES, inherit: true },
    { key: 'zAlert', label: 'zAlert (\u03c3)' },
    { key: 'zHot', label: 'zHot (\u03c3)' },
    { key: 'rearmZ', label: 'rearmZ (\u03c3)' },
    { key: 'minPct', label: 'Min move %' },
    { key: 'gapWindowMin', label: 'Gap window (min)' },
    { key: 'thresholdPct', label: 'Threshold %', pctOnly: true },
    { key: 'hotPct', label: 'Hot %', pctOnly: true },
    { key: 'direction', label: 'Direction', options: AR_DIRECTIONS, inherit: true },
    AR_COOL,
  ],
  absolute: [
    { key: 'condition', label: 'Condition', options: AR_LEVEL_CONDITIONS },
    { key: 'targetPrice', label: 'Target price' },
    { key: 'oneShot', label: 'One-shot', bool: true },
    { key: 'expiresAt', label: 'Expires', date: true },
    AR_COOL,
  ],
  ema_cross: [
    { key: 'fast', label: 'Fast EMA' },
    { key: 'slow', label: 'Slow EMA' },
    { key: 'direction', label: 'Cross', options: AR_CROSS_DIRECTIONS },
    AR_COOL,
  ],
  rsi_threshold: [
    { key: 'period', label: 'Period' },
    { key: 'threshold', label: 'RSI level' },
    { key: 'condition', label: 'Condition', options: AR_RSI_CONDITIONS },
    AR_COOL,
  ],
  'volume-spike': [{ key: 'factor', label: 'Volume \u00d7' }, AR_COOL],
  'earnings-day': [{ key: 'leadDays', label: 'Lead days' }, AR_COOL],
  'short-ratio-spike': [{ key: 'sigma', label: 'Sigma' }, AR_COOL],
  'market-light-drop': [
    { key: 'from', label: 'From', options: AR_LIGHT_STATES },
    { key: 'to', label: 'To', options: AR_LIGHT_STATES },
    AR_COOL,
  ],
};
/** Fields rules.py requires per kind: a missing one is a 400, so the editor refuses to submit it. */
const AR_REQUIRED = {
  absolute: ['condition', 'targetPrice'], ema_cross: ['fast', 'slow', 'direction'],
  rsi_threshold: ['period', 'threshold', 'condition'], 'volume-spike': ['factor'],
  'earnings-day': ['leadDays'], 'short-ratio-spike': ['sigma'], 'market-light-drop': ['from', 'to'],
};

function arParse(raw, key) {
  const lim = AR_LIMITS[key];
  const v = parseNumber(raw);
  if (!lim || v === null || v < lim[0] || v > lim[1]) return null;
  return AR_INT_KEYS.includes(key) ? Math.round(v) : v;
}
const arRange = key => `${AR_LIMITS[key][0]} and ${AR_LIMITS[key][1]}`;

/** Editable string/bool values for a rule of `kind`, seeded from `from` (a stored rule). */
export function ruleValues(kind, from = {}) {
  const v = {};
  for (const f of AR_KIND_FIELDS[kind] || []) {
    const cur = from[f.key];
    if (f.bool) v[f.key] = !!cur;
    else if (f.options) v[f.key] = cur != null && cur !== '' ? String(cur) : (f.inherit ? '' : f.options[0]);
    else v[f.key] = cur == null ? '' : String(cur);
  }
  return v;
}

function makeRule(kind, from) {
  const k = kind || 'pct-move';
  return { kind: k, values: ruleValues(k, from || {}) };
}

function makeBlock(id, label, rules, stored) {
  return { id, label: label || id, stored: !!stored, enabled: rules.length > 0, rules };
}

/**
 * GET /api/alert-rules block + the alertable entries [{id,label}] -> editable model
 * {default:{mode, zAlert, zHot, rearmZ, minPct, gapWindowMin, thresholdPct, hotPct, cooldownMin, direction},
 *  tickers:[{id,label,stored,enabled,rules:[{kind,values}]}], sigma:{ID:{sigmaPct, asOf}}}.
 * Stored tickers that are not in `entries` are kept (so saving never silently deletes a stored rule).
 */
export function rulesModelFromBlock(block, entries) {
  const base = isObj(block) && isObj(block.default) ? block.default : {};
  const per = isObj(block) && isObj(block.perTicker) ? block.perTicker : {};
  const pick = k => str(base[k] != null ? base[k] : AR_DEFAULTS[k]);
  const def = {
    mode: AR_MODES.includes(base.mode) ? base.mode : 'sigma',
    zAlert: pick('zAlert'), zHot: pick('zHot'), rearmZ: pick('rearmZ'), minPct: pick('minPct'), gapWindowMin: pick('gapWindowMin'),
    thresholdPct: pick('thresholdPct'), hotPct: pick('hotPct'), cooldownMin: pick('cooldownMin'),
    direction: AR_DIRECTIONS.includes(base.direction) ? base.direction : 'both',
  };
  const sigma = {};
  if (isObj(block) && isObj(block.sigma)) for (const [k, v] of Object.entries(block.sigma)) sigma[normId(k)] = v;
  const stored = new Map();
  for (const [k, v] of Object.entries(per)) stored.set(normId(k), v);
  const rulesOf = id => {
    if (!stored.has(id)) return [];
    const s = stored.get(id);
    return (Array.isArray(s) ? s : [s]).filter(isObj).map(r => makeRule(str(r.kind) || 'pct-move', r));
  };
  const tickers = [];
  const seen = new Set();
  for (const e of entries || []) {
    const id = normId(e.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    tickers.push(makeBlock(id, e.label, rulesOf(id), stored.has(id)));
  }
  for (const id of stored.keys()) {
    if (!id || seen.has(id)) continue;
    seen.add(id);
    tickers.push(makeBlock(id, id, rulesOf(id), true));
  }
  return { default: def, tickers, sigma };
}

/** Re-align the model's ticker blocks with the current alertable entries (in place). */
export function syncRuleBlocks(model, entries) {
  const want = new Map();
  for (const e of entries || []) {
    const id = normId(e.id);
    if (id && !want.has(id)) want.set(id, e.label || id);
  }
  const byId = new Map(model.tickers.map(b => [b.id, b]));
  const next = [];
  for (const [id, label] of want) {
    const b = byId.get(id) || makeBlock(id, label, [], false);
    b.label = label;
    next.push(b);
  }
  for (const b of model.tickers) if (!want.has(b.id) && b.stored) next.push(b); // kept: stored rules, ticker no longer alertable
  model.tickers = next;
  return model;
}

/** rules.py coherence of the sigma fields: zHot >= zAlert, rearmZ < zAlert. v: numbers (missing = not checked). -> [{key, message}] */
export function sigmaCoherence(v) {
  const out = [];
  const has = k => typeof v[k] === 'number';
  if (has('zHot') && has('zAlert') && v.zHot < v.zAlert) out.push({ key: 'zHot', message: 'zHot must not be below zAlert' });
  if (has('rearmZ') && has('zAlert') && v.rearmZ >= v.zAlert) out.push({ key: 'rearmZ', message: 'rearmZ must be below zAlert' });
  return out;
}

/**
 * model -> {payload:{version:3, default, perTicker}, errors:[{ticker|null, index, key, message}]}.
 * Same limits and messages as rules.py. thresholdPct / hotPct are required only in pct mode; the coherence rules
 * are checked on the default block and on every pct-move rule merged over it. Every problem is reported, not only the first.
 */
export function collectRules(model) {
  const payload = { version: 3, default: {}, perTicker: {} };
  const errors = [];
  const def = (model && model.default) || {};
  const mode = AR_MODES.includes(def.mode) ? def.mode : 'sigma';
  payload.default.mode = mode;
  const dnums = {};
  for (const [key] of AR_DEFAULT_NUM) {
    const v = arParse(def[key], key);
    if (v === null) errors.push({ ticker: null, index: -1, key, message: `Default: ${key} must be between ${arRange(key)}` });
    else { payload.default[key] = v; dnums[key] = v; }
  }
  for (const [key] of AR_DEFAULT_PCT) {
    const raw = trim(def[key]);
    if (!raw && mode !== 'pct') continue; // sigma mode: the fixed thresholds are unused
    const v = arParse(raw, key);
    if (v === null) errors.push({ ticker: null, index: -1, key, message: `Default: ${key} must be between ${arRange(key)}` });
    else payload.default[key] = v;
  }
  payload.default.direction = AR_DIRECTIONS.includes(def.direction) ? def.direction : 'both';
  if (!errors.length) {
    for (const c of sigmaCoherence(dnums)) errors.push({ ticker: null, index: -1, key: c.key, message: `Default: ${c.message}` });
  }

  for (const block of (model && model.tickers) || []) {
    if (!block.enabled) continue; // unticked: the whole list is dropped and the ticker inherits the default
    const list = [];
    block.rules.forEach((r, index) => {
      const rule = collectRule(block.id, r, index, errors, dnums);
      if (rule) list.push(rule);
    });
    if (list.length) payload.perTicker[block.id] = list;
  }
  return { payload, errors };
}

function collectRule(ticker, r, index, errors, defaults = {}) {
  const kind = r.kind;
  const values = r.values || {};
  const required = AR_REQUIRED[kind] || [];
  const rule = { kind };
  const before = errors.length;
  const fail = (key, message) => errors.push({ ticker, index, key, message: `${ticker} ${kind}: ${message}` });
  for (const f of AR_KIND_FIELDS[kind] || []) {
    const v = values[f.key];
    if (f.options) {
      const val = str(v);
      if (!val) { if (required.includes(f.key)) fail(f.key, `${f.key} must be one of ${f.options.join('/')}`); continue; }
      if (!f.options.includes(val)) { fail(f.key, `${f.key} must be one of ${f.options.join('/')}`); continue; }
      rule[f.key] = val;
      continue;
    }
    if (f.bool) { if (v === true) rule[f.key] = true; continue; }
    if (f.date) {
      const d = trim(v);
      if (d && !/^\d{4}-\d{2}-\d{2}$/.test(d)) fail(f.key, `${f.key} must be a date (YYYY-MM-DD)`);
      else if (d) rule[f.key] = d;
      continue;
    }
    const raw = trim(v);
    if (!raw) { if (required.includes(f.key)) fail(f.key, `${f.key} is required`); continue; } // blank = inherit the default
    const n = arParse(raw, f.key);
    if (n === null) { fail(f.key, `${f.key} must be between ${arRange(f.key)}`); continue; }
    rule[f.key] = n;
  }
  if (errors.length === before) {
    if (kind === 'ema_cross' && rule.fast != null && rule.slow != null && rule.slow <= rule.fast) {
      errors.push({ ticker, index, key: 'slow', message: `${ticker}: slow must be greater than fast` });
    }
    if (kind === 'market-light-drop' && rule.from && rule.to &&
        AR_LIGHT_STATES.indexOf(rule.to) >= AR_LIGHT_STATES.indexOf(rule.from)) {
      errors.push({ ticker, index, key: 'to', message: `${ticker} market-light-drop: "to" must be a drop from ${rule.from}` });
    }
    if (kind === 'pct-move' && ['zAlert', 'zHot', 'rearmZ'].some(key => rule[key] != null)) {
      const merged = { ...defaults, ...rule }; // a blank field inherits the default block
      for (const c of sigmaCoherence(merged)) errors.push({ ticker, index, key: c.key, message: `${ticker} ${kind}: ${c.message}` });
    }
  }
  return errors.length === before ? rule : null;
}

/** Stable string for dirty checks (ignores empty never-stored blocks created by syncing). */
export function rulesFingerprint(model) {
  if (!model) return '';
  return JSON.stringify([model.default, model.tickers
    .filter(b => b.stored || b.enabled || b.rules.length)
    .map(b => [b.id, b.enabled, b.rules.map(r => [r.kind, r.values])])]);
}

/* ================================================================== policy */

export const CCYS = ['EUR', 'USD', 'GBP', 'CHF', 'JPY'];
let polSeq = 0;
export function newLookRow(over = {}) { return { key: ++polSeq, id: '', pct: '', ...over }; }
export function newLookFund(over = {}) { return { key: ++polSeq, fund: '', asOf: '', source: '', rows: [newLookRow()], ...over }; }

/** Key-order independent copy, so two configs compare equal whatever order the server stored them in. */
function canon(v) {
  if (Array.isArray(v)) return v.map(canon);
  if (isObj(v)) return Object.fromEntries(Object.keys(v).sort().map(k => [k, canon(v[k])]));
  return v;
}
const same = (a, b) => JSON.stringify(canon(a)) === JSON.stringify(canon(b));

/**
 * GET /api/config -> editable policy model (strings for inputs). `orig` keeps the stored values so unknown keys survive
 * and validatePolicy sends only what changed.
 */
export function polFromConfig(cfg) {
  const c = isObj(cfg) ? cfg : {};
  const ip = isObj(c.investmentPolicy) ? c.investmentPolicy : null;
  const band = ip && isObj(ip.band) ? ip.band : {};
  const text = v => (v == null ? '' : String(v));
  const targets = new Map();
  if (ip && isObj(ip.targets)) for (const [k, v] of Object.entries(ip.targets)) targets.set(normId(k), text(v));
  const exp = new Map();
  if (isObj(c.exposureCurrency)) for (const [k, v] of Object.entries(c.exposureCurrency)) exp.set(normId(k), upper(v));
  const look = [];
  if (isObj(c.lookThrough)) {
    for (const [fund, v] of Object.entries(c.lookThrough)) {
      const o = isObj(v) ? v : {};
      const w = isObj(o.weights) ? o.weights : {};
      look.push(newLookFund({
        fund: normId(fund), asOf: text(o.asOf), source: text(o.source),
        rows: Object.entries(w).map(([id, pct]) => newLookRow({ id: normId(id), pct: text(pct) })),
      }));
    }
  }
  return {
    orig: {
      policy: ip, bench: c.performanceBenchmark == null ? null : String(c.performanceBenchmark),
      exp: isObj(c.exposureCurrency) ? c.exposureCurrency : null, look: isObj(c.lookThrough) ? c.lookThrough : null,
      tax: c.taxResidency == null || c.taxResidency === '' ? null : String(c.taxResidency),
    },
    targets, maxSingle: ip ? text(ip.maxSingleStockPct) : '',
    absPct: band.absPct == null ? '5' : String(band.absPct), relPct: band.relPct == null ? '25' : String(band.relPct),
    contrib: ip ? text(ip.monthlyContributionEur) : '', trend: !!(ip && ip.trendFilter),
    bench: text(c.performanceBenchmark), exp, look,
    tax: c.taxResidency == null || c.taxResidency === '' ? 'none' : String(c.taxResidency),
  };
}

/**
 * Validate the policy model like config_edit.py and build only the CHANGED PUT /api/config keys.
 * ctx: {holdings:[{id, currency}], watchIds:[id], today}. -> {errors:[{key, message}], body}
 * body keys: investmentPolicy (null removes it), performanceBenchmark (null removes), exposureCurrency, lookThrough (null removes), taxResidency (null = none).
 * Targets must sum to 100 +- 0.5; error keys are pol:<field> for the UI.
 */
export function validatePolicy(pol, ctx) {
  const errors = [];
  const body = {};
  const err = (key, message) => errors.push({ key, message });
  const holdings = (ctx && ctx.holdings) || [];
  const hset = new Set(holdings.map(x => x.id));
  const watch = new Set((ctx && ctx.watchIds) || []);
  const orig = pol.orig || {};

  /* ---- investmentPolicy ---- */
  const polStart = errors.length;
  const targets = {};
  let sum = 0;
  for (const [id, raw] of pol.targets) {
    if (trim(raw) === '') continue;
    const v = parseNumber(raw);
    if (v === null || v < 0 || v > 100) { err(`pol:target:${id}`, 'Target must be a number from 0 to 100.'); continue; }
    if (!hset.has(id)) { err(`pol:target:${id}`, `${id} is not a holding: clear its target.`); continue; }
    targets[id] = v;
    sum += v;
  }
  const have = Object.keys(targets).length > 0;
  /** blank -> null; invalid -> undefined (error recorded); else the number */
  const optNum = (raw, key, label, lo, hi, loOpen) => {
    if (trim(raw) === '') return null;
    const v = parseNumber(raw);
    if (v === null || v < lo || v > hi || (loOpen && v <= lo)) {
      err(key, `${label} must be a number ${loOpen ? 'above' : 'from'} ${lo} to ${hi}.`);
      return undefined;
    }
    return v;
  };
  const maxS = optNum(pol.maxSingle, 'pol:max', 'Max single stock %', 0, 100, true);
  const abs = trim(pol.absPct) === '' ? 5 : optNum(pol.absPct, 'pol:abs', 'Band absolute points', 0, 100);
  const rel = trim(pol.relPct) === '' ? 25 : optNum(pol.relPct, 'pol:rel', 'Band relative %', 0, 100);
  const contrib = optNum(pol.contrib, 'pol:contrib', 'Monthly contribution', 0, 10000000);
  const touched = have || typeof maxS === 'number' || typeof contrib === 'number' || !!pol.trend || abs !== 5 || rel !== 25;
  if (touched && !have) err('pol:targets', 'Set a target for your holdings first: the policy needs targets that add up to 100%.');
  else if (have && Math.abs(sum - 100) > 0.5) err('pol:targets', `Targets add up to ${Math.round(sum * 100) / 100}%; they must add up to 100% (\u00b10.5).`);
  if (errors.length === polStart) {
    if (!touched) {
      if (orig.policy) body.investmentPolicy = null;
    } else {
      const base = isObj(orig.policy) ? orig.policy : {};
      const next = { ...base, targets, band: { ...(isObj(base.band) ? base.band : {}), absPct: abs, relPct: rel }, trendFilter: !!pol.trend };
      if (typeof maxS === 'number') next.maxSingleStockPct = maxS; else delete next.maxSingleStockPct;
      if (typeof contrib === 'number') next.monthlyContributionEur = contrib; else delete next.monthlyContributionEur;
      if (!same(next, orig.policy)) body.investmentPolicy = next;
    }
  }

  /* ---- performanceBenchmark ---- */
  const bench = normId(pol.bench);
  if (bench && !watch.has(bench) && bench !== normId(orig.bench)) err('pol:bench', `${bench} is not on the watchlist.`);
  else if ((bench || null) !== (orig.bench == null ? null : normId(orig.bench))) body.performanceBenchmark = bench || null;

  /* ---- exposureCurrency: only holdings whose choice differs from their listing currency (or that were stored) ---- */
  const origExp = isObj(orig.exp) ? orig.exp : {};
  const exp = { ...origExp };
  const expStart = errors.length;
  for (const h of holdings) {
    const v = upper(pol.exp.get(h.id) || h.currency);
    if (!CCYS.includes(v) && v !== upper(origExp[h.id])) { err(`pol:exp:${h.id}`, `Exposure currency must be one of ${CCYS.join(', ')}.`); continue; }
    if (v !== h.currency || Object.hasOwn(origExp, h.id)) exp[h.id] = v;
  }
  if (errors.length === expStart && !same(exp, origExp)) body.exposureCurrency = Object.keys(exp).length ? exp : null;

  /* ---- lookThrough ---- */
  const origLook = isObj(orig.look) ? orig.look : {};
  const look = {};
  const seen = new Set();
  const lookStart = errors.length;
  for (const f of pol.look) {
    const k = s => `pol:look:${f.key}:${s}`;
    const fund = normId(f.fund);
    if (!fund) err(k('fund'), 'Pick the fund.');
    else if (!watch.has(fund) && !Object.hasOwn(origLook, fund)) err(k('fund'), `${fund} is not on the watchlist.`);
    else if (seen.has(fund)) err(k('fund'), `${fund} is listed twice.`);
    seen.add(fund);
    const asOf = trim(f.asOf);
    if (asOf && !isIsoDate(asOf)) err(k('asOf'), 'As-of must be a date (YYYY-MM-DD).');
    const weights = {};
    let wsum = 0;
    if (!f.rows.length) err(k('rows'), 'Add at least one weight, or remove the fund.');
    for (const r of f.rows) {
      const id = normId(r.id);
      const rk = s => k(`row:${r.key}:${s}`);
      let idOk = true;
      if (!id) { err(rk('id'), 'Enter the holding id.'); idOk = false; }
      else if (id === fund) { err(rk('id'), 'A fund cannot hold itself.'); idOk = false; }
      else if (Object.hasOwn(weights, id)) { err(rk('id'), `${id} is listed twice.`); idOk = false; }
      const pct = parseNumber(r.pct);
      if (pct === null || pct <= 0 || pct > 100) err(rk('pct'), 'Percentage must be above 0 and at most 100.');
      else if (idOk) { weights[id] = pct; wsum += pct; }
    }
    if (wsum > 100.5) err(k('sum'), `The weights add up to ${Math.round(wsum * 100) / 100}% of the fund; they cannot exceed 100%.`);
    if (fund) {
      const entry = { ...(isObj(origLook[fund]) ? origLook[fund] : {}), weights };
      if (asOf) entry.asOf = asOf; else delete entry.asOf;
      if (trim(f.source)) entry.source = trim(f.source); else delete entry.source;
      look[fund] = entry;
    }
  }
  if (errors.length === lookStart && !same(look, origLook)) body.lookThrough = Object.keys(look).length ? look : null;

  /* ---- taxResidency ---- */
  const tax = pol.tax === 'none' || !pol.tax ? null : pol.tax;
  if (tax !== (orig.tax == null ? null : orig.tax)) body.taxResidency = tax;

  return { errors, body };
}

/* ================================================================== the UI */

const TABS = [
  { key: 'watchlist', label: 'Watchlist' },
  { key: 'positions', label: 'Positions' },
  { key: 'policy', label: 'Policy' },
  { key: 'rules', label: 'Alert rules' },
  { key: 'appearance', label: 'Appearance' },
];
const THEMES = [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']];
const VENUE_OPTS = [['', 'Select venue\u2026'], ...Object.keys(VENUES).map(m => [m, `${m} (${VENUES[m]})`])];
const FLAG_PREFIX = { watchlist: 'wl:', positions: 'pos:', policy: 'pol:', rules: 'rl:' };

export function openSettings({ tab } = {}) {
  const existing = document.querySelector('.st-root');
  if (existing && existing.stHandle) {
    existing.dispatchEvent(new CustomEvent('st-tab', { detail: tab }));
    return existing.stHandle;
  }

  const uid = Math.random().toString(36).slice(2, 8);
  let seq = 0;
  const nid = () => `st-${uid}-${++seq}`;

  /* ---- state ---- */
  const S = {
    tab: TABS.some(t => t.key === tab) ? tab : 'watchlist',
    status: 'loading', error: null,         // config/watchlist load
    wl: [], wlNote: '', add: blankDraft(), pos: new Map(), pol: null, chatModel: '', configTs: null,
    models: { status: 'idle', list: [], error: null },
    rules: null, rulesStatus: 'idle', rulesError: null, rulesSnap: '', notify: { status: 'idle', policy: null, error: null },
    baseSnap: '', err: new Map(), busy: false, confirming: false, closed: false,
  };
  const lrModels = latestRequest();
  const lrRules = latestRequest();
  const lrNotify = latestRequest();
  let loadSeq = 0;
  const offs = [];

  /* ---- skeleton ---- */
  const tabBtns = {};
  const panels = {};
  const tabsEl = el('div', { class: 'st-tabs', role: 'tablist', 'aria-label': 'Settings sections' });
  const panelsWrap = el('div', { class: 'st-panels' });
  for (const t of TABS) {
    const btnId = `st-tab-${t.key}-${uid}`;
    const panelId = `st-panel-${t.key}-${uid}`;
    tabBtns[t.key] = el('button', {
      type: 'button', class: 'st-tab', role: 'tab', id: btnId, 'aria-controls': panelId,
      'aria-selected': 'false', tabindex: '-1', dataset: { tab: t.key },
    }, el('span', { text: t.label }), el('span', { class: 'st-tab-flag badge', hidden: true }));
    panels[t.key] = el('div', { class: 'st-panel', role: 'tabpanel', id: panelId, 'aria-labelledby': btnId, tabindex: '-1', hidden: true, dataset: { panel: t.key } });
    tabsEl.append(tabBtns[t.key]);
    panelsWrap.append(panels[t.key]);
  }
  const alertEl = el('div', { class: 'st-alert', role: 'alert', hidden: true });
  const statusEl = el('span', { class: 'st-status', role: 'status', 'aria-live': 'polite' });
  const saveBtn = el('button', { type: 'button', class: 'btn btn-primary st-save', text: 'Save', onclick: () => save() });
  const cancelBtn = el('button', { type: 'button', class: 'btn st-cancel', text: 'Cancel', onclick: () => dlg.close() });
  const footer = el('div', { class: 'st-footer' }, alertEl,
    el('div', { class: 'st-footer-row' }, statusEl, el('div', { class: 'st-actions' }, cancelBtn, saveBtn)));
  const root = el('div', { class: 'st-root' }, tabsEl, panelsWrap, footer);

  const setStatus = t => { statusEl.textContent = t || ''; };
  const setAlert = t => { alertEl.textContent = t || ''; alertEl.hidden = !t; };
  function paintSave() {
    const blocked = S.status !== 'ready';
    saveBtn.disabled = blocked;
    saveBtn.setAttribute('aria-disabled', String(blocked || S.busy));
    saveBtn.classList.toggle('is-busy', S.busy);
    saveBtn.textContent = S.busy ? 'Saving\u2026' : 'Save';
    cancelBtn.disabled = S.busy;
    panelsWrap.inert = S.busy;
    root.setAttribute('aria-busy', String(S.busy));
  }

  /* ---- error plumbing: S.err key -> message; keys wl:<draftKey>:<field>, pos:<id>:<field>, rl:<ticker|default>:<i>:<key> ---- */
  function fieldWrap(labelText, control, errKey, { hint, help, cls } = {}) {
    if (!control.id) control.id = nid();
    const wrap = el('div', { class: 'field st-f' + (cls ? ' ' + cls : '') },
      el('label', { class: 'lbl', for: control.id }, labelText, hint ? el('span', { class: 'st-hint', text: ' ' + hint }) : null),
      control, help ? el('div', { class: 'field-help', text: help }) : null);
    if (errKey) {
      control.dataset.errKey = errKey;
      const msg = S.err.get(errKey);
      if (msg) {
        control.setAttribute('aria-invalid', 'true');
        control.setAttribute('aria-describedby', control.id + '-err');
        wrap.append(el('div', { class: 'field-error', id: control.id + '-err', text: msg }));
      }
    }
    return wrap;
  }
  function paintFlags() {
    for (const [tabKey, prefix] of Object.entries(FLAG_PREFIX)) {
      let n = 0;
      for (const k of S.err.keys()) if (k.startsWith(prefix)) n++;
      const flag = tabBtns[tabKey].querySelector('.st-tab-flag');
      flag.hidden = n === 0;
      flag.textContent = n ? String(n) : '';
      flag.setAttribute('aria-label', n ? `${n} problem${n === 1 ? '' : 's'}` : '');
    }
  }
  function onEdit(e) {
    const t = e.target;
    const k = t && t.dataset && t.dataset.errKey;
    if (!k || !S.err.has(k)) return;
    S.err.delete(k);
    t.removeAttribute('aria-invalid');
    t.removeAttribute('aria-describedby');
    const w = t.closest('.st-f');
    const msg = w && w.querySelector('.field-error');
    if (msg) msg.remove();
    paintFlags();
  }
  root.addEventListener('input', onEdit);
  root.addEventListener('change', onEdit);
  function focusFirstInvalid() {
    const bad = panels[S.tab].querySelector('[aria-invalid="true"]');
    if (bad) { bad.focus(); bad.scrollIntoView({ block: 'center' }); return true; }
    return false;
  }

  /* ---- small control builders ---- */
  function textInput(value, onInput, attrs = {}) {
    return el('input', { type: 'text', value, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', oninput: e => onInput(e.target.value), ...attrs });
  }
  function selectInput(options, current, onChange) {
    return el('select', { onchange: e => onChange(e.target.value) },
      options.map(o => {
        const [v, text] = Array.isArray(o) ? o : [o, o];
        return el('option', { value: v, text, selected: v === current });
      }));
  }
  const btn = (text, onclick, cls = '', attrs = {}) => el('button', { type: 'button', class: ('btn ' + cls).trim(), text, onclick, ...attrs });

  /* ============================================================ tab gating */
  function gate(panel) {
    if (S.status === 'loading') { renderState(panel, { loading: 'Loading settings' }); return true; }
    if (S.status === 'error') { renderState(panel, { error: S.error, retry: load }); return true; }
    return false;
  }

  /* ============================================================ WATCHLIST */
  function entryFields(d, { withId }) {
    const k = f => `wl:${d.key}:${f}`;
    const fields = [];
    if (withId) {
      fields.push(fieldWrap('Id', textInput(d.id, v => { d.id = v; }, { placeholder: 'e.g. SXR8', maxlength: 40, autocapitalize: 'characters' }), k('id'), { help: 'Letters, digits, . - _ (max 24). Cannot be changed later.' }));
    }
    fields.push(fieldWrap('Label', textInput(d.label, v => { d.label = v; }, { placeholder: 'Display name', autocomplete: 'off' }), k('label')));
    const kindSel = selectInput(KINDS, d.kind, v => {
      const prev = d.kind;
      d.kind = v;
      if (withId) { // a new entry: pick the matching default role like the server does
        if (v === 'index') d.role = 'benchmark';
        else if (prev === 'index' && d.role === 'benchmark') d.role = 'holding';
        roleSel.value = d.role;
      }
    });
    const roleSel = selectInput(ROLES, d.role, v => { d.role = v; if (d._chip) paintChip(d._chip, v); });
    const ccySel = selectInput(CURRENCIES, d.currency, v => { d.currency = v; d._ccyTouched = true; });
    const venueSel = selectInput(VENUE_OPTS, d.venue, v => {
      d.venue = v;
      if (withId && !d._ccyTouched && VENUES[v]) { d.currency = VENUES[v] === 'US' ? 'USD' : 'EUR'; ccySel.value = d.currency; }
    });
    fields.push(
      fieldWrap('Kind', kindSel, k('kind')),
      fieldWrap('Role', roleSel, k('role')),
      fieldWrap('Listing venue', venueSel, k('venue')),
      fieldWrap('Listing currency', ccySel, k('currency')),
      fieldWrap('Yahoo symbol', textInput(d.yahoo, v => { d.yahoo = v; }, { placeholder: 'e.g. ASML.AS' }), k('yahoo')),
    );
    const more = el('details', { class: 'st-more' },
      el('summary', { text: 'Listing symbol & other providers' }),
      el('div', { class: 'st-grid' },
        fieldWrap('Listing symbol', textInput(d.listingSymbol, v => { d.listingSymbol = v; }, { placeholder: 'same as Yahoo symbol' }), k('listingSymbol'), { help: 'Only if the series shown differs from the Yahoo symbol.' }),
        fieldWrap('Twelve Data symbol', textInput(d.twelvedata, v => { d.twelvedata = v; }, { placeholder: 'optional' }), k('twelvedata')),
        fieldWrap('Finnhub symbol', textInput(d.finnhub, v => { d.finnhub = v; }, { placeholder: 'optional' }), k('finnhub'))));
    if (d.listingSymbol || d.twelvedata || d.finnhub) more.open = true;
    return { fields, more };
  }
  function paintChip(chip, role) {
    chip.textContent = role;
    chip.className = 'chip st-role ' + (role === 'benchmark' ? 'chip-info' : 'chip-ok');
  }

  function entryCard(d) {
    const { fields, more } = entryFields(d, { withId: false });
    const chip = el('span');
    paintChip(chip, d.role);
    d._chip = chip;
    return el('section', { class: 'st-entry card', dataset: { key: d.key, id: d.id }, 'aria-label': d.id },
      el('div', { class: 'st-entry-head' },
        el('strong', { class: 'st-entry-id num', text: d.id }), chip,
        d.orig ? null : el('span', { class: 'chip chip-warn', text: 'new, unsaved' }),
        el('span', { class: 'st-spacer' }),
        btn('Remove', () => removeEntry(d), 'btn-sm btn-danger st-remove', { 'aria-label': `Remove ${d.id} from the watchlist` })),
      el('div', { class: 'st-grid' }, fields), more);
  }

  async function removeEntry(d) {
    const id = normId(d.id);
    const p = S.pos.get(id);
    let msg = `Remove ${id} from the watchlist?`;
    if (p) {
      const sh = parseNumber(p.shares);
      msg += ` Its position (${sh !== null ? sh : '?'} shares${p.unknown ? ', cost unknown' : ''}) is removed with it.`;
    }
    msg += ' Nothing is written until you press Save.';
    if (!await confirmDialog(msg, { title: 'Remove entry', confirmText: 'Remove', danger: true })) return;
    S.wl = S.wl.filter(x => x !== d);
    S.pos.delete(id);
    for (const key of [...S.err.keys()]) if (key.startsWith(`wl:${d.key}:`)) S.err.delete(key);
    renderWatchlist();
    paintFlags();
    setStatus(`${id} removed - press Save to apply.`);
    const next = panels.watchlist.querySelector('.st-add-card input');
    if (next) next.focus();
  }

  function addCard() {
    const d = S.add;
    const { fields, more } = entryFields(d, { withId: true });
    const card = el('section', { class: 'st-add-card card', 'aria-labelledby': `st-add-h-${uid}` },
      el('h3', { class: 'card-title', id: `st-add-h-${uid}`, text: 'Add entry' }),
      el('div', { class: 'st-grid' }, fields), more,
      el('div', { class: 'st-add-actions' }, btn('Add to watchlist', () => addEntry(), 'btn-primary st-add')));
    return card;
  }
  function addEntry() {
    for (const key of [...S.err.keys()]) if (key.startsWith(`wl:${S.add.key}:`)) S.err.delete(key);
    const errs = validateWatchlist([...S.wl, S.add]).filter(e => e.key === S.add.key);
    if (errs.length) {
      errs.forEach(e => S.err.set(`wl:${e.key}:${e.field}`, e.message));
      renderWatchlist();
      paintFlags();
      const bad = panels.watchlist.querySelector('.st-add-card [aria-invalid="true"]');
      if (bad) bad.focus();
      setStatus(`${errs.length} problem${errs.length === 1 ? '' : 's'} with the new entry.`);
      return;
    }
    const added = S.add;
    S.add = blankDraft();
    S.wl.push(added);
    renderWatchlist();
    paintFlags();
    setStatus(`${normId(added.id)} added - press Save to apply.`);
    const card = panels.watchlist.querySelector(`.st-entry[data-key="${added.key}"]`);
    if (card) { card.setAttribute('tabindex', '-1'); card.focus(); card.scrollIntoView({ block: 'nearest' }); }
  }

  function renderWatchlist() {
    const p = panels.watchlist;
    if (gate(p)) return;
    clear(p);
    const general = S.err.get('wl:null:general');
    p.append(...[
      el('p', { class: 'st-intro muted', text: 'Everything the dashboard tracks. A holding needs a listing (venue, currency, symbol); the benchmark is shown for comparison only and never produces alerts or analysis.' }),
      S.wlNote ? el('div', { class: 'state state-error st-note', role: 'status', text: S.wlNote }) : null,
      general ? el('div', { class: 'state state-error', role: 'alert' }, el('div', { class: 'state-text', text: general })) : null,
    ].filter(Boolean));
    if (!S.wl.length) p.append(el('div', { class: 'state state-empty' }, el('div', { class: 'state-text', text: 'The watchlist is empty. Add at least one entry below.' })));
    const list = el('div', { class: 'st-entries' });
    S.wl.forEach(d => list.append(entryCard(d)));
    p.append(list, addCard());
  }

  /* ============================================================ POSITIONS */
  const holdingDrafts = () => S.wl.filter(d => d.role === 'holding' && normId(d.id));
  const holdingIdSet = () => new Set(holdingDrafts().map(d => normId(d.id)));

  const fmtShares = n => String(Math.round(n * 1e6) / 1e6);

  function lotsDerivedText(p) {
    const dv = deriveFromLots(p.lots);
    let t = `Derived from the lots (FIFO, buy fees spread over the shares): ${fmtShares(dv.shares)} shares, cost ${fmtEur(dv.cost, { dec: 2 })}`;
    if (dv.shares > 1e-9) t += `, average ${fmtEur(dv.cost / dv.shares, { dec: 2 })} per share`;
    if (dv.skipped) t += `. ${dv.skipped} incomplete row${dv.skipped === 1 ? '' : 's'} not counted yet`;
    if (dv.oversold > 1e-9) t += '. A sell exceeds the shares held';
    return t + '.';
  }
  function avgText(p) {
    if (p.lots.length) return lotsDerivedText(p);
    if (p.unknown) return 'Cost unknown \u2013 profit/loss cannot be shown.';
    const a = avgCost(p);
    return a === null ? 'Average cost per share: \u2013' : `Average cost per share: ${fmtEur(a, { dec: 2 })}`;
  }

  /** With lots the server derives shares and cost: the plain inputs show the client-side FIFO result, read-only. */
  function positionFields(p, id) {
    const k = f => `pos:${id}:${f}`;
    const locked = p.lots.length > 0;
    const avg = el('div', { class: 'st-avg num', 'aria-live': 'polite', text: avgText(p) });
    const shares = el('input', {
      type: 'number', min: '0', step: 'any', inputmode: 'decimal', placeholder: 'e.g. 12.5', readOnly: locked,
      oninput: e => { if (!locked) { p.shares = e.target.value; avg.textContent = avgText(p); } },
    });
    const cost = el('input', {
      type: 'number', min: '0', step: 'any', inputmode: 'decimal', placeholder: 'total paid in EUR', readOnly: locked, disabled: !locked && p.unknown,
      oninput: e => { if (!locked) { p.invested = e.target.value; avg.textContent = avgText(p); } },
    });
    const unknown = el('input', {
      type: 'checkbox', checked: !locked && p.unknown, disabled: locked,
      onchange: e => {
        p.unknown = e.target.checked;
        cost.disabled = p.unknown;
        if (p.unknown) { S.err.delete(k('invested')); cost.removeAttribute('aria-invalid'); const m = cost.closest('.st-f').querySelector('.field-error'); if (m) m.remove(); paintFlags(); }
        avg.textContent = avgText(p);
      },
    });
    const paintDerived = () => {
      avg.textContent = avgText(p);
      if (locked) {
        const dv = deriveFromLots(p.lots);
        shares.value = fmtShares(dv.shares);
        cost.value = String(Math.round(dv.cost * 100) / 100);
      }
    };
    if (locked) paintDerived();
    else { shares.value = p.shares; cost.value = p.invested; }
    shares.id = `st-shares-${id}`;
    cost.id = `st-cost-${id}`;
    return {
      paintDerived,
      nodes: [
        fieldWrap('Shares', shares, k('shares'), { help: locked ? 'Derived from the dated lots below: read-only.' : undefined }),
        fieldWrap('Cost basis, EUR total', cost, k('invested'), { help: locked ? 'FIFO cost of the remaining shares, fees included: read-only.' : 'Total amount paid for all shares, in EUR.' }),
        el('label', { class: 'st-check' }, unknown, el('span', { text: 'Cost unknown' })),
        avg,
      ],
    };
  }

  /* ---- dated lots ---- */
  const clearLotErrors = id => { for (const key of [...S.err.keys()]) if (key.startsWith(`pos:${id}:lot`)) S.err.delete(key); };

  function rerenderPosition(id, focusSel) {
    const d = holdingDrafts().find(x => normId(x.id) === id);
    const old = panels.positions.querySelector(`.st-pos[data-id="${CSS.escape(id)}"]`);
    if (!d || !old) { renderPositions(); return; }
    const fresh = positionCard(d);
    old.replaceWith(fresh);
    paintFlags();
    const f = focusSel && fresh.querySelector(focusSel);
    if (f) f.focus();
  }

  /** Leaving lot mode keeps the derived numbers as the plain values (nothing is lost), when they are usable. */
  function adoptDerivedAsPlain(p) {
    const dv = deriveFromLots(p.lots);
    if (dv.shares > 1e-9 && !dv.skipped && dv.oversold <= 1e-9) {
      p.shares = fmtShares(dv.shares);
      p.invested = String(Math.round(dv.cost * 100) / 100);
      p.unknown = false;
    }
  }

  function addLot(p, id) {
    let row;
    if (!p.lots.length) { // the first lot starts from the plain position so nothing is retyped
      const sh = parseNumber(p.shares);
      const avg = avgCost(p);
      row = newLot(sh !== null && sh > 0 ? { shares: String(sh), priceEur: avg !== null ? String(Math.round(avg * 10000) / 10000) : '' } : {});
    } else {
      row = newLot();
    }
    p.lots.push(row);
    clearLotErrors(id);
    rerenderPosition(id, `.st-lot[data-lot="${p.lots.length - 1}"] input[type="date"]`);
  }

  function removeLot(p, id, i) {
    if (p.lots.length === 1) adoptDerivedAsPlain(p);
    p.lots.splice(i, 1);
    clearLotErrors(id);
    rerenderPosition(id, '.st-add-lot');
  }

  async function removeAllLots(p, id) {
    const dv = deriveFromLots(p.lots);
    const ok = await confirmDialog(
      `Remove all ${p.lots.length} dated lot${p.lots.length === 1 ? '' : 's'} of ${id}? Shares (${fmtShares(dv.shares)}) and cost (${fmtEur(dv.cost, { dec: 2 })}) stay as plain values; the purchase dates are lost. Nothing is written until you press Save.`,
      { title: 'Remove dated lots', confirmText: 'Remove dated lots', danger: true });
    if (!ok) return;
    adoptDerivedAsPlain(p);
    p.lots = [];
    clearLotErrors(id);
    rerenderPosition(id, '.st-add-lot');
    setStatus(`Dated lots of ${id} removed - press Save to apply.`);
  }

  function lotRow(p, id, lot, i, paintDerived) {
    const k = f => `pos:${id}:lot:${i}:${f}`;
    const field = (label, f, control) => fieldWrap(label, control, k(f), { cls: 'st-lot-f' });
    const numIn = f => el('input', { type: 'number', min: '0', step: 'any', inputmode: 'decimal', value: lot[f], oninput: e => { lot[f] = e.target.value; paintDerived(); } });
    const rowErr = S.err.get(k('row'));
    return el('div', { class: 'st-lot', dataset: { lot: i }, role: 'group', 'aria-label': `Lot ${i + 1} of ${id}` },
      el('div', { class: 'st-lot-fields' },
        field('Date', 'date', el('input', { type: 'date', max: todayIso(), value: lot.date, oninput: e => { lot.date = e.target.value; paintDerived(); } })),
        field('Shares', 'shares', numIn('shares')),
        field('Price, EUR', 'priceEur', numIn('priceEur')),
        field('Fees, EUR', 'feesEur', numIn('feesEur')),
        field('Side', 'side', selectInput([['buy', 'Buy'], ['sell', 'Sell']], lot.side, v => { lot.side = v; paintDerived(); })),
        el('button', { type: 'button', class: 'icon-btn st-lot-del', 'aria-label': `Remove lot ${i + 1} of ${id}`, title: 'Remove this lot', text: '\u00d7', onclick: () => removeLot(p, id, i) })),
      rowErr ? el('div', { class: 'field-error', role: 'alert', text: rowErr }) : null);
  }

  function lotsEditor(p, id, paintDerived) {
    const has = p.lots.length > 0;
    const listErr = S.err.get(`pos:${id}:lots`);
    const rows = el('div', { class: 'st-lot-list' });
    p.lots.forEach((lot, i) => rows.append(lotRow(p, id, lot, i, paintDerived)));
    return el('section', { class: 'st-lots', dataset: { id }, 'aria-label': `Dated lots of ${id}` },
      el('div', { class: 'st-lots-head' },
        el('h4', { class: 'st-lots-title', text: 'Dated lots' }),
        el('span', { class: 'muted st-lots-count', text: has ? `${p.lots.length} lot${p.lots.length === 1 ? '' : 's'}` : 'none \u2013 plain shares and cost above' })),
      el('p', { class: 'muted st-intro', text: has
        ? 'Shares and cost basis are derived from these lots (FIFO) and saved as lots. Dates unlock time-weighted return and the 3-year tax test.'
        : 'Optional. Record each buy and sell with its date to get time-weighted return, XIRR and the 3-year tax test. Adding a lot switches this position to lots.' }),
      listErr ? el('div', { class: 'field-error', role: 'alert', text: listErr }) : null,
      rows,
      el('div', { class: 'st-lots-actions' },
        btn('Add lot', () => addLot(p, id), 'btn-sm st-add-lot', { 'aria-label': `Add a dated lot to ${id}` }),
        has ? btn('Remove dated lots', () => removeAllLots(p, id), 'btn-sm btn-danger st-remove-lots', { 'aria-label': `Remove all dated lots of ${id}` }) : null));
  }

  function positionCard(d) {
    const id = normId(d.id);
    const p = S.pos.get(id);
    const lst = d.venue ? `${d.venue} \u00b7 ${d.currency}` : '';
    const head = el('div', { class: 'st-entry-head' },
      el('strong', { class: 'st-entry-id num', text: id }),
      d.label && d.label !== id ? el('span', { class: 'muted', text: d.label }) : null,
      lst ? el('span', { class: 'chip', text: lst }) : null,
      p && p.lots.length ? el('span', { class: 'chip chip-info', text: 'dated lots' }) : null,
      el('span', { class: 'st-spacer' }),
      p ? btn('Remove position', () => removePosition(id), 'btn-sm btn-danger', { 'aria-label': `Remove the position of ${id}` }) : null);
    let body;
    if (p) {
      const f = positionFields(p, id);
      body = el('div', { class: 'st-pos-body' }, el('div', { class: 'st-pos-fields' }, f.nodes), lotsEditor(p, id, f.paintDerived));
    } else {
      body = el('div', { class: 'state st-nopos' },
        el('div', { class: 'state-text' },
          el('div', { class: 'state-title', text: 'No position' }),
          el('div', { class: 'state-detail', text: `${id} is tracked but no shares are recorded, so it contributes nothing to the portfolio.` })),
        btn('Add position', () => addPosition(id), 'btn-sm st-add-pos'));
    }
    return el('section', { class: 'st-pos card', dataset: { id }, 'aria-label': `Position ${id}` }, head, body);
  }

  function orphanCard(p) {
    return el('section', { class: 'st-pos st-orphan card', dataset: { id: p.id }, 'aria-label': `Position without holding ${p.id}` },
      el('div', { class: 'st-entry-head' },
        el('strong', { class: 'st-entry-id num', text: p.id }),
        el('span', { class: 'chip chip-err', text: 'not a holding' }),
        el('span', { class: 'st-spacer' }),
        btn('Remove position', () => removePosition(p.id), 'btn-sm btn-danger', { 'aria-label': `Remove the position of ${p.id}` })),
      el('div', { class: 'field-error', text: S.err.get(`pos:${p.id}:id`) || `${p.id} is no longer a holding on the watchlist: remove this position, or set the entry's role back to holding.` }));
  }

  function renderPositions() {
    const p = panels.positions;
    if (getBook() === 'practice') {
      // Practice positions belong to the agent (Trading 212 demo bucket): no editor, nothing is ever PUT for them.
      renderState(p, { empty: 'Positions in Practice are managed by the agent on the Trading 212 demo account; switch to Real to edit your portfolio.' });
      return;
    }
    if (gate(p)) return;
    clear(p);
    const holdings = holdingDrafts();
    const ids = holdingIdSet();
    const orphans = [...S.pos.values()].filter(x => !ids.has(x.id));
    p.append(el('p', { class: 'st-intro muted', text: 'One row per holding. Amounts are EUR totals; the average cost per share is derived. Optional dated lots (buy / sell) replace the plain numbers: shares and cost are then derived from them.' }));
    if (!holdings.length && !orphans.length) {
      p.append(el('div', { class: 'state state-empty' }, el('div', { class: 'state-text', text: 'No holdings. Add a watchlist entry with the role "holding" first.' })));
      return;
    }
    const list = el('div', { class: 'st-entries' });
    holdings.forEach(d => list.append(positionCard(d)));
    p.append(list);
    if (orphans.length) {
      p.append(el('h3', { class: 'card-title st-sub', text: 'Positions without a holding' }));
      const ol = el('div', { class: 'st-entries' });
      orphans.forEach(o => ol.append(orphanCard(o)));
      p.append(ol);
    }
  }

  function addPosition(id) {
    S.pos.set(id, { id, shares: '', invested: '', unknown: false, lots: [], hadLots: false });
    renderPositions();
    const first = panels.positions.querySelector(`.st-pos[data-id="${CSS.escape(id)}"] input`);
    if (first) first.focus();
  }
  async function removePosition(id) {
    const p = S.pos.get(id);
    const sh = p ? parseNumber(p.shares) : null;
    const ok = await confirmDialog(`Remove the position of ${id}${sh !== null ? ` (${sh} shares)` : ''}? The entry stays on the watchlist. Nothing is written until you press Save.`,
      { title: 'Remove position', confirmText: 'Remove position', danger: true });
    if (!ok) return;
    S.pos.delete(id);
    for (const key of [...S.err.keys()]) if (key.startsWith(`pos:${id}:`)) S.err.delete(key);
    renderPositions();
    paintFlags();
    setStatus(`Position of ${id} removed - press Save to apply.`);
    const again = panels.positions.querySelector(`.st-pos[data-id="${CSS.escape(id)}"] .st-add-pos`);
    (again || panels.positions).focus();
  }

  /* ============================================================ POLICY */
  const watchEntries = () => S.wl.filter(d => normId(d.id)).map(d => ({ id: normId(d.id), label: trim(d.label) || normId(d.id) }));

  /** Structural change (add/remove a fund or row): redraw the tab, keep the scroll position. */
  function rerenderPolicy(focusSel) {
    const top = panelsWrap.scrollTop;
    renderPolicy();
    panelsWrap.scrollTop = top;
    paintFlags();
    const f = focusSel && panels.policy.querySelector(focusSel);
    if (f) f.focus();
  }
  const dropErrors = prefix => { for (const key of [...S.err.keys()]) if (key.startsWith(prefix)) S.err.delete(key); };

  function targetsCard(P) {
    const holdings = holdingDrafts().map(d => ({ id: normId(d.id), label: trim(d.label) }));
    const hset = new Set(holdings.map(x => x.id));
    const orphans = [...P.targets.keys()].filter(id => !hset.has(id));
    const sumMsg = S.err.get('pol:targets');
    const sumErr = el('div', { class: 'field-error', role: 'alert', hidden: !sumMsg, text: sumMsg || '' });
    const sumEl = el('div', { class: 'st-pol-sum num', id: 'st-pol-sum', role: 'status', 'aria-live': 'polite' });
    const paintSum = () => {
      let sum = 0;
      let n = 0;
      for (const v of P.targets.values()) { const x = parseNumber(v); if (x !== null) { sum += x; n++; } }
      const ok = n === 0 || Math.abs(sum - 100) <= 0.5;
      sumEl.textContent = n === 0
        ? 'No targets set. Once you set them they must add up to 100% (\u00b10.5).'
        : `Sum of targets: ${sum.toFixed(1)}% \u2013 ${ok ? 'OK' : 'must be 100% \u00b1 0.5'}`;
      sumEl.classList.toggle('is-bad', !ok);
      sumEl.dataset.sum = String(Math.round(sum * 100) / 100);
      sumEl.dataset.ok = String(ok);
    };
    const targetField = (id, label, orphan) => {
      const input = el('input', {
        type: 'number', min: '0', max: '100', step: 'any', inputmode: 'decimal', value: P.targets.has(id) ? P.targets.get(id) : '',
        dataset: { target: id },
        oninput: e => {
          P.targets.set(id, e.target.value);
          paintSum();
          if (S.err.delete('pol:targets')) { sumErr.hidden = true; paintFlags(); }
        },
      });
      return fieldWrap(`${id}${label && label !== id ? ' ' + label : ''} target %`, input, `pol:target:${id}`, { hint: orphan ? 'not a holding: clear it' : '' });
    };
    const plain = (label, key, value, set, attrs, opts) => fieldWrap(label,
      el('input', { type: 'number', step: 'any', inputmode: 'decimal', value, oninput: e => set(e.target.value), ...attrs }), key, opts);
    const trend = el('input', { type: 'checkbox', checked: P.trend, id: 'st-pol-trend', onchange: e => { P.trend = e.target.checked; } });
    paintSum();
    return el('section', { class: 'st-sec card', id: 'st-pol-targets', 'aria-labelledby': `st-pol-targets-h-${uid}` },
      el('h3', { class: 'card-title', id: `st-pol-targets-h-${uid}`, text: 'Targets and rebalancing' }),
      el('p', { class: 'muted st-intro', text: 'Target weight per holding, in %. Leave all blank to run without a policy.' }),
      el('div', { class: 'st-grid' },
        holdings.map(x => targetField(x.id, x.label, false)),
        orphans.map(id => targetField(id, '', true))),
      sumEl, sumErr,
      el('div', { class: 'st-grid' },
        plain('Max single stock %', 'pol:max', P.maxSingle, v => { P.maxSingle = v; }, { min: '0', max: '100' }, { help: 'Warn when one stock exceeds this share. Blank = no limit.' }),
        plain('Band: absolute points', 'pol:abs', P.absPct, v => { P.absPct = v; }, { min: '0', max: '100' }, { help: 'Default 5.' }),
        plain('Band: relative %', 'pol:rel', P.relPct, v => { P.relPct = v; }, { min: '0', max: '100' }, { help: 'Default 25. Half-width = min(absolute, target \u00d7 relative / 100).' }),
        plain('Monthly contribution, EUR', 'pol:contrib', P.contrib, v => { P.contrib = v; }, { min: '0' }, { help: 'Split toward the targets without selling. Blank = none.' })),
      el('label', { class: 'st-check' }, trend, el('span', { text: 'Trend filter (10-month moving average)' })));
  }

  function benchmarkCard(P) {
    const opts = [['', '(default: the benchmark entry)'],
      ...watchEntries().map(e => [e.id, `${e.id}${e.label !== e.id ? ' \u00b7 ' + e.label : ''}${e.id === 'SXR8' ? ' \u2013 total return, recommended' : ''}`])];
    if (P.bench && !opts.some(o => o[0] === P.bench)) opts.push([P.bench, `${P.bench} (not on the watchlist)`]);
    const sel = selectInput(opts, P.bench, v => { P.bench = v; });
    sel.id = 'st-pol-bench';
    return el('section', { class: 'st-sec card', id: 'st-pol-benchmark' },
      el('h3', { class: 'card-title', text: 'Performance benchmark' }),
      fieldWrap('Compare my performance and the advice scoreboard against', sel, 'pol:bench', {
        help: 'SXR8 (total return) is recommended: it reinvests dividends like your holdings do. A price index such as GSPC leaves dividends out and flatters the comparison.',
      }));
  }

  function exposureCard(P) {
    const fields = holdingDrafts().map(d => {
      const id = normId(d.id);
      const def = upper(d.currency) || 'EUR';
      const cur = P.exp.get(id) || def;
      const opts = CCYS.includes(cur) ? [...CCYS] : [...CCYS, cur];
      const sel = selectInput(opts, cur, v => { P.exp.set(id, v); });
      sel.dataset.exposure = id;
      return fieldWrap(`${id} exposure currency`, sel, `pol:exp:${id}`, { hint: `listed in ${def}` });
    });
    return el('section', { class: 'st-sec card', id: 'st-pol-exposure' },
      el('h3', { class: 'card-title', text: 'Exposure currency' }),
      el('p', { class: 'muted st-intro', text: 'The currency each holding is really exposed to (an S&P 500 ETF listed in EUR is a USD exposure). Drives the currency split and the FX part of returns. Defaults to the listing currency.' }),
      fields.length ? el('div', { class: 'st-grid' }, fields) : el('div', { class: 'muted', text: 'No holdings yet.' }));
  }

  function fundCard(P, f, ids) {
    const k = s => `pol:look:${f.key}:${s}`;
    const fundOpts = [['', 'Select fund\u2026'], ...ids.map(id => [id, id])];
    if (f.fund && !ids.includes(f.fund)) fundOpts.push([f.fund, `${f.fund} (not on the watchlist)`]);
    const fundSel = selectInput(fundOpts, f.fund, v => { f.fund = v; });
    const asOf = el('input', { type: 'date', value: f.asOf, oninput: e => { f.asOf = e.target.value; } });
    const source = textInput(f.source, v => { f.source = v; }, { placeholder: 'e.g. Slickcharts S&P 500 weights' });
    const sumEl = el('div', { class: 'st-look-sum num muted', role: 'status', 'aria-live': 'polite' });
    const paintSum = () => {
      let s = 0;
      let n = 0;
      f.rows.forEach(r => { const x = parseNumber(r.pct); if (x !== null) { s += x; n++; } });
      sumEl.textContent = n ? `Listed weights add up to ${s.toFixed(2)}% of the fund` : 'No weights yet';
      sumEl.classList.toggle('is-bad', s > 100.5);
    };
    const rows = el('div', { class: 'st-look-rows' });
    f.rows.forEach(r => {
      const del = el('button', { type: 'button', class: 'icon-btn st-look-del', 'aria-label': `Remove the ${r.id || 'empty'} weight of ${f.fund || 'this fund'}`, title: 'Remove this weight', text: '\u00d7',
        onclick: () => { f.rows = f.rows.filter(x => x !== r); dropErrors(`pol:look:${f.key}:`); rerenderPolicy(`.st-fund[data-key="${f.key}"] .st-add-weight`); } });
      rows.append(el('div', { class: 'st-look-row', dataset: { row: r.key } },
        fieldWrap('Holding id', textInput(r.id, v => { r.id = v; }, { list: 'st-pol-ids', autocapitalize: 'characters', placeholder: 'e.g. NVDA' }), k(`row:${r.key}:id`)),
        fieldWrap('% of the fund', el('input', { type: 'number', min: '0', max: '100', step: 'any', inputmode: 'decimal', value: r.pct, oninput: e => { r.pct = e.target.value; paintSum(); } }), k(`row:${r.key}:pct`)),
        del));
    });
    paintSum();
    const listErr = S.err.get(k('rows'));
    const sumErr = S.err.get(k('sum'));
    return el('section', { class: 'st-fund card', dataset: { key: f.key }, 'aria-label': `Look-through of ${f.fund || 'a new fund'}` },
      el('div', { class: 'st-entry-head' },
        el('strong', { class: 'st-entry-id num', text: f.fund || 'New fund' }),
        el('span', { class: 'st-spacer' }),
        btn('Remove fund', () => { P.look = P.look.filter(x => x !== f); dropErrors(`pol:look:${f.key}:`); rerenderPolicy('#st-pol-look .st-add-fund'); }, 'btn-sm btn-danger', { 'aria-label': `Remove the look-through of ${f.fund || 'this fund'}` })),
      el('div', { class: 'st-grid' },
        fieldWrap('Fund', fundSel, k('fund')),
        fieldWrap('As of', asOf, k('asOf')),
        fieldWrap('Source', source, k('source'))),
      listErr ? el('div', { class: 'field-error', role: 'alert', text: listErr }) : null,
      rows,
      sumEl,
      sumErr ? el('div', { class: 'field-error', role: 'alert', text: sumErr }) : null,
      el('div', { class: 'st-add-actions' },
        btn('Add weight', () => { f.rows.push(newLookRow()); dropErrors(`pol:look:${f.key}:`); rerenderPolicy(`.st-fund[data-key="${f.key}"] .st-look-row:last-child input`); }, 'btn-sm st-add-weight', { 'aria-label': `Add a weight to ${f.fund || 'this fund'}` })));
  }

  function lookThroughCard(P) {
    const ids = watchEntries().map(e => e.id);
    const funds = el('div', { class: 'st-entries', id: 'st-pol-lookthrough' });
    P.look.forEach(f => funds.append(fundCard(P, f, ids)));
    return el('section', { class: 'st-sec card', id: 'st-pol-look', 'aria-labelledby': `st-pol-look-h-${uid}` },
      el('h3', { class: 'card-title', id: `st-pol-look-h-${uid}`, text: 'Look-through' }),
      el('p', { class: 'muted st-intro', text: 'For a fund or ETF: how much of it (in %) is another watchlist entry, for example an S&P 500 fund holding NVDA. Used to show your real exposure to each name. Leave empty for none.' }),
      el('datalist', { id: 'st-pol-ids' }, ids.map(id => el('option', { value: id }))),
      !P.look.length ? el('div', { class: 'muted st-rt-empty', text: 'No look-through weights.' }) : null,
      funds,
      el('div', { class: 'st-add-actions' },
        btn('Add fund', () => { P.look.push(newLookFund()); rerenderPolicy('#st-pol-lookthrough .st-fund:last-child select'); }, 'btn-sm st-add-fund')));
  }

  function taxCard(P) {
    const opts = [['none', 'None / not set'], ['CZ', 'Czech Republic (CZ)']];
    if (!opts.some(o => o[0] === P.tax)) opts.push([P.tax, P.tax]);
    const sel = selectInput(opts, P.tax, v => { P.tax = v; });
    sel.id = 'st-pol-tax';
    return el('section', { class: 'st-sec card', id: 'st-pol-taxcard' },
      el('h3', { class: 'card-title', text: 'Tax residency' }),
      fieldWrap('Tax residency', sel, 'pol:tax'),
      el('p', { class: 'muted st-intro', id: 'st-pol-tax-note', text: 'Czech Republic: a sale of securities held for more than 3 years (the time test) is exempt from income tax, and so are a year\u2019s sales up to CZK 100,000 in gross proceeds. With CZ set, the dashboard tracks each lot\u2019s unlock date and tags advice with the tax status of a sale. This is an aid, not tax advice.' }));
  }

  function renderPolicy() {
    const p = panels.policy;
    if (gate(p) || !S.pol) return;
    const P = S.pol;
    clear(p);
    const srv = S.err.get('pol:server');
    p.append(el('p', { class: 'st-intro muted', text: 'Your portfolio policy: target weights and bands, the benchmark you are measured against, currency and look-through exposure, and tax residency. Nothing is written until you press Save.' }));
    if (srv) p.append(el('div', { class: 'state state-error', role: 'alert' }, el('div', { class: 'state-text', text: srv })));
    p.append(targetsCard(P), benchmarkCard(P), exposureCard(P), lookThroughCard(P), taxCard(P));
  }

  /* ============================================================ ALERT RULES */
  const alertableEntries = () => S.wl.filter(isAlertable).map(d => ({ id: normId(d.id), label: trim(d.label) || normId(d.id) }));

  async function loadRules(force) {
    if (S.status !== 'ready' || S.closed) return;
    if (S.rulesStatus === 'loading' || (S.rulesStatus === 'ready' && !force)) return;
    S.rulesStatus = 'loading';
    renderRules();
    const t = lrRules.begin();
    const r = await apiGet('/api/alert-rules', { signal: t.signal, timeoutMs: 15000 });
    if (!t.current() || S.closed) return;
    if (!r.ok || !isObj(r.data)) {
      S.rulesStatus = 'error';
      S.rulesError = r.ok ? 'The server returned an unexpected alert-rules payload.' : r.error;
    } else {
      S.rules = rulesModelFromBlock(r.data, alertableEntries());
      S.rulesSnap = rulesFingerprint(S.rules);
      S.rulesStatus = 'ready';
      S.rulesError = null;
    }
    renderRules();
  }

  /* ---- notification policy, read-only (GET /api/background notify.policy) ---- */
  async function loadNotify(force) {
    if (S.status !== 'ready' || S.closed) return;
    if (S.notify.status === 'loading' || (S.notify.status === 'ready' && !force)) return;
    S.notify = { status: 'loading', policy: null, error: null };
    repaintNotify();
    const t = lrNotify.begin();
    const r = await apiGet('/api/background', { signal: t.signal, timeoutMs: 15000 });
    if (!t.current() || S.closed) return;
    const pol = r.ok && isObj(r.data) && isObj(r.data.notify) ? r.data.notify.policy : null;
    S.notify = isObj(pol)
      ? { status: 'ready', policy: pol, error: null }
      : { status: 'error', policy: null, error: r.ok ? 'The server did not report a notification policy (older backend?).' : r.error };
    repaintNotify();
  }
  function repaintNotify() {
    const old = panels.rules.querySelector('#st-notify');
    if (old) old.replaceWith(notifyCard());
  }
  function notifyCard() {
    const n = S.notify;
    const card = el('section', { class: 'st-rt st-notify card', id: 'st-notify', 'aria-labelledby': `st-notify-h-${uid}` },
      el('h3', { class: 'card-title', id: `st-notify-h-${uid}`, text: 'Notifications (read-only)' }),
      el('p', { class: 'muted st-intro', text: 'Quiet hours, per-source caps and the ops topic come from the container environment (NOTIFY_QUIET_HOURS, NOTIFY_SOURCE_CAPS, NTFY_TOPIC_OPS). They are shown for reference and cannot be changed in the app.' }));
    if (n.status === 'idle' || n.status === 'loading') {
      card.append(el('div', { class: 'muted', role: 'status', text: 'Loading notification policy\u2026' }));
      return card;
    }
    if (n.status === 'error') {
      card.append(el('div', { class: 'state state-error st-note', role: 'alert' },
        el('div', { class: 'state-text' }, el('div', { class: 'state-title', text: 'Notification policy unavailable' }), el('div', { class: 'state-detail', text: n.error })),
        btn('Retry', () => loadNotify(true), 'btn-sm')));
      return card;
    }
    const p = n.policy;
    const qh = isObj(p.quiet_hours) ? p.quiet_hours : {};
    const win = trim(qh.window) || 'off';
    const caps = isObj(p.caps) ? p.caps : {};
    const counts = isObj(p.counts_24h) ? p.counts_24h : {};
    const kv = (k, ...v) => el('div', {}, el('dt', { text: k }), el('dd', {}, ...v));
    const capItems = Object.keys(caps).sort().map(s => {
      const used = Number(counts[s]) || 0;
      return el('li', { class: 'num' + (used >= Number(caps[s]) ? ' is-cap' : ''), text: `${s} ${used}/${caps[s]}${used >= Number(caps[s]) ? ' (at cap)' : ''}` });
    });
    card.append(el('dl', { class: 'kv st-notify-kv' },
      kv('Quiet hours', win === 'off' ? 'off' : `${win} (container-local time) \u2013 ${qh.active ? 'active now' + (qh.until ? ', until ' + fmtDateTime24(qh.until) : '') : 'not active now'}`),
      kv('Caps per source, rolling 24 h', capItems.length ? el('ul', { class: 'st-caps' }, capItems) : 'none'),
      kv('Held pushes', String(Number(p.held) || 0)),
      kv('Ops topic', trim(p.ops_topic) || '\u2013')));
    return card;
  }

  function ruleField(block, index, rule, f, repaint) {
    const key = `rl:${block.id}:${index}:${f.key}`;
    const lim = AR_LIMITS[f.key];
    const set = v => { rule.values[f.key] = v; if (f.key === 'mode' && repaint) repaint(); };
    let control;
    if (f.options) {
      const cur = rule.values[f.key];
      const opts = f.inherit ? ['', ...f.options] : [...f.options];
      if (cur && !opts.includes(cur)) opts.push(cur);
      control = selectInput(opts.map(o => [o, o || '(default)']), cur, set);
    } else if (f.bool) {
      control = el('input', { type: 'checkbox', checked: !!rule.values[f.key], onchange: e => set(e.target.checked) });
      return el('label', { class: 'st-check st-rf' }, control, el('span', { text: f.label }));
    } else if (f.date) {
      control = el('input', { type: 'date', value: rule.values[f.key], oninput: e => set(e.target.value) });
    } else {
      const int = AR_INT_KEYS.includes(f.key);
      control = el('input', { type: 'number', min: String(lim[0]), max: String(lim[1]), step: int ? '1' : 'any', inputmode: int ? 'numeric' : 'decimal', value: rule.values[f.key], oninput: e => set(e.target.value) });
    }
    control.dataset.rkey = key;
    return fieldWrap(f.label, control, key, { hint: lim ? `${lim[0]}\u2013${lim[1]}` : '', cls: 'st-rf' });
  }

  function ruleRow(block, index, rule, refreshBlock) {
    const kinds = AR_KINDS.includes(rule.kind) ? AR_KINDS : [...AR_KINDS, rule.kind];
    const fieldsHost = el('div', { class: 'st-rule-fields' });
    function paintFields() {
      clear(fieldsHost);
      const mode = rule.values.mode || S.rules.default.mode; // thresholdPct / hotPct only apply in pct mode
      (AR_KIND_FIELDS[rule.kind] || []).filter(f => !f.pctOnly || mode === 'pct')
        .forEach(f => fieldsHost.append(ruleField(block, index, rule, f, repaintMode)));
    }
    function repaintMode() {
      paintFields();
      const m = fieldsHost.querySelector('[data-rkey$=":mode"]');
      if (m) m.focus();
    }
    const kindSel = selectInput(kinds, rule.kind, v => {
      const keep = rule.values.cooldownMin; // the cooldown means the same thing for every kind
      rule.kind = v;
      rule.values = ruleValues(v, keep ? { cooldownMin: keep } : {});
      paintFields();
    });
    kindSel.setAttribute('aria-label', `Rule ${index + 1} kind for ${block.id}`);
    paintFields();
    return el('div', { class: 'st-rule', dataset: { kind: rule.kind, index } },
      el('div', { class: 'st-rule-kind' }, kindSel,
        el('button', { type: 'button', class: 'icon-btn st-rule-del', 'aria-label': `Remove rule ${index + 1} of ${block.id}`, title: 'Remove this rule', text: '\u00d7', onclick: () => { block.rules.splice(index, 1); refreshBlock(`.st-rule-del, .st-add-rule`); } })),
      fieldsHost);
  }

  /** Current volatility of one ticker from GET /api/alert-rules `sigma`. */
  function sigmaChip(block) {
    const sg = S.rules && isObj(S.rules.sigma) ? S.rules.sigma[block.id] : null;
    const pct = sg ? parseNumber(sg.sigmaPct) : null;
    if (pct === null) return el('span', { class: 'chip st-sigma is-na', title: 'No volatility estimate yet (needs about a year of daily closes)', text: '\u03c3 n/a' });
    return el('span', { class: 'chip st-sigma', dataset: { sigma: block.id }, title: `EWMA daily volatility${sg.asOf ? ' as of ' + sg.asOf : ''}`, text: `\u03c3 ${pct.toFixed(2)}%` });
  }

  function tickerBlock(block) {
    const node = el('section', { class: 'st-rt card' + (block.enabled ? '' : ' is-off'), dataset: { id: block.id } });
    const refresh = focusSel => {
      const fresh = tickerBlock(block);
      node.replaceWith(fresh);
      paintFlags();
      const f = focusSel && fresh.querySelector(focusSel);
      if (f) f.focus();
    };
    const on = el('input', {
      type: 'checkbox', checked: block.enabled, 'aria-label': `Use per-ticker rules for ${block.id}`,
      onchange: e => {
        block.enabled = e.target.checked;
        if (block.enabled && !block.rules.length) block.rules.push(makeRule('pct-move'));
        refresh('.st-rt-on');
      },
    });
    on.classList.add('st-rt-on');
    const count = block.enabled ? `${block.rules.length} rule${block.rules.length === 1 ? '' : 's'}` : 'inherits the default';
    node.append(el('div', { class: 'st-rt-head' },
      el('label', { class: 'st-check' }, on, el('strong', { class: 'num', text: block.id }),
        block.label && block.label !== block.id ? el('span', { class: 'muted', text: block.label }) : null),
      sigmaChip(block),
      el('span', { class: 'muted st-rt-count', text: count }),
      el('span', { class: 'st-spacer' }),
      btn('+ rule', () => { block.rules.push(makeRule('pct-move')); refresh('.st-rule:last-of-type select'); }, 'btn-sm st-add-rule', { disabled: !block.enabled, 'aria-label': `Add a rule for ${block.id}` })));
    if (block.enabled) {
      const list = el('div', { class: 'st-rules' });
      block.rules.forEach((r, i) => list.append(ruleRow(block, i, r, refresh)));
      if (!block.rules.length) list.append(el('div', { class: 'muted st-rt-empty', text: 'No rules - the default applies.' }));
      node.append(list);
    }
    return node;
  }

  function defaultCard() {
    const d = S.rules.default;
    const fk = k => `rl:default:-1:${k}`;
    const mk = (k, label) => {
      const lim = AR_LIMITS[k];
      const int = AR_INT_KEYS.includes(k);
      const c = el('input', { type: 'number', min: String(lim[0]), max: String(lim[1]), step: int ? '1' : 'any', inputmode: int ? 'numeric' : 'decimal', value: d[k], oninput: e => { d[k] = e.target.value; } });
      c.dataset.rkey = fk(k);
      return fieldWrap(label, c, fk(k), { hint: `${lim[0]}\u2013${lim[1]}`, cls: 'st-rf' });
    };
    const modeSel = selectInput([['sigma', 'sigma (scaled to volatility)'], ['pct', 'pct (fixed thresholds)']], d.mode, v => {
      d.mode = v;
      renderRules(); // thresholdPct / hotPct appear only in pct mode
      const m = panels.rules.querySelector('#st-rl-mode');
      if (m) m.focus();
    });
    modeSel.id = 'st-rl-mode';
    const fields = [
      fieldWrap('Mode', modeSel, null, { cls: 'st-rf' }),
      ...AR_DEFAULT_NUM.map(([k, l]) => mk(k, l)),
      fieldWrap('Direction', selectInput(AR_DIRECTIONS, d.direction, v => { d.direction = v; }), null, { cls: 'st-rf' }),
    ];
    if (d.mode === 'pct') fields.push(...AR_DEFAULT_PCT.map(([k, l]) => mk(k, l)));
    return el('section', { class: 'st-rt st-default card', id: 'st-rl-default' },
      el('h3', { class: 'card-title', text: 'Default rule' }),
      el('p', { class: 'muted st-intro', text: 'Applies to every ticker without its own rules; a field left blank in a per-ticker rule inherits it.' }),
      el('p', { class: 'muted st-intro', text: d.mode === 'pct'
        ? 'pct mode: alert at the fixed Threshold %, hot at Hot %. The z fields still have to be coherent.'
        : 'sigma mode: alert when the session move is at least zAlert times the ticker\u2019s \u03c3 (and at least Min move %); hot at zHot; re-armed when it falls below rearmZ. The gap rule only runs in the first minutes after the open. zHot must not be below zAlert and rearmZ must be below zAlert.' }),
      el('div', { class: 'st-rule-fields' }, fields));
  }

  function renderRules() {
    const p = panels.rules;
    if (gate(p)) return;
    if (S.rulesStatus === 'idle' || S.rulesStatus === 'loading') { renderState(p, { loading: 'Loading alert rules' }); return; }
    if (S.rulesStatus === 'error') { renderState(p, { error: S.rulesError, retry: () => loadRules(true) }); return; }
    syncRuleBlocks(S.rules, alertableEntries());
    clear(p);
    p.append(defaultCard(), notifyCard());
    const shown = S.rules.tickers.filter(b => alertableEntries().some(e => e.id === b.id));
    const orphans = S.rules.tickers.filter(b => !shown.includes(b));
    p.append(el('h3', { class: 'card-title st-sub', text: 'Per-ticker rules' }));
    if (!shown.length) p.append(el('div', { class: 'state state-empty' }, el('div', { class: 'state-text', text: 'No alertable watchlist entries (benchmarks and indices never alert).' })));
    const list = el('div', { class: 'st-entries' });
    shown.forEach(b => list.append(tickerBlock(b)));
    p.append(list);
    if (orphans.length) {
      p.append(el('h3', { class: 'card-title st-sub', text: 'Stored rules for tickers that are not alertable watchlist entries' }),
        el('p', { class: 'muted st-intro', text: 'Kept as stored. Untick a ticker to delete its rules when you save.' }));
      const ol = el('div', { class: 'st-entries' });
      orphans.forEach(b => ol.append(tickerBlock(b)));
      p.append(ol);
    }
  }

  /* ============================================================ APPEARANCE */
  let modelBox = null;

  async function loadModels() {
    S.models = { status: 'loading', list: [], error: null };
    paintModelBox();
    const t = lrModels.begin();
    const r = await apiGet('/api/models', { signal: t.signal, timeoutMs: 15000 });
    if (!t.current() || S.closed) return;
    if (r.ok && isObj(r.data) && Array.isArray(r.data.models)) {
      const list = r.data.models
        .map(m => (typeof m === 'string' ? { id: m, name: m } : { id: trim(m && m.id), name: trim(m && m.name) || trim(m && m.id) }))
        .filter(m => m.id);
      S.models = { status: 'ready', list, error: null };
    } else {
      S.models = { status: 'error', list: [], error: r.ok ? 'Unexpected /api/models payload' : r.error };
    }
    paintModelBox();
  }

  function paintModelBox() {
    if (!modelBox) return;
    if (S.status === 'loading') { renderState(modelBox, { loading: 'Loading' }); return; }
    if (S.status === 'error') { renderState(modelBox, { error: S.error, retry: load }); return; }
    clear(modelBox);
    const opts = S.models.list.map(m => [m.id, m.name]);
    if (S.chatModel && !opts.some(o => o[0] === S.chatModel)) opts.unshift([S.chatModel, `${S.chatModel} (current)`]);
    if (!opts.length) opts.push(['lane', 'Active GPU lane']);
    const sel = selectInput(opts, S.chatModel || opts[0][0], v => { S.chatModel = v; });
    if (!S.chatModel) S.chatModel = opts[0][0];
    if (S.models.status === 'loading') sel.disabled = true;
    modelBox.append(fieldWrap('Chat model', sel, null, { help: 'Model used by the chat assistant. "lane" follows whichever GPU lane is live.' }));
    if (S.models.status === 'loading') modelBox.append(el('div', { class: 'muted', role: 'status', text: 'Loading model list\u2026' }));
    if (S.models.status === 'error') {
      modelBox.append(el('div', { class: 'state state-error st-note', role: 'alert' },
        el('div', { class: 'state-text' }, el('div', { class: 'state-title', text: 'Model list unavailable' }), el('div', { class: 'state-detail', text: S.models.error })),
        btn('Retry', () => loadModels(), 'btn-sm')));
    }
  }

  function renderAppearance() {
    const p = panels.appearance;
    clear(p);
    const name = `st-theme-${uid}`;
    const current = store.get('themePref') || 'system';
    const radios = THEMES.map(([v, text]) => el('label', { class: 'st-radio' },
      el('input', { type: 'radio', name, value: v, checked: v === current, onchange: e => { if (e.target.checked) store.set('themePref', v); } }),
      el('span', { text })));
    modelBox = el('div', { class: 'st-model' });
    const about = el('dl', { class: 'kv st-about' },
      el('div', {}, el('dt', { text: 'Application' }), el('dd', { text: 'Portfolio Dashboard' })),
      el('div', {}, el('dt', { text: 'Currency' }), el('dd', { text: 'All amounts in EUR' })),
      el('div', {}, el('dt', { text: 'Frontend' }), el('dd', { text: 'ES modules, no build step' })),
      S.configTs ? el('div', {}, el('dt', { text: 'Config' }), el('dd', {}, stamp(S.configTs, { prefix: 'loaded ' }))) : null);
    p.append(
      el('fieldset', { class: 'st-fieldset' },
        el('legend', { class: 'card-title', text: 'Theme' }),
        el('div', { class: 'st-radios' }, radios),
        el('div', { class: 'field-help', text: 'Applies immediately and is remembered in this browser; it is not part of Save.' })),
      el('section', { class: 'st-sec' }, el('h3', { class: 'card-title', text: 'Chat' }), modelBox),
      el('section', { class: 'st-sec' }, el('h3', { class: 'card-title', text: 'About' }), about));
    paintModelBox();
  }
  offs.push(subscribeBook(() => { renderPositions(); paintFlags(); paintSave(); }));
  offs.push(store.subscribe('themePref', v => {
    const val = v || 'system';
    root.querySelectorAll(`input[type="radio"][name="st-theme-${uid}"]`).forEach(r => { r.checked = r.value === val; });
  }));

  /* ============================================================ tabs */
  function renderTab(key) {
    if (key === 'watchlist') renderWatchlist();
    else if (key === 'positions') renderPositions();
    else if (key === 'policy') renderPolicy();
    else if (key === 'rules') renderRules();
    else renderAppearance();
  }
  function selectTab(key, { focus = false } = {}) {
    if (!panels[key]) return;
    S.tab = key;
    for (const t of TABS) {
      const on = t.key === key;
      tabBtns[t.key].setAttribute('aria-selected', String(on));
      tabBtns[t.key].tabIndex = on ? 0 : -1;
      panels[t.key].hidden = !on;
    }
    if (key === 'positions' || key === 'policy' || key === 'rules') renderTab(key); // derived from the watchlist drafts
    if (key === 'rules') { loadRules(); loadNotify(); }
    if (focus) tabBtns[key].focus();
  }
  tabsEl.addEventListener('keydown', e => {
    const i = TABS.findIndex(t => t.key === S.tab);
    let n = -1;
    if (e.key === 'ArrowRight') n = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') n = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = TABS.length - 1;
    if (n < 0) return;
    e.preventDefault();
    selectTab(TABS[n].key, { focus: true });
  });
  tabsEl.addEventListener('click', e => {
    const b = e.target.closest('.st-tab');
    if (b) selectTab(b.dataset.tab);
  });
  root.addEventListener('st-tab', e => { if (e.detail) selectTab(e.detail, { focus: true }); });

  const paintAll = () => { TABS.forEach(t => renderTab(t.key)); paintFlags(); paintSave(); };

  /* ============================================================ load */
  async function again(fn) {
    let r = await fn();
    if (r === null) r = await fn(); // superseded by another refresh: ask once more
    return r;
  }

  function adoptConfig(cfg, watchlistArr) {
    S.wl = watchlistArr.map(draftFromEntry);
    S.pos = new Map();
    const pf = isObj(cfg.portfolio) ? cfg.portfolio : {};
    for (const [rawId, p] of Object.entries(pf)) {
      const id = normId(rawId);
      const o = isObj(p) ? p : {};
      const lots = (Array.isArray(o.lots) ? o.lots : []).filter(isObj).map(l => newLot({
        date: str(l.date), shares: l.shares == null ? '' : String(l.shares), priceEur: l.priceEur == null ? '' : String(l.priceEur),
        feesEur: l.feesEur == null ? '' : String(l.feesEur), side: l.side === 'sell' ? 'sell' : 'buy',
      }));
      S.pos.set(id, { id, shares: o.shares == null ? '' : String(o.shares), invested: o.investedAmount == null ? '' : String(o.investedAmount), unknown: o.investedAmount == null, lots, hadLots: lots.length > 0 });
    }
    S.pol = polFromConfig(cfg);
    S.chatModel = trim(cfg.chatModel) || 'lane';
    S.baseSnap = fpMain();
  }

  async function load() {
    const my = ++loadSeq;
    S.status = 'loading';
    S.error = null;
    paintAll();
    const [cfg, wl] = await Promise.all([again(refreshConfig), again(refreshWatchlist)]);
    if (my !== loadSeq || S.closed) return;
    if (!cfg || !cfg.ok || !isObj(cfg.data)) {
      S.status = 'error';
      const msg = cfg ? (cfg.ok ? 'The server returned an unexpected config payload.' : cfg.error) : 'The request was superseded - retry.';
      S.error = cfg && cfg.status === 503
        ? `The server cannot read the stored config (503): ${msg} Settings cannot be edited until this is fixed.`
        : msg;
      paintAll();
      return;
    }
    let entries = wl && wl.ok && Array.isArray(wl.data) ? wl.data : null;
    S.wlNote = '';
    if (!entries) {
      entries = Array.isArray(cfg.data.watchlist) ? cfg.data.watchlist : null;
      if (!entries) {
        S.status = 'error';
        S.error = `The watchlist could not be loaded${wl && wl.error ? ': ' + wl.error : ''}.`;
        paintAll();
        return;
      }
      S.wlNote = `The normalised watchlist is unavailable (${wl && wl.error ? wl.error : 'request failed'}); showing the stored entries.`;
    }
    S.configTs = store.slot('config').ts;
    adoptConfig(cfg.data, entries);
    S.status = 'ready';
    paintAll();
    if (S.models.status === 'idle') loadModels();
    if (S.tab === 'rules') { loadRules(); loadNotify(); }
  }

  /* ============================================================ dirty / close */
  const fpPolicy = P => (!P ? '' : JSON.stringify([
    [...P.targets].filter(([, v]) => trim(v) !== ''), P.maxSingle, P.absPct, P.relPct, P.contrib, P.trend, P.bench,
    [...P.exp], P.look.map(f => [f.fund, f.asOf, f.source, f.rows.map(r => [r.id, r.pct])]), P.tax,
  ]));
  function fpMain() {
    return JSON.stringify([
      S.wl.map(d => [d.id, d.label, d.kind, d.role, d.venue, d.currency, d.listingSymbol, d.yahoo, d.twelvedata, d.finnhub, d.orig ? 1 : 0]),
      [...S.pos.values()].map(p => (p.lots.length
        ? [p.id, 'lots', p.lots.map(l => [l.date, l.shares, l.priceEur, l.feesEur, l.side])] // shares/cost are derived: not part of the edit
        : [p.id, p.shares, p.invested, p.unknown])),
      S.chatModel,
      fpPolicy(S.pol),
    ]);
  }
  const rulesDirty = () => S.rulesStatus === 'ready' && rulesFingerprint(S.rules) !== S.rulesSnap;
  const isDirty = () => S.status === 'ready' && (fpMain() !== S.baseSnap || rulesDirty());

  /* ============================================================ save */
  /** A 400 text that names a lot or a policy key -> inline error on that panel. Returns the tab key to show, or null. */
  function routeServerError(message) {
    const text = str(message);
    let tab = null;
    if (/lot/i.test(text)) {
      for (const [id, p] of S.pos) {
        const upperText = text.toUpperCase();
        const named = [/[^A-Z0-9_.-]+/, /[^A-Z0-9_-]+/].some(sep => upperText.split(sep).includes(id.toUpperCase()));
        if (!named) continue;
        const m = /lots?\s*[[.](\d+)/i.exec(text);
        const idx = m ? Number(m[1]) : -1;
        S.err.set(idx >= 0 && idx < p.lots.length ? `pos:${id}:lot:${idx}:row` : `pos:${id}:lots`, text);
        tab = 'positions';
      }
    }
    if (!tab && /investmentPolicy|lookThrough|exposureCurrency|performanceBenchmark|taxResidency/.test(text)) {
      S.err.set('pol:server', text);
      tab = 'policy';
    }
    if (tab) { renderPositions(); renderPolicy(); }
    return tab;
  }

  function validateAll() {
    S.err.clear();
    const issues = [];
    const note = (tabKey, key, message) => { S.err.set(key, message); issues.push({ tab: tabKey, message }); };
    validateWatchlist(S.wl).forEach(e => note('watchlist', `wl:${e.key}:${e.field || 'general'}`, e.message));
    const pv = getBook() === 'practice' ? { errors: [], portfolio: null } : validatePositions([...S.pos.values()], holdingIdSet());
    pv.errors.forEach(e => note('positions', `pos:${e.id}:${e.field}`, e.message));
    let pol = null;
    if (S.pol) {
      pol = validatePolicy(S.pol, {
        holdings: holdingDrafts().map(d => ({ id: normId(d.id), currency: upper(d.currency) || 'EUR' })),
        watchIds: S.wl.map(d => normId(d.id)).filter(Boolean),
        today: todayIso(),
      });
      pol.errors.forEach(e => note('policy', e.key, e.message));
    }
    let rules = null;
    if (S.rulesStatus === 'ready') {
      syncRuleBlocks(S.rules, alertableEntries());
      rules = collectRules(S.rules);
      rules.errors.forEach(e => note('rules', `rl:${e.ticker === null ? 'default' : e.ticker}:${e.index}:${e.key}`, e.message));
    }
    return { issues, pv, pol, rules };
  }

  async function save() {
    if (S.busy || S.status !== 'ready') return;
    setAlert('');
    if (!isDirty()) { setStatus('No changes to save.'); return; }
    const v = validateAll();
    if (v.issues.length) {
      const first = TABS.map(t => t.key).find(k => v.issues.some(i => i.tab === k));
      TABS.forEach(t => { if (t.key !== 'appearance') renderTab(t.key); });
      paintFlags();
      selectTab(first);
      if (!focusFirstInvalid()) panels[first].focus();
      const n = v.issues.length;
      setStatus(`${n} problem${n === 1 ? '' : 's'} to fix: ${v.issues[0].message}`);
      return;
    }
    S.busy = true;
    paintSave();
    setStatus('Saving\u2026');
    const body = { watchlist: buildWatchlist(S.wl), portfolio: v.pv.portfolio, chatModel: S.chatModel, ...(v.pol ? v.pol.body : {}) };
    if (getBook() === 'practice') delete body.portfolio; // Practice never writes positions
    const r = await apiSend('PUT', '/api/config', body, { timeoutMs: 30000 });
    if (S.closed) return;
    if (!r.ok) {
      S.busy = false;
      paintSave();
      setStatus('');
      const routed = routeServerError(r.error); // a 400 about a lot / policy key is shown inline too
      setAlert(`Settings were not saved: ${r.error}${v.rules && rulesDirty() ? ' The alert rules were not saved either.' : ''}`);
      if (routed) { paintFlags(); selectTab(routed); }
      return;
    }
    if (isObj(r.data) && Array.isArray(r.data.watchlist)) {
      const keepBase = S.baseSnap;
      adoptConfig(r.data, r.data.watchlist); // the server's normalised copy becomes the new baseline
      if (fpMain() === keepBase) S.baseSnap = keepBase;
    } else {
      S.baseSnap = fpMain();
    }
    if (v.rules && rulesDirty()) {
      setStatus('Saving alert rules\u2026');
      const rr = await apiSend('POST', '/api/alert-rules', v.rules.payload, { timeoutMs: 20000 });
      if (S.closed) return;
      if (!rr.ok) {
        S.busy = false;
        paintAll();
        setStatus('');
        setAlert(`Watchlist, positions and chat model were saved, but the alert rules were NOT saved: ${rr.error}`);
        refreshAll();
        return;
      }
      if (isObj(rr.data)) {
        S.rules = rulesModelFromBlock(rr.data, alertableEntries());
        S.rulesSnap = rulesFingerprint(S.rules);
      } else {
        S.rulesSnap = rulesFingerprint(S.rules);
      }
    }
    S.busy = false;
    paintSave();
    toast('Settings saved', { type: 'ok' });
    refreshAll();
    realClose();
  }

  /* ============================================================ open */
  const dlg = openDialog({
    title: 'Settings', body: root, kind: 'drawer', size: 'lg', initialFocus: tabBtns[S.tab],
    onClose: () => {
      S.closed = true;
      loadSeq++;
      lrModels.abort();
      lrRules.abort();
      lrNotify.abort();
      offs.forEach(f => f());
      root.removeEventListener('input', onEdit);
      root.removeEventListener('change', onEdit);
      store.set('settingsOpen', false);
    },
  });
  const realClose = dlg.close;
  dlg.close = () => {
    if (S.closed || S.busy || S.confirming) return;
    if (!isDirty()) { realClose(); return; }
    S.confirming = true;
    confirmDialog('You have unsaved changes in Settings. Discard them?', {
      title: 'Unsaved changes', confirmText: 'Discard changes', cancelText: 'Keep editing', danger: true,
    }).then(ok => { S.confirming = false; if (ok) realClose(); });
  };
  dlg.body.classList.add('st-dialog-body');
  root.stHandle = dlg;
  store.set('settingsOpen', true);
  selectTab(S.tab);
  renderAppearance();
  paintAll();
  load();
  return dlg;
}

export const settings = { openSettings };
