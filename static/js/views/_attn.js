// "Needs attention" model + card DOM for the overview.
//
//   buildAttentionItems(inputs) -> items sorted by severity (stable within a severity)
//   buildAttentionCard(item)    -> <li class="attn-card">
//
// An item: {key, sev:'urgent'|'warn'|'info', title, ticker|null, deltaPct|null, detail|null,
//           thesis|null, why|null, event:{date,text,days}|null, action:{label, fn}|null}
//
// Sources: unread alerts (Open -> #alerts/<id>), today's moves (sigma rule: |z| >= zAlert, else the
// percent thresholds), 52-week drawdown, stale quotes, portfolio warnings, policy breaches (drift
// over/under band, max-single-stock) and pending advice (/api/advice `pending`). Ticker cards carry
// a one-line thesis (/api/advice excerpt, ~140 chars, the rest in <details>), a "why it matters"
// line (weight, P/L, youngest lot age when lots exist) and the ticker's next event.
import { el, fmtDateTime24, fmtPct, isNum } from '../util.js';
import { SEV_CHIP, SEV_ORDER, SEV_TEXT, chip, daysFromToday, deltaNode, eventText, normSeverity, plain, shortDate, splitAt } from './_common.js';

const THESIS_CHARS = 140;
const MDD_MIN = 15;

/** Effective alert rule for an id: default overlaid by perTicker (v3 rules; v2 configs have no `mode`). */
export function ruleFor(rules, id) {
  const def = (rules && rules.default) || {};
  const per = (rules && rules.perTicker && rules.perTicker[id]) || {};
  return { ...def, ...per };
}

/** Today's move classification for a position: {sev, z|null, limit} or null when below the rule. */
export function classifyMove(p, rules, sigma) {
  if (!isNum(p.dayPct)) return null;
  const r = ruleFor(rules, p.id);
  if (r.direction === 'up' && p.dayPct < 0) return null;
  if (r.direction === 'down' && p.dayPct > 0) return null;
  const s = sigma && sigma[p.id] && sigma[p.id].sigmaPct;
  if (r.mode === 'sigma' && isNum(s) && s > 0) {
    const z = p.dayPct / s;
    const zAlert = isNum(r.zAlert) ? r.zAlert : 2;
    const zHot = isNum(r.zHot) ? r.zHot : 3;
    const minPct = isNum(r.minPct) ? r.minPct : 1;
    if (Math.abs(z) < zAlert || Math.abs(p.dayPct) < minPct) return null;
    return { sev: Math.abs(z) >= zHot ? 'urgent' : 'warn', z };
  }
  const thr = isNum(r.thresholdPct) ? r.thresholdPct : 3;
  const hot = isNum(r.hotPct) ? r.hotPct : 5;
  if (Math.abs(p.dayPct) < thr) return null;
  return { sev: Math.abs(p.dayPct) >= hot ? 'urgent' : 'warn', z: null, limit: thr };
}

/** Age in whole days of the youngest dated buy lot, or null when there are none. */
export function youngestLotAgeDays(lots, now = new Date()) {
  let best = null;
  for (const l of Array.isArray(lots) ? lots : []) {
    if (l && l.side === 'sell') continue;
    const d = daysFromToday(l && l.date, now);
    if (d == null) continue;
    const age = -d;
    if (best == null || age < best) best = age;
  }
  return best;
}

/** Earliest upcoming event (today or later) for a ticker: {date, text, days} | null. */
export function nextEventFor(events, id, now = new Date()) {
  let best = null;
  for (const e of (events && events.events) || []) {
    if (e.ticker !== id) continue;
    const days = daysFromToday(e.date, now);
    if (days == null || days < 0) continue;
    if (!best || e.date < best.date) best = { date: e.date, days, e };
  }
  return best ? { date: best.date, days: best.days, text: eventText(best.e) } : null;
}

/**
 * inputs: {portfolio: slot, alerts: slot, lane: slot, config, rules, advice, events, policy,
 *          labelOf(id), positionOf(id), go(hash), settings(tab), retryAlerts(), retryPortfolio(), now}
 */
