/* Settings drawer: Watchlist | Positions | Alert rules | Appearance.
   export function openSettings({tab}) -> dialog handle (ui.openDialog drawer).

   Pure logic (validateWatchlist, buildWatchlist, collectRules, ...) is exported separately from the DOM code so it
   can be unit-tested in Node. Everything stateful lives in the openSettings closure: there is no
   module-level DOM state. Server contracts: app/api/registry.py normalize_entry,
   app/api/config_edit.py, app/api/rules.py. */
import { el, clear, fmtEur } from '../util.js';
import { apiGet, apiSend, latestRequest } from '../api.js';
import * as store from '../store.js';
import { openDialog, confirmDialog, renderState, toast, stamp } from '../ui.js';
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

/**
 * positions: [{id, shares:string, invested:string, unknown:boolean}]; holdingIds: Set|Array of ids.
 * -> {errors:[{id, field:'shares'|'invested'|'id', message}], portfolio:{ID:{shares, investedAmount|null}}}
 */
export function validatePositions(positions, holdingIds) {
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
    const shares = parseNumber(p.shares);
    if (shares === null || shares <= 0) errors.push({ id, field: 'shares', message: 'Shares must be a number greater than 0.' });
    let invested = null;
    if (!p.unknown) {
      invested = parseNumber(p.invested);
      if (invested === null || invested < 0) {
        errors.push({ id, field: 'invested', message: 'Cost basis must be a number of at least 0 EUR - or tick "Cost unknown".' });
      }
    }
    if (errors.length === before) portfolio[id] = { shares, investedAmount: invested };
  }
  return { errors, portfolio };
}

/** Average cost per share in EUR, or null. */
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
  leadDays: [0, 45],
};
const AR_INT_KEYS = ['cooldownMin', 'leadDays', 'period', 'fast', 'slow'];
const AR_DEFAULT_FIELDS = [['thresholdPct', 'Threshold %'], ['hotPct', 'Hot %'], ['cooldownMin', 'Cooldown (min)']];
const AR_COOL = { key: 'cooldownMin', label: 'Cooldown (min)' };
/* kind -> fields. `options` renders a select (inherit: blank = inherit the default), `bool` a
   checkbox, `date` a date input, anything else a number bounded by AR_LIMITS[key]. */
