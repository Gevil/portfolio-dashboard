/* Market view: market light, benchmark, macro (curve/FRED/COT), earnings,
   insider Form 4, short volume, filings, worker rings.
   export function mount(root, ctx) -> unmount() */
import { createKit, isObj, num, str, fmtAgo, fmtDay, fmtNum, fmtPct, tone, errText } from './_kit.js';
import { summarizeWorkers, renderRings } from './_workers.js';

const LIGHT = { green: { cls: 'up', word: 'Green – risk-on' }, yellow: { cls: 'warn', word: 'Yellow – mixed' }, red: { cls: 'down', word: 'Red – risk-off' } };
const INSIDER_CODE = { P: ['purchase', 'up'], S: ['sale', 'down'], A: ['grant', 'flat'], F: ['tax', 'flat'], M: ['option ex.', 'flat'], G: ['gift', 'flat'], D: ['disposition', 'flat'] };

function maturityMonths(label) {
  const m = /^(\d+)\s*(Mo|Yr)/i.exec(label);
  return m ? Number(m[1]) * (/yr/i.test(m[2]) ? 12 : 1) : 1e9;
}
function safeUrl(u) {
  try { const x = new URL(String(u)); return x.protocol === 'https:' ? x.href : null; } catch (_) { return null; }
}

