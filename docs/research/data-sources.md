# Data sources to make the EU large-cap data more accurate and stable

Probed 2026-10-21 (~10:40-10:55 UTC) from the Bazzite host, public keyless HTTP only. No API key was read or used. Sample names: SAP (`SAP.DE`), ASML (`ASML.AS`), Siemens (`SIE.DE`), LVMH (`MC.PA`), NVIDIA (`NVD.DE`). `[UNVERIFIED]` = not observed. `[INFERENCE]` = my reading. P# = appendix row.

## 1. Bottom line
- **No free, ToS-clean second EOD source for EU equities was usable.** Stooq's robots.txt disallows all generic agents (P1). Euronext's terms forbid spiders and robots without written permission ([terms](https://www.euronext.com/en/terms-use)). Deutsche Börse's Xetra public S3 dataset returns 403 (P2).
- **The cheapest legitimate cross-check is paid:** EODHD EOD All-World, €19.99/month. Twelve Data's EU exchanges need Grow (about $29/month, EOD only) ([exchanges](https://twelvedata.com/exchanges)). Both need a key, so I could not test them. The owner must approve the spend.
- **The biggest accuracy gap is in our own pipeline.** Returns use raw closes (`auto_adjust=False`, `prices.py:651,674`), so dividends and splits distort momentum and returns.
- **Free sources that work:** ECB Data Portal, FRED CSV, ECB press RSS, OpenFIGI, ESMA FIRDS index, AFM CSV exports, AMF BDIF JSON API. Registers cover only a minority of our names (section 3.6).
- **Dead ends:** VSTOXX (no free series), HICP (Eurostat and ECB stop at 2025-12), BaFin (robots disallow all), AFM MAR export (no amounts).

## 2. Evidence from our repo (read-only)
- The ASML packet `adhoc-2026-10-20` has `price.stale: true`: close dated 2026-10-19 against an as-of of 2026-10-20 17:30 CEST. `momentum12_1` is on a "raw close" basis. `nextEarnings` is `MISSING`.
- `data/universe_news/earnings.jsonl` (2026-10-21, 127 ids): 31 null dates (XAMS 15, XPAR 11, XETR 5), including ASML and MC. 2 universe ids were never observed. One day of data, so stability is unknown.
- Analyst fields (`prices.py:1342-43`) are fetched for watchlist holdings only, and the cache is overwritten. No point-in-time history exists.


## 3. Findings by topic

### 3.1 EOD prices, second source, adjustments
| Source | Probe result | Limits, licence | 5-name coverage |
|---|---|---|---|
| Stooq CSV | robots `Disallow: /` for `*` (P1); not fetched | n/a | n/a |
| Euronext, Börse Frankfurt | ToS forbids robots; not fetched | n/a | n/a |
| Twelve Data | Keyless `/exchanges` and `/stocks` worked: XETR present, `SAP SE` listed (P3). XETR/XAMS/XPAR/XBRU need Grow, XMIL needs Pro | Free: 8 credits/min, 800/day, internal use only ([pricing](https://twelvedata.com/pricing)) | SAP verified in the symbol list; others follow from the exchange list. Free tier covers none |
| EODHD | Not probed (key). Docs: `SAP.XETRA`, `AIR.PA`; `adjusted_close`; dividends with `date/declarationDate/recordDate/paymentDate/value/unadjustedValue` ([EOD](https://eodhd.com/financial-apis/api-for-historical-data-and-volumes), [dividends](https://eodhd.com/financial-apis/api-splits-dividends)) | Free 20 calls/day ([limits](https://eodhd.com/financial-apis/api-limits)); €19.99 EOD, €29.99 EOD+intraday ([pricing](https://eodhd.com/pricing)); personal licence exists | SAP.XETRA in docs; four others `[UNVERIFIED]` |
| Marketstack | Pricing page only | Free: 100 req/month, 1 yr, splits and dividends, non-commercial. $9.99: 10k req/month ([product](https://marketstack.com/product)) | `[UNVERIFIED]` |
| Alpha Vantage | Pricing page only | 25 requests/day ([premium](https://www.alphavantage.co/premium/)); useless for 127 names | n/a |

**Adjustment handling:** keep the raw close for price and valuation, and store the adjusted close separately for returns and momentum. Cross-check on raw close with 0.5% tolerance. A ratio near an integer or simple fraction suggests a split, which is a reconciliation event and not a price move [INFERENCE]. Yahoo stays primary. Its robots.txt disallows `*` (P4), and I made no Yahoo request.

### 3.2 Intraday or delayed quotes beyond Yahoo
None free and ToS-clean. Paid only: EODHD Extended €29.99 (depth `[UNVERIFIED]`) and Twelve Data's Cboe Europe add-on (price `[UNVERIFIED]`). Keep Yahoo plus a freshness guard (item 1).

### 3.3 Corporate actions and identity
- EODHD's dividends and splits API is the only structured ex-date source I found. ECB, Eurostat and ESMA carry none.
- **OpenFIGI** (keyless POST, P5): `DE0007164600` maps to `SAP GR/GF/GD/SW`, `NL0010273215` to `ASML NA`, `US67066G1040` with `exchCode GY` to `NVD GY`. 1.1 s for three ISINs. It supports ISIN-keyed identity checks for ticker or listing changes.
- **ESMA FIRDS** Solr index (P6): 70 files since 2026-10-14, daily `DLTINS_*.zip` on `firds.esma.europa.eu`. Reference data only.

### 3.4 Earnings dates
The yfinance calendar is the current source (null for 31/127, section 2). The Nasdaq calendar is US-only. EODHD's calendar returns `report_date`, `before_after_market`, `estimate`, `actual` ([docs](https://eodhd.com/financial-apis/calendar-upcoming-earnings-ipos-and-splits)); EU coverage and plan inclusion are `[UNVERIFIED]`. Euronext's calendar is ToS-blocked.

### 3.5 Macro and regime
| Series | Endpoint | Probe | Notes |
|---|---|---|---|
| EUR/USD | ECB `data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?format=csvdata` | 200, 44 ms; 1.1215 on 2026-10-20 | 14:15 CET reference; Frankfurter mirrors it |
| Policy rates | `FM/B.U2.EUR.4F.KR.MRR_FR.LEV`, `...DFR.LEV` | 200, 121 ms; MRO 2.40 to 2.65, DFR 2.25 to 2.50 on 2026-09-16 | Hike regime; change dates are event features |
| €STR | `EST/B.EU000A2X2A25.WT` | 200; 2.44 on 2026-10-20 | |
| AAA 10Y | `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y` | 200, 43 ms; 3.44 on 2026-10-20 | |
| VIXCLS, DGS10 | `fred.stlouisfed.org/graph/fredgraph.csv?id=` | 200, 0.27 s with default curl UA; VIX 18.44, DGS10 5.15 on 2026-10-19 | A `Mozilla/...` UA hung or reset (exit 28/92, P7). robots Crawl-delay 1. VIX is Cboe-copyright, reprinted with permission |
| HICP | Eurostat `prc_hicp_manr`; ECB `ICP/M.U2.N.000000.4.ANR` | 200, but both end at 2025-12 (2.0 vs 1.9). `une_rt_m` was updated 2026-10-21 | Unusable live. A 2026 classification change is my guess, unconfirmed |
| VSTOXX | `www.stoxx.com` | TLS verify failure, curl exit 60 (P8); `stoxx.com` search returns articles only | No free series. Use realised vol from our own index closes [INFERENCE] |
| Sector indices | none | ECB equity-index series 404 | Xetra sector UCITS ETFs through the existing price path [INFERENCE, unprobed] |

Licence: ECB statistics are free to reuse with the source cited and data unmodified ([policy](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)). Eurostat reuse needs source acknowledgement ([notice](https://ec.europa.eu/eurostat/web/main/help/copyright-notice)). ECB rate limit `[UNVERIFIED]`.

### 3.6 Regulator registers
| Register | Format and probe | Coverage of our names | Licence and risk |
|---|---|---|---|
| **AFM net short, current** | `afm.nl/export.aspx?type=8a46a4ef-f196-4467-a7ab-1ae1cb58f0e7&format=csv`; 200, 55 ms, 68 KB, 746 rows; `;`-delimited UTF-8 BOM; holder, issuer, ISIN, net short %, date; latest 2026-10-19 | Positions of 0.5% or more only. 9 of 129 names have rows (AKZA, DSFIR, IMCD, BESI, MAGN, AD, ARGX, UMG, PHIA). ASML: 0. SAP, SIE, MC, NVDA: outside AFM jurisdiction | robots allow all but `/sitecore`. © AFM; no open-data licence found `[UNVERIFIED]`. The export GUID could change |
| AFM net short, archive | `type=3ca31b3d-...`; 200, 0.33 s, 2 MB, 21,711 rows; not valid UTF-8 | Same | Same |
| **AFM MAR 19** | `type=0ee836dc-...`; 200, 0.18 s, 674 KB, 9,563 rows (2005-12-31 to 2026-10-20). Four columns only: date, issuer, notifier, surname. No ISIN, side, amount, price | 132 issuers in 20 years. ASML, Philips, SAP, Siemens, LVMH, NVIDIA: 0 rows. About 24 universe names match by name | Too thin to use as a signal |
| **BaFin Directors' Dealings** | `portal.mvp.bafin.de/database/DealingsInfo/`; robots `Disallow: /` for all agents (P9) | n/a | **Not usable by automation.** I fetched the landing page twice before reading robots; nothing more. BaFin short data `[UNVERIFIED]` |
| **AMF BDIF** | `bdif.amf-france.org/back/api/v1/informations?TypesInformation=DD\|VAD&RechercheTexte=&DateDebut=&From=&Size=`; 200, 0.12-0.28 s; metadata plus PDF path, amounts only in the PDF. 27,842 DD documents; 260 VAD (net short) notices since 2026-10-01 | Filter by legal name in `RechercheTexte` ("TotalEnergies": 83 DD since 2026-07-01; "LVMH": 0) or by `Jetons`. `Isin=` is silently ignored. Universe names with VAD notices: SGO, TTE, ENGI | Undocumented API found in the SPA bundle (breakage risk). No robots.txt (SPA fallback). Reuse terms `[UNVERIFIED]` |
| **ESMA** | FIRDS index only (3.3). The AFM page links ESMA's short-selling exemptions register, not positions | n/a | Reuse terms `[UNVERIFIED]` |

All keyless endpoints answered in 0.04-1.3 s. I stayed near 1 request/s. Published limits exist only for EODHD (20/day free, 1,000/min). Reliability risk, highest first: AMF API, AFM GUID exports, FRED CSV, ECB and ESMA.

### 3.7 Analyst consensus
Our code reads `targetMeanPrice` and `recommendationKey`; the library's estimate-count fields are `[UNVERIFIED]` (no Yahoo probe). Yahoo keeps no history of estimates, so **a daily snapshot is the only way to get a point-in-time series**. It is viable: 127 names at 1-3 calls/day, append-only JSONL with `fetchedAt` like `earnings.jsonl`. Revision features need at least 60 sessions first [INFERENCE]. EU mid-cap coverage is sparse, so store the analyst count and treat small n as MISSING. Twelve Data's `analystRatings` (`twelvedata.py:276-285`) is plan-gated.

### 3.8 News quality
- **ECB press RSS** `ecb.europa.eu/rss/press.xml`: 200, 34 ms, items dated 2026-10-21. Free macro-event headlines.
- **Regulatory-event flags** from AMF BDIF DD/VAD metadata and the AFM exports. These are primary-source and need only a name match, so they can tag existing items as "insider filing" or "short notice".
- No free source found that replaces yfinance, Google RSS, GDELT or Finnhub (US ISINs only).

## 4. Ranked build list (forecast value x effort)
1. **Total-return and staleness fix (no new source, a few hours).** In `paper_universe.daily_closes` and `prices._yf_bars_blocking`, keep `auto_adjust=False` and also store `Adj Close`. Compute `momentum12_1` and returns in `forecast_packets` from adjusted closes; keep raw closes for price and valuation. Make `stale` a gate: a close older than the last session blocks the packet.
2. **ECB and FRED regime feed (free, about half a day).** New `app/api/providers/ecb.py` (CSV, 1 req/s, source string). `macro.py` adds MRO/DFR with last-change date, €STR, AAA 10Y. `forex.py` gets the ECB reference as a third provider. Add a keyless FRED CSV fallback (plain UA, 1 s spacing) behind the keyed call. `forecast_packets` extends `regime`. Skip HICP and VSTOXX.
3. **EOD cross-check worker (EODHD €19.99; needs owner approval and a key in `env.secrets`).** New `providers/eodhd.py` and a daily `price_xcheck.py` in `main.BACKGROUND_MODULES`. One pass compares the universe's last close with the yfinance raw close (0.5% tolerance, split detection), logs mismatches to `runlog`/`ops_watch`, and blocks the packet on a hard mismatch. Test the 5 names first and check EU coverage before buying a longer plan.
4. **Analyst-consensus snapshot log (free, about 2 hours).** A daily job in `universe_news.py` (or a new `consensus_log.py`) writing `data/consensus/<YYYY-MM>.jsonl` with `{id, targetMean, recKey, nAnalysts, fetchedAt}`. Nothing reads it until the week-12 readout.
5. **Earnings-date robustness (free, about 2 hours).** In `universe_news._yf_calendar_blocking`, carry the last observed future date forward until it passes (`source: carried`). Add a null-rate metric to `ops_watch`. If item 3 goes ahead, add EODHD's calendar as a second observation.
6. **Regulatory-event flags (free, about 1 day, partial coverage).** New `app/api/regulators.py`: AFM short CSV (daily, cp1252-safe, ISIN-keyed, 9 names) and BDIF DD/VAD metadata (resolve `Jetons` once per name). Append typed, URL-free rows to `universe_news` with `source: regulator`; `pit_news` uses them as tags only.
7. **Identity audit (free, about 2 hours, monthly).** OpenFIGI check of each registry ISIN against its Yahoo symbol and venue, reporting to `ops_watch`.

Not building: Stooq, Euronext, Börse Frankfurt (ToS/robots), BaFin (robots), AFM MAR export, HICP, VSTOXX, Xetra PDS (403).

**Owner decisions:** approve €19.99/month for EODHD (item 3).

## Appendix: probe log (2026-10-21 UTC; one request unless noted)
| P | Target | Result |
|---|---|---|
| 1 | stooq.com/robots.txt | 200, 0.17 s; `*` disallowed |
| 2 | deutsche-boerse-xetra-pds S3 list | 403 |
| 3 | api.twelvedata.com `/exchanges`, `/stocks?symbol=SAP` | 200, 0.28/0.44 s; `isin` gated |
| 4 | query1.finance.yahoo.com/robots.txt | 200, 0.46 s; `Disallow: /` |
| 5 | api.openfigi.com/v3/mapping (3 ISINs) | 200, 1.14 s |
| 6 | registers.esma.europa.eu Solr FIRDS | 200; 70 files |
| 7 | fredgraph.csv (6 requests) | Mozilla UA exit 92/28; default UA 200, 0.27-0.48 s |
| 8 | www.stoxx.com (x3); stoxx.com robots, wp-json | exit 60; 200 |
| 9 | portal.mvp.bafin.de (2 GETs), robots | 200; `Disallow: /` |

| 11 | data-api.ecb.europa.eu: EXR, FM x3, YC, EST, ICP, equity-index | 200, 0.04-0.37 s; equity-index 404 |
| 12 | Eurostat: `prc_hicp_manr` x5, `une_rt_m`, `ei_bsco_m` | 200; HICP ends 2025-12 |
| 13 | afm.nl: 3 exports, pages, robots | 200, 0.05-0.33 s |
| 14 | bdif.amf-france.org: referentiels, informations x6, about 25 JS assets | 200, 0.12-1.28 s |

| 16 | Vendor docs: twelvedata, eodhd (5), marketstack (3), alphavantage, finnhub (3), euronext terms | 200, except marketstack docs 302 and coverage page 404 |