export const AR_KIND_FIELDS = {
  'pct-move': [
    { key: 'thresholdPct', label: 'Threshold %' },
    { key: 'hotPct', label: 'Hot %' },
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
 * {default:{thresholdPct,hotPct,cooldownMin,direction}, tickers:[{id,label,stored,enabled,rules:[{kind,values}]}]}.
 * Stored tickers that are not in `entries` are kept (so saving never silently deletes a stored rule).
 */
export function rulesModelFromBlock(block, entries) {
  const base = isObj(block) && isObj(block.default) ? block.default : {};
  const per = isObj(block) && isObj(block.perTicker) ? block.perTicker : {};
  const def = {
    thresholdPct: str(base.thresholdPct), hotPct: str(base.hotPct), cooldownMin: str(base.cooldownMin),
    direction: AR_DIRECTIONS.includes(base.direction) ? base.direction : 'both',
  };
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
  return { default: def, tickers };
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

/**
 * model -> {payload:{version:2, default, perTicker}, errors:[{ticker|null, index, key, message}]}.
 * Same limits and messages as the old editor; every problem is reported, not only the first.
 */
export function collectRules(model) {
  const payload = { version: 2, default: {}, perTicker: {} };
  const errors = [];
  const def = (model && model.default) || {};
  for (const [key] of AR_DEFAULT_FIELDS) {
    const v = arParse(def[key], key);
    if (v === null) errors.push({ ticker: null, index: -1, key, message: `Default: ${key} must be between ${arRange(key)}` });
    else payload.default[key] = v;
  }
  payload.default.direction = AR_DIRECTIONS.includes(def.direction) ? def.direction : 'both';

  for (const block of (model && model.tickers) || []) {
    if (!block.enabled) continue; // unticked: the whole list is dropped and the ticker inherits the default
    const list = [];
    block.rules.forEach((r, index) => {
      const rule = collectRule(block.id, r, index, errors);
      if (rule) list.push(rule);
    });
    if (list.length) payload.perTicker[block.id] = list;
  }
  return { payload, errors };
}

function collectRule(ticker, r, index, errors) {
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

/* ================================================================== the UI */

const TABS = [
  { key: 'watchlist', label: 'Watchlist' },
  { key: 'positions', label: 'Positions' },
  { key: 'rules', label: 'Alert rules' },
  { key: 'appearance', label: 'Appearance' },
];
const THEMES = [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']];
const VENUE_OPTS = [['', 'Select venue\u2026'], ...Object.keys(VENUES).map(m => [m, `${m} (${VENUES[m]})`])];
const FLAG_PREFIX = { watchlist: 'wl:', positions: 'pos:', rules: 'rl:' };

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
    wl: [], wlNote: '', add: blankDraft(), pos: new Map(), chatModel: '', configTs: null,
    models: { status: 'idle', list: [], error: null },
    rules: null, rulesStatus: 'idle', rulesError: null, rulesSnap: '',
    baseSnap: '', err: new Map(), busy: false, confirming: false, closed: false,
  };
  const lrModels = latestRequest();
  const lrRules = latestRequest();
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

  function avgText(p) {
    if (p.unknown) return 'Cost unknown \u2013 profit/loss cannot be shown.';
    const a = avgCost(p);
    return a === null ? 'Average cost per share: \u2013' : `Average cost per share: ${fmtEur(a, { dec: 2 })}`;
  }

  function positionFields(p, id) {
    const k = f => `pos:${id}:${f}`;
    const avg = el('div', { class: 'st-avg num', 'aria-live': 'polite', text: avgText(p) });
    const refresh = () => { avg.textContent = avgText(p); };
    const shares = el('input', { type: 'number', min: '0', step: 'any', inputmode: 'decimal', value: p.shares, placeholder: 'e.g. 12.5', oninput: e => { p.shares = e.target.value; refresh(); } });
    const cost = el('input', { type: 'number', min: '0', step: 'any', inputmode: 'decimal', value: p.invested, placeholder: 'total paid in EUR', disabled: p.unknown, oninput: e => { p.invested = e.target.value; refresh(); } });
    const unknown = el('input', {
      type: 'checkbox', checked: p.unknown,
      onchange: e => {
        p.unknown = e.target.checked;
        cost.disabled = p.unknown;
        if (p.unknown) { S.err.delete(k('invested')); cost.removeAttribute('aria-invalid'); const m = cost.closest('.st-f').querySelector('.field-error'); if (m) m.remove(); paintFlags(); }
        refresh();
      },
    });
    return [
      fieldWrap('Shares', shares, k('shares')),
      fieldWrap('Cost basis, EUR total', cost, k('invested'), { help: 'Total amount paid for all shares, in EUR.' }),
      el('label', { class: 'st-check' }, unknown, el('span', { text: 'Cost unknown' })),
      avg,
    ];
  }

  function positionCard(d) {
    const id = normId(d.id);
    const p = S.pos.get(id);
    const lst = d.venue ? `${d.venue} \u00b7 ${d.currency}` : '';
    const head = el('div', { class: 'st-entry-head' },
      el('strong', { class: 'st-entry-id num', text: id }),
      d.label && d.label !== id ? el('span', { class: 'muted', text: d.label }) : null,
      lst ? el('span', { class: 'chip', text: lst }) : null,
      el('span', { class: 'st-spacer' }),
      p ? btn('Remove position', () => removePosition(id), 'btn-sm btn-danger', { 'aria-label': `Remove the position of ${id}` }) : null);
    const body = p
      ? el('div', { class: 'st-pos-fields' }, positionFields(p, id))
      : el('div', { class: 'state st-nopos' },
        el('div', { class: 'state-text' },
          el('div', { class: 'state-title', text: 'No position' }),
          el('div', { class: 'state-detail', text: `${id} is tracked but no shares are recorded, so it contributes nothing to the portfolio.` })),
        btn('Add position', () => addPosition(id), 'btn-sm st-add-pos'));
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
    if (gate(p)) return;
    clear(p);
    const holdings = holdingDrafts();
    const ids = holdingIdSet();
    const orphans = [...S.pos.values()].filter(x => !ids.has(x.id));
    p.append(el('p', { class: 'st-intro muted', text: 'One row per holding. Amounts are EUR totals; the average cost per share is derived.' }));
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
    S.pos.set(id, { id, shares: '', invested: '', unknown: false });
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

  function ruleField(block, index, rule, f) {
    const key = `rl:${block.id}:${index}:${f.key}`;
    const lim = AR_LIMITS[f.key];
    const set = v => { rule.values[f.key] = v; };
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
    const paintFields = () => {
      clear(fieldsHost);
      (AR_KIND_FIELDS[rule.kind] || []).forEach(f => fieldsHost.append(ruleField(block, index, rule, f)));
    };
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
    return el('section', { class: 'st-rt st-default card' },
      el('h3', { class: 'card-title', text: 'Default rule' }),
      el('p', { class: 'muted st-intro', text: 'Applies to every ticker without its own rules; a field left blank in a per-ticker rule inherits it.' }),
      el('div', { class: 'st-rule-fields' },
        AR_DEFAULT_FIELDS.map(([k, l]) => mk(k, l)),
        fieldWrap('Direction', selectInput(AR_DIRECTIONS, d.direction, v => { d.direction = v; }), null, { cls: 'st-rf' })));
  }

  function renderRules() {
    const p = panels.rules;
    if (gate(p)) return;
    if (S.rulesStatus === 'idle' || S.rulesStatus === 'loading') { renderState(p, { loading: 'Loading alert rules' }); return; }
    if (S.rulesStatus === 'error') { renderState(p, { error: S.rulesError, retry: () => loadRules(true) }); return; }
    syncRuleBlocks(S.rules, alertableEntries());
    clear(p);
    p.append(defaultCard());
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
  offs.push(store.subscribe('themePref', v => {
    const val = v || 'system';
    root.querySelectorAll(`input[type="radio"][name="st-theme-${uid}"]`).forEach(r => { r.checked = r.value === val; });
  }));

  /* ============================================================ tabs */
  function renderTab(key) {
    if (key === 'watchlist') renderWatchlist();
    else if (key === 'positions') renderPositions();
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
    if (key === 'positions' || key === 'rules') renderTab(key); // derived from the watchlist drafts
    if (key === 'rules') loadRules();
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
      S.pos.set(id, { id, shares: o.shares == null ? '' : String(o.shares), invested: o.investedAmount == null ? '' : String(o.investedAmount), unknown: o.investedAmount == null });
    }
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
    if (S.tab === 'rules') loadRules();
  }

  /* ============================================================ dirty / close */
  function fpMain() {
    return JSON.stringify([
      S.wl.map(d => [d.id, d.label, d.kind, d.role, d.venue, d.currency, d.listingSymbol, d.yahoo, d.twelvedata, d.finnhub, d.orig ? 1 : 0]),
      [...S.pos.values()].map(p => [p.id, p.shares, p.invested, p.unknown]),
      S.chatModel,
    ]);
  }
  const rulesDirty = () => S.rulesStatus === 'ready' && rulesFingerprint(S.rules) !== S.rulesSnap;
  const isDirty = () => S.status === 'ready' && (fpMain() !== S.baseSnap || rulesDirty());

  /* ============================================================ save */
  function validateAll() {
    S.err.clear();
    const issues = [];
    const note = (tabKey, key, message) => { S.err.set(key, message); issues.push({ tab: tabKey, message }); };
    validateWatchlist(S.wl).forEach(e => note('watchlist', `wl:${e.key}:${e.field || 'general'}`, e.message));
    const pv = validatePositions([...S.pos.values()], holdingIdSet());
    pv.errors.forEach(e => note('positions', `pos:${e.id}:${e.field}`, e.message));
    let rules = null;
    if (S.rulesStatus === 'ready') {
      syncRuleBlocks(S.rules, alertableEntries());
      rules = collectRules(S.rules);
      rules.errors.forEach(e => note('rules', `rl:${e.ticker === null ? 'default' : e.ticker}:${e.index}:${e.key}`, e.message));
    }
    return { issues, pv, rules };
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
    const body = { watchlist: buildWatchlist(S.wl), portfolio: v.pv.portfolio, chatModel: S.chatModel };
    const r = await apiSend('PUT', '/api/config', body, { timeoutMs: 30000 });
    if (S.closed) return;
    if (!r.ok) {
      S.busy = false;
      paintSave();
      setStatus('');
      setAlert(`Settings were not saved: ${r.error}${v.rules && rulesDirty() ? ' The alert rules were not saved either.' : ''}`);
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