export function buildAttentionItems(inp) {
  const { labelOf, positionOf, go, settings } = inp;
  const now = inp.now || new Date();
  const pf = inp.portfolio;
  const d = pf.data;
  const adviceMap = inp.advice && typeof inp.advice === 'object' ? inp.advice : {};
  const cfgPortfolio = (inp.config && inp.config.portfolio) || {};
  const rules = (inp.rules && inp.rules.default) ? inp.rules : (inp.config && inp.config.alertRules) || null;
  const sigma = (inp.rules && inp.rules.sigma) || {};
  const items = [];
  const open = id => ({ label: 'Open', fn: () => go(`#detail/${encodeURIComponent(id)}`) });

  // Fields every ticker card shares.
  const tickerFields = id => {
    const p = positionOf(id);
    const a = adviceMap[id];
    const lotAge = youngestLotAgeDays((cfgPortfolio[id] || {}).lots, now);
    const bits = [];
    if (p) {
      bits.push(`weight ${isNum(p.weightPct) ? fmtPct(p.weightPct, { sign: false, dec: 1 }) : '\u2013'}`);
      bits.push(`P/L ${isNum(p.pnlPct) ? fmtPct(p.pnlPct) : 'n/a'}`);
      if (lotAge != null) bits.push(`youngest lot ${lotAge} d`);
    }
    return {
      ticker: id,
      deltaPct: p && isNum(p.dayPct) ? p.dayPct : null,
      thesis: a && a.excerpt ? plain(a.excerpt) : null,
      why: bits.length ? bits.join(' \u00b7 ') : null,
      event: nextEventFor(inp.events, id, now),
    };
  };

  const al = inp.alerts;
  if (al.error && !al.data) items.push({ key: 'alerts-error', sev: 'info', title: `Alerts could not be loaded: ${al.error}`, action: { label: 'Retry', fn: inp.retryAlerts } });
  const unacked = al.data && Array.isArray(al.data.items) ? al.data.items.filter(a => !a.acked) : [];
  const alerted = new Set();
  unacked.slice(0, 5).forEach(a => {
    if (a.ticker && a.source === 'price') alerted.add(a.ticker);
    items.push({
      key: `alert-${a.id}`, sev: normSeverity(a.severity), title: a.title,
      detail: a.message && a.message !== a.title ? a.message : null,
      meta: fmtDateTime24(a.time),
      ...(a.ticker && positionOf(a.ticker) ? tickerFields(a.ticker) : { ticker: null }),
      action: { label: 'Open', fn: () => go(`#alerts/${encodeURIComponent(a.id)}`) },
    });
  });
  if (unacked.length > 5) items.push({ key: 'alerts-more', sev: 'info', title: `${unacked.length - 5} more unread alerts`, action: { label: 'Alerts', fn: () => go('#alerts') } });

  if (pf.error) {
    items.push({ key: 'pf-error', sev: d ? 'warn' : 'urgent', title: `Portfolio could not be refreshed: ${pf.error}${d ? ` (showing data from ${fmtDateTime24(pf.ts)})` : ''}`, action: { label: 'Retry', fn: inp.retryPortfolio } });
  }
  if (d) {
    (d.warnings || []).forEach((w, i) => {
      const single = Array.isArray(w.ids) && w.ids.length === 1 && positionOf(w.ids[0]);
      items.push({ key: `warn-${w.code || i}`, sev: 'warn', title: w.message || w.code, action: single ? open(w.ids[0]) : null });
    });
    if (d.fx && d.fx.stale) items.push({ key: 'fx-stale', sev: 'warn', title: `FX rate is stale (${d.fx.source || 'unknown source'}, as of ${fmtDateTime24(d.fx.asOf)}) \u2013 EUR values may be off.` });
    const missing = (d.totals && d.totals.costMissing) || [];
    const warned = (d.warnings || []).some(w => /cost/i.test(w.code || ''));
    if (missing.length && !warned) items.push({ key: 'cost-missing', sev: 'info', title: `Cost basis missing for ${missing.map(labelOf).join(', ')}; total P&L excludes it.`, action: { label: 'Settings', fn: () => settings('positions') } });
    for (const p of d.positions || []) {
      const name = p.label || p.id;
      const mv = alerted.has(p.id) ? null : classifyMove(p, rules, sigma);
      if (mv) {
        items.push({ key: `move-${p.id}`, sev: mv.sev, title: `${name} moved ${fmtPct(p.dayPct)} today${mv.z != null ? ` (z ${mv.z >= 0 ? '+' : '\u2212'}${Math.abs(mv.z).toFixed(1)})` : ` (threshold \u00b1${mv.limit}%)`}`, ...tickerFields(p.id), action: open(p.id) });
      }
      if (isNum(p.mddPct) && Math.abs(p.mddPct) >= MDD_MIN) {
        items.push({ key: `mdd-${p.id}`, sev: 'warn', title: `${name} is ${fmtPct(-Math.abs(p.mddPct), { dec: 1 })} from its 52-week high`, ...tickerFields(p.id), action: open(p.id) });
      }
      if (p.stale) items.push({ key: `stale-${p.id}`, sev: 'info', title: `${name}: price is stale (last tick ${fmtDateTime24(p.priceAsOf)})`, ...tickerFields(p.id), action: open(p.id) });
    }
  }

  const pol = inp.policy;
  if (pol && pol.configured) {
    const limit = pol.maxSingleStock && pol.maxSingleStock.limitPct;
    const breached = new Set((pol.maxSingleStock && pol.maxSingleStock.breaches) || []);
    for (const id of breached) {
      const cur = pol.current && pol.current[id];
      items.push({ key: `policy-max-${id}`, sev: 'warn', title: `${labelOf(id)} is above the single-stock limit${isNum(limit) ? ` of ${limit}%` : ''}${isNum(cur) ? ` (now ${fmtPct(cur, { sign: false, dec: 1 })})` : ''}`, ...tickerFields(id), action: open(id) });
    }
    for (const r of pol.drift || []) {
      if (r.status !== 'over' && r.status !== 'under') continue;
      if (breached.has(r.id)) continue;
      items.push({
        key: `policy-drift-${r.id}`, sev: 'warn',
        title: `${labelOf(r.id)} is ${r.status} its target band: ${fmtPct(r.currentPct, { sign: false, dec: 1 })} vs ${fmtPct(r.targetPct, { sign: false, dec: 0 })} (${fmtPct(r.bandLoPct, { sign: false, dec: 1 })}\u2013${fmtPct(r.bandHiPct, { sign: false, dec: 1 })})`,
        ...tickerFields(r.id), action: open(r.id),
      });
    }
  }

  for (const [id, a] of Object.entries(adviceMap)) {
    if (!a || !a.pending) continue;
    const pub = a.published && a.published.rating;
    items.push({
      key: `advice-pending-${id}`, sev: 'info',
      title: `${labelOf(id)}: advice pending ${a.pending.rating || '?'}${pub ? ` (published ${pub})` : ''}${a.pending.since ? ` since ${shortDate(a.pending.since)}` : ''}`,
      ...tickerFields(id), action: open(id),
    });
  }

  const lane = inp.lane && inp.lane.data;
  if (lane && lane.role === 'fallback') {
    items.push({ key: 'lane-fallback', sev: 'info', title: `AI answers are coming from the fallback model${lane.model ? ` (${lane.model})` : ''}.`, action: { label: 'AI Ops', fn: () => go('#aiops') } });
  }
  // Array.prototype.sort is stable: insertion order is kept within a severity.
  return items.sort((a, b) => SEV_ORDER[a.sev] - SEV_ORDER[b.sev]);
}