export function mount(root, ctx) {
  const kit = createKit(ctx, root);
  const { h, chip, svg } = kit;
  const grid = h('div', { class: 'v-grid v-market' });
  root.appendChild(grid);

  const watchSrc = kit.memo(() => kit.get('/api/watchlist'), 3000);
  const tickerIds = async () => {
    const r = await watchSrc();
    if (!r.ok) return { ok: false, ids: [], error: r.error };
    const es = Array.isArray(r.data) ? r.data : (r.data && Array.isArray(r.data.entries) ? r.data.entries : []);
    return { ok: true, ids: es.filter(e => e && e.kind !== 'index' && e.role !== 'benchmark' && e.analyzeable !== false).map(e => str(e.id || e.symbol)).filter(Boolean) };
  };

  /* ---------------- market light ---------------- */
  const light = kit.section({ title: 'Market light', id: 'light', hint: 'breadth · index · momentum' });
  kit.autoLoad(light, {
    source: '/api/market-light',
    isEmpty: d => !isObj(d) || !d.status,
    emptyText: 'No market-light snapshot yet. It is built once a day after the close from watchlist breadth, the index vs its 200-day average and 5-day momentum.',
    render: (body, j) => {
      const L = LIGHT[String(j.status).toLowerCase()] || { cls: 'flat', word: str(j.status) };
      body.appendChild(h('div', { class: 'v-light' },
        h('span', { class: 'v-light__dot v-light__dot--' + L.cls, 'aria-hidden': 'true' }),
        h('div', {},
          h('div', { class: 'v-light__word' }, L.word),
          h('div', { class: 'v-muted' }, (num(j.score) !== null ? 'score ' + Math.round(j.score) : '') + (j.date ? ' · ' + fmtDay(j.date) : ''),
            j.data_quality === 'limited' ? ' ' : null, j.data_quality === 'limited' ? chip('limited data', 'warn') : null))));
      const dims = isObj(j.dimensions) ? Object.entries(j.dimensions) : [];
      if (dims.length) {
        const ul = h('ul', { class: 'v-dims' });
        dims.forEach(([name, d]) => {
          if (!isObj(d) || d.available === false) { ul.appendChild(h('li', {}, h('span', { class: 'v-label' }, name), chip('n/a', 'flat'))); return; }
          const L2 = LIGHT[String(d.status).toLowerCase()];
          ul.appendChild(h('li', {}, h('span', { class: 'v-label' }, name), chip(str(d.status, '–'), L2 ? L2.cls : 'flat'), d.detail ? h('span', { class: 'v-muted' }, str(d.detail)) : null));
        });
        body.appendChild(ul);
      }
      const reasons = Array.isArray(j.reasons) ? j.reasons : [];
      if (reasons.length) {
        body.appendChild(h('h3', { class: 'v-h3' }, 'Why'));
        const ul = h('ul', { class: 'v-bullets' });
        reasons.slice(0, 5).forEach(r => ul.appendChild(h('li', {}, str(r))));
        body.appendChild(ul);
      }
    },
  }, 300000);

  /* ---------------- benchmark ---------------- */
  const bench = kit.section({ title: 'Benchmark', id: 'bench', hint: 'for comparison only' });
  kit.autoLoad(bench, {
    source: '/api/portfolio',
    isEmpty: d => !isObj(d) || !isObj(d.benchmark),
    emptyText: 'No benchmark quote available.',
    render: (body, p) => {
      const b = p.benchmark;
      const t = isObj(p.totals) ? p.totals : {};
      const diff = num(t.dayPnlPct) !== null && num(b.dayPct) !== null ? t.dayPnlPct - b.dayPct : null;
      body.appendChild(h('div', { class: 'v-bench' },
        h('div', { class: 'v-bench__big v-' + tone(b.dayPct) }, fmtPct(b.dayPct, 2, true)),
        h('div', { class: 'v-muted' }, str(b.label || b.id, 'Benchmark') + ' · day' + (b.asOf ? ' · ' + fmtAgo(b.asOf) : ''))));
      const dl = h('dl', { class: 'v-kv v-kv--row' },
        h('dt', {}, 'Portfolio today'), h('dd', { class: 'v-' + tone(t.dayPnlPct) }, fmtPct(t.dayPnlPct, 2, true)),
        h('dt', {}, 'vs benchmark'), h('dd', { class: 'v-' + tone(diff) }, diff === null ? '–' : fmtPct(diff, 2, true) + ' pts'));
      body.appendChild(dl);
    },
  }, 60000);

  /* ---------------- macro ---------------- */
  const macro = kit.section({ title: 'Macro', id: 'macro', hint: 'US Treasury curve · FRED · CFTC', cls: 'v-span-2' });
  kit.autoLoad(macro, {
    source: '/api/macro',
    isEmpty: d => !isObj(d) || (!isObj(d.curve) && !isObj(d.fred) && !(Array.isArray(d.cot) && d.cot.length)),
    emptyText: 'Yield curve not cached yet – it refreshes daily at startup.',
    render: (body, m) => {
      const wrap = h('div', { class: 'v-split' });
      body.appendChild(wrap);
      const curve = isObj(m.curve) && isObj(m.curve.yields) ? m.curve : null;
      const left = h('div', { class: 'v-block' }, h('h3', { class: 'v-h3' }, 'UST yield curve' + (curve && curve.date ? ' · ' + fmtDay(curve.date) : '')));
      if (curve) {
        const pts = Object.entries(curve.yields).filter(([, v]) => num(v) !== null).sort((a, b) => maturityMonths(a[0]) - maturityMonths(b[0]));
        if (pts.length > 1) left.appendChild(curveSvg(pts));
        const inv = num(curve.spread2s10s);
        left.appendChild(h('p', { class: 'v-summary' }, '2s10s spread ',
          h('span', { class: 'v-' + tone(inv) }, inv === null ? '–' : (inv >= 0 ? '+' : '') + inv.toFixed(2) + ' pp'),
          inv !== null && inv < 0 ? chip('inverted', 'warn') : null));
        const rows = pts.map(([k, v]) => h('tr', {}, h('th', { scope: 'row' }, k), h('td', { class: 'v-num' }, Number(v).toFixed(2) + '%')));
        left.appendChild(kit.table('Treasury yields by maturity', [{ label: 'Maturity' }, { label: 'Yield', num: true }], rows));
      } else {
        left.appendChild(kit.empty('Yield curve not cached yet – refreshed daily at startup.'));
      }
      const right = h('div', { class: 'v-block' });
      const fredEntries = isObj(m.fred) ? Object.entries(m.fred).filter(([, v]) => Array.isArray(v) && v.length) : [];
      if (fredEntries.length) {
        right.appendChild(h('h3', { class: 'v-h3' }, 'FRED'));
        const dl = h('dl', { class: 'v-kv v-kv--row' });
        fredEntries.forEach(([k, v]) => {
          dl.append(h('dt', {}, k), h('dd', {}, num(v[0].value) !== null ? Number(v[0].value).toFixed(2) + '%' : '–', h('span', { class: 'v-muted' }, ' ' + fmtDay(v[0].date))));
        });
        right.appendChild(dl);
      } else if (isObj(m.status) && m.status.fred_key === false) {
        right.appendChild(h('p', { class: 'v-muted' }, 'FRED series are off (no API key configured).'));
      }
      const cot = Array.isArray(m.cot) ? m.cot : [];
      right.appendChild(h('h3', { class: 'v-h3' }, 'CFTC COT · S&P 500 futures'));
      if (cot.length) {
        const rows = cot.slice(0, 5).map(r => {
          const net = num(r.noncomm_long) !== null && num(r.noncomm_short) !== null ? r.noncomm_long - r.noncomm_short : null;
          return h('tr', {}, h('th', { scope: 'row' }, fmtDay(r.date)),
            h('td', { class: 'v-num' }, fmtNum(r.open_interest)),
            h('td', { class: 'v-num v-' + tone(net) }, net === null ? '–' : (net > 0 ? '+' : '') + fmtNum(net)),
            h('td', { class: 'v-muted v-note', title: str(r.market) }, str(r.market).slice(0, 28)));
        });
        right.appendChild(kit.table('Commitments of traders', [{ label: 'Week' }, { label: 'Open interest', num: true }, { label: 'Non-comm. net', num: true }, { label: 'Market' }], rows));
      } else {
        right.appendChild(kit.empty('No COT rows cached yet (weekly, published Fridays).'));
      }
      wrap.append(left, right);
    },
  }, 300000);

  function curveSvg(pts) {
    const W = 320, H = 110, P = 16;
    const vals = pts.map(p => Number(p[1]));
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = hi - lo || 1;
    const xy = pts.map((p, i) => [P + (i * (W - 2 * P)) / (pts.length - 1), H - P - ((Number(p[1]) - lo) / span) * (H - 2 * P)]);
    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'v-curve', role: 'img', 'aria-label': 'Treasury yield curve from ' + pts[0][0] + ' to ' + pts[pts.length - 1][0] });
    root.appendChild(svg('polyline', { class: 'v-curve__line', fill: 'none', points: xy.map(p => p.join(',')).join(' ') }));
    xy.forEach((p, i) => {
      const c = svg('circle', { class: 'v-curve__pt', cx: p[0], cy: p[1], r: 2.5 });
      c.appendChild(svg('title', {}, document.createTextNode(pts[i][0] + ': ' + Number(pts[i][1]).toFixed(2) + '%')));
      root.appendChild(c);
    });
    [0, pts.length - 1].forEach(i => {
      const t = svg('text', { class: 'v-curve__lbl', x: xy[i][0], y: H - 2, 'text-anchor': i ? 'end' : 'start' }, document.createTextNode(pts[i][0]));
      root.appendChild(t);
    });
    return root;
  }

  /* ---------------- earnings ---------------- */
  const earn = kit.section({ title: 'Earnings calendar', id: 'earn' });
  kit.autoLoad(earn, {
    source: '/api/earnings',
    isEmpty: d => !isObj(d) || !isObj(d.earnings) || !Object.keys(d.earnings).filter(k => k !== '_fetched').length,
    emptyText: 'No upcoming earnings dates cached for the watchlist.',
    render: (body, d) => {
      const rows = Object.entries(d.earnings).filter(([k]) => k !== '_fetched')
        .map(([sym, v]) => ({ sym, it: Array.isArray(v) ? (v[0] || {}) : (isObj(v) ? v : {}) }))
        .sort((a, b) => str(a.it.date).localeCompare(str(b.it.date)));
      const today = new Date(); today.setHours(0, 0, 0, 0);
      const trs = rows.map(({ sym, it }) => {
        const dt = it.date ? new Date(it.date + 'T00:00:00') : null;
        const days = dt && !Number.isNaN(dt.getTime()) ? Math.round((dt - today) / 86400000) : null;
        const soon = days !== null && days >= 0 && days <= 7;
        return h('tr', { class: soon ? 'v-row--soon' : null },
          h('th', { scope: 'row', class: 'v-mono' }, sym),
          h('td', {}, it.date ? fmtDay(it.date) : '–', days !== null ? h('span', { class: 'v-muted' }, days === 0 ? ' · today' : days > 0 ? ' · in ' + days + ' d' : ' · ' + (-days) + ' d ago') : null),
          h('td', { class: 'v-muted' }, str(it.time, '–')),
          h('td', { class: 'v-num' }, it.epsForecast ? 'EPS est ' + it.epsForecast : '–'));
      });
      body.appendChild(kit.table('Next earnings dates', [{ label: 'Ticker' }, { label: 'Date' }, { label: 'Time' }, { label: 'Forecast', num: true }], trs));
    },
  }, 600000);

  /* ---------------- insider ---------------- */
  const ins = kit.section({ title: 'Insider Form 4', id: 'insider', hint: 'US issuers only' });
  kit.autoLoad(ins, {
    source: '/api/insider',
    isEmpty: d => !isObj(d) || !Array.isArray(d.entries) || !d.entries.length,
    emptyText: 'No Form 4 filings in the recent window. US issuers only – ASML is a foreign private issuer (6-K/20-F, no insider forms).',
    render: (body, d) => {
      const trs = [];
      d.entries.slice(0, 12).forEach(en => (Array.isArray(en.trades) ? en.trades : []).slice(0, 4).forEach(t => {
        const code = INSIDER_CODE[t.code] || [str(t.code, '?'), 'flat'];
        trs.push(h('tr', {},
          h('th', { scope: 'row', class: 'v-mono' }, str(en.ticker || en.symbol, '?')),
          h('td', {}, chip(code[0], code[1])),
          h('td', {}, str(en.insider, 'insider'), en.officer ? h('span', { class: 'v-muted' }, ' (' + en.officer + ')') : null),
          h('td', { class: 'v-num' }, fmtNum(Math.round(num(t.shares) || 0)) + ' sh', num(t.price) ? ' @ $' + fmtNum(t.price, 2) : ''),
          h('td', { class: 'v-num' }, num(t.notional) !== null ? '$' + fmtNum(Math.round(t.notional)) : '–'),
          h('td', { class: 'v-muted' }, t.date ? fmtDay(t.date) : '–')));
      }));
      body.appendChild(kit.table('Recent insider transactions', [{ label: 'Ticker' }, { label: 'Type' }, { label: 'Insider' }, { label: 'Shares', num: true }, { label: 'Value', num: true }, { label: 'Date' }], trs));
    },
  }, 600000);

  /* ---------------- short volume ---------------- */
  const sv = kit.section({ title: 'Short volume', id: 'short', hint: 'FINRA daily short ratio, US listings' });
  kit.autoLoad(sv, {
    source: async () => {
      const t = await tickerIds();
      if (!t.ok) return { ok: false, error: t.error };
      const res = await Promise.all(t.ids.slice(0, 8).map(async id => ({ id, r: await kit.get('/api/shortvolume/' + encodeURIComponent(id)) })));
      const rows = res.filter(x => x.r.ok && isObj(x.r.data) && Array.isArray(x.r.data.series) && x.r.data.series.length)
        .map(x => ({ id: x.id, series: x.r.data.series, latest: x.r.data.latest }));
      if (!rows.length && res.length && res.every(x => !x.r.ok)) return { ok: false, error: errText(res[0].r.error), status: res[0].r.status };
      return { ok: true, data: rows };
    },
    isEmpty: d => !d.length,
    emptyText: 'No short-volume data. FINRA covers US-listed names only; EU listings have none.',
    render: (body, rows) => {
      const ul = h('ul', { class: 'v-sv' });
      rows.forEach(r => {
        const ratios = r.series.slice(-30).map(x => num(x.ratio)).filter(v => v !== null);
        const pct = (v) => (v <= 1 ? v * 100 : v);
        const last = r.latest ? num(r.latest.ratio) : null;
        const avg = ratios.length ? ratios.reduce((a, b) => a + b, 0) / ratios.length : null;
        ul.appendChild(h('li', {},
          h('span', { class: 'v-mono v-strong' }, r.id),
          h('span', { class: 'v-num' }, last === null ? '–' : fmtPct(pct(last), 1)),
          avg !== null && last !== null ? h('span', { class: 'v-muted' }, '30-day avg ' + fmtPct(pct(avg), 1)) : null,
          ratios.length > 1 ? spark(ratios, r.id) : null,
          r.latest && r.latest.date ? h('span', { class: 'v-muted' }, fmtDay(r.latest.date)) : null));
      });
      body.appendChild(ul);
    },
  }, 600000);

  function spark(vals, id) {
    const W = 90, H = 24;
    const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
    const pts = vals.map((v, i) => [(i * (W - 2)) / (vals.length - 1) + 1, H - 2 - ((v - lo) / span) * (H - 4)]);
    const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'v-spark', role: 'img', 'aria-label': id + ' short ratio, last ' + vals.length + ' days' });
    s.appendChild(svg('polyline', { class: 'v-spark__line', fill: 'none', points: pts.map(p => p.join(',')).join(' ') }));
    return s;
  }

  /* ---------------- filings ---------------- */
  const fil = kit.section({ title: 'Filings', id: 'filings', hint: 'SEC watchlist events', cls: 'v-span-2' });
  kit.autoLoad(fil, {
    source: '/api/filings?limit=15',
    isEmpty: d => !isObj(d) || !Array.isArray(d.events) || !d.events.length,
    emptyText: 'No recent filings for the watchlist.',
    render: (body, d) => {
      const trs = d.events.slice(0, 15).map(e => {
        const url = safeUrl(e.url);
        const label = str(e.company || e.title, '–');
        return h('tr', {},
          h('td', { class: 'v-muted' }, e.filed ? fmtDay(e.filed) : '–'),
          h('th', { scope: 'row', class: 'v-mono' }, str(e.ticker || e.symbol, '?')),
          h('td', {}, chip(str(e.form, '?'), 'flat'), e.keyword ? h('span', { class: 'v-muted' }, ' ' + e.keyword) : null),
          h('td', {}, url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'v-link' }, label) : label));
      });
      body.appendChild(kit.table('Recent filings', [{ label: 'Filed' }, { label: 'Ticker' }, { label: 'Form' }, { label: 'Company' }], trs));
    },
  }, 600000);

  /* ---------------- workers ---------------- */
  const wk = kit.section({ title: 'Worker runs', id: 'workers', hint: 'one dot per recent run', cls: 'v-span-2' });
  kit.autoLoad(wk, {
    source: async () => {
      const [bg, runs] = await Promise.all([kit.get('/api/background'), kit.get('/api/worker-runs?limit=200')]);
      if (!bg.ok && !runs.ok) return { ok: false, error: errText(bg.error), status: bg.status };
      return { ok: true, data: { bg: bg.ok ? bg.data : null, runs: runs.ok && Array.isArray(runs.data) ? runs.data : [] } };
    },
    isEmpty: () => false,
    render: (body, d) => renderRings(kit, body, summarizeWorkers(d.bg, d.runs)),
  }, 60000);

  grid.append(light.el, bench.el, macro.el, earn.el, ins.el, sv.el, fil.el, wk.el);
  return () => kit.dispose();
}