export function buildAttentionCard(it) {
  const top = el('div', { class: 'attn-top' },
    chip(SEV_TEXT[it.sev], `attn-sev ${SEV_CHIP[it.sev]}`),
    el('span', { class: 'attn-title' }, it.title, it.meta ? el('span', { class: 'attn-meta num', text: ` \u00b7 ${it.meta}` }) : null),
    it.deltaPct != null ? el('span', { class: 'attn-delta', title: 'Change today' }, deltaNode(it.deltaPct)) : null);
  const body = [];
  if (it.detail) body.push(el('p', { class: 'attn-detail muted', text: it.detail }));
  if (it.thesis) {
    const { head, rest } = splitAt(it.thesis, THESIS_CHARS);
    body.push(el('p', { class: 'attn-thesis' }, head,
      rest ? el('details', { class: 'attn-more' }, el('summary', { text: 'more' }), rest) : null));
  }
  if (it.why) body.push(el('p', { class: 'attn-why muted' }, el('span', { class: 'lbl', text: 'Why it matters' }), ' ', it.why));
  if (it.event) {
    body.push(el('p', { class: 'attn-next muted' }, el('span', { class: 'lbl', text: 'Next' }), ' ',
      `${it.event.text} on ${shortDate(it.event.date)}${it.event.days === 0 ? ' (today)' : ` (in ${it.event.days} d)`}`));
  }
  const open = it.action ? el('button', { type: 'button', class: 'btn btn-sm attn-open', onclick: it.action.fn, text: it.action.label }) : null;
  return el('li', { class: `attn-card sev-${it.sev}`, dataset: { key: it.key, ...(it.ticker ? { symbol: it.ticker } : {}) } },
    el('div', { class: 'attn-main' }, top, ...body), open);
}
