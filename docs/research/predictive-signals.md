# Predictive signals at 1–20 trading days: evidence and EU large-cap data availability

Tags: **[INFERENCE]** is my own reasoning. **[UNVERIFIED]** means I could not fetch a primary source, so the claim comes from memory or a search snippet. Where I cite an abstract only, I say so. Fetched 2026-10-20.

## 0. Evidence quality up front

- Most findings are US, monthly-rebalanced, and measured before costs. I found **no** fetched, independent replication of the LLM-news result on EU large caps.
- Anomalies decay after publication. McLean & Pontiff measured about 50% post-publication decay across 56 characteristics (SSRN 2012 abstract). Chordia, Subrahmanyam & Tong find average anomaly returns roughly halved after US decimalization, and link the decline to hedge-fund AUM, short interest and turnover (SSRN 2012 abstract).
- These effects are strongest in small, illiquid stocks. Large caps are the hardest cell, and EU large caps like ASML, NVDA on Xetra and SXR8 are heavily arbitraged. **[INFERENCE]** Treat every effect size below as an upper bound for our universe.
- Search tools were degraded and the headless browser was missing in this session. Several registers (AFM, BaFin, ESMA, STOXX, Finnhub docs) could not be fetched. Those rows are marked **[UNVERIFIED]**.

## 1. Signal-by-signal evidence

| Signal | Effect size (source) | Horizon | Decay | Cost sensitivity | EU large-cap fit |
|---|---|---|---|---|---|
| **LLM news sentiment** | GPT-4 headline score gives a long-short return of 34 bps/day before costs. Sample is 4,123 US stocks, Oct 2021–May 2024, with post-cutoff headlines only. Hit rate is about 90% for the *initial* reaction, which is not tradable. Drift is concentrated in small stocks and negative news [LLT25] | Next 1–2 days | Annualised Sharpe fell from 6.54 (2021Q4) to 3.68 (2022), 2.33 (2023) and 1.22 (Jan–May 2024) [LLT25] | Very high. The authors say it is feasible only for participants with very low costs, e.g. market makers [LLT25] | Untested on EU. Performance rises with model size, and small/weak models showed no drift predictability [LLT25]. **[INFERENCE]** A local 27B model sits below GPT-4 on this ladder. |
| LLM vs dictionary sentiment | Next-day regression coefficients of 0.274 and 0.254 for OPT and BERT/FinBERT scores. Long-short Sharpe is 3.05 (OPT), 2.11 (BERT), 2.07 (FinBERT) and 1.23 (Loughran-McDonald). Sample is 965k US articles, 2010–2023. Backtest overlaps training data, so look-ahead is likely [KG24] | Next day | Not studied | High | Untested on EU |
| **Look-ahead bias in LLM backtests** | Anonymising headlines made in-sample results *better*, so the "distraction effect" outweighs look-ahead. Out-of-sample look-ahead is not a concern, but distraction remains [GL23]. Lookahead Propensity collapses to about zero after the training cutoff, and LLM predictive power is amplified on high-LAP firm-dates [GJY26] | n/a | n/a | n/a | Any backtest before the model's cutoff is contaminated |
| Text-model news sentiment (non-LLM) | Supervised sentiment from Dow Jones Newswires "excels at extracting return-predictive signals" (abstract only, no numbers fetched) [KKX19]. Media pessimism predicts short-run price pressure followed by reversion [T07] | 1–few days | Reversion, per [T07] | High | **[UNVERIFIED]** for EU |
| **Post-earnings drift (PEAD)** | Classic result: roughly 8–9% per quarter for zero-investment SUE portfolios in 1974–86 [Wiki-PEAD, citing Bernard & Thomas 1990]. The top-minus-bottom SUE spread fell from about 5% (1980s–90s) to 3% or lower by the late 2010s [Wiki-PEAD, secondary]. Kettell et al. 2022 attribute the decline to falling SUE persistence [Wiki-PEAD]. A text-based variant (PEAD.txt) reportedly exceeds classic PEAD [Wiki-PEAD, citing Meursault et al. 2021] | 1–60 days. About 25–30% of the drift falls in the next three announcement windows [Wiki-PEAD] | Large. International PEAD fell after IFRS-era reporting improvements [HLW15] | Moderate. The signal is event-driven, so turnover is low. | Evidence is weaker for large caps. It is stronger for illiquid names with low analyst coverage [Wiki-PEAD] |
| **Analyst revisions** | Post-revision drift is stronger for "high-innovation" revisions, for lower-coverage firms and for obscure analysts. A substantial part of the drift is realised around the next earnings or revision date [GL03] | Weeks | Not quantified in the abstract | Low–moderate | Coverage is high in EU large caps, so expect a smaller effect. **[INFERENCE]** |
| **Short-term reversal vs momentum** | Reversal: stocks whose returns lack fundamental support (cash-flow news proxied by analyst revisions) reverse more. The enhanced reversal strategy earns a risk-adjusted return four times the standard one [DLS14]. Momentum: 12-month-type winners beat losers by more than 1%/month in 12 countries, 1980–95, with continuation lasting about a year [R98] | Reversal: 1 day–1 month. Momentum: 3–12 months | Both are post-publication anomalies, so assume decay. Momentum is subject to crashes. **[UNVERIFIED]** | Reversal is **very** high, because turnover is high and spreads and impact eat the edge. | Momentum is documented outside the US [R98]. Reversal's liquidity-provision component weakens in large caps. **[INFERENCE]** |
| **Insider trading** | US insider purchases earn abnormal returns above 6% per year. Insider sales earn nothing significant [JMZ99]. UK reaction is larger than US, which the authors attribute to speedier UK reporting. Trades preceded by M&A or CEO-replacement news carry less information [FGR05] | Weeks–months | Not studied in these abstracts | Low (rare events) | Good in principle: MAR requires PDMR disclosure. **[UNVERIFIED]** Dutch/German registers could not be fetched. |
| **Short interest** | Hedge funds' disclosed European large short positions earn about 5.5% annualised Fama-French risk-adjusted return, largely explained by mispricing factors. Local, more diversified, more active funds do better, and there is a first-mover advantage [JS15] | Months. A 20-day signal is **[INFERENCE]** weak. | Not studied | Low | Directly European. Disclosure thresholds: **[UNVERIFIED]** (from memory, 0.5% public). |
| **Options-implied** | Calls expensive relative to puts (put-call parity deviation): +50 bps/week for the long-short, with predictability that declines over the sample [CW10]. Steep volatility smirk: about −10.9%/yr risk-adjusted for steepest vs flattest, persisting for 6+ months [XZZ08] | 1 week–6 months | CW10 reports gradual reduction over time | Moderate (needs liquid option quotes) | Both are US-option-based. EU single-name option data is paid. **[INFERENCE]** |
| **Macro regime / volatility** | Volatility-managed portfolios (less risk when volatility is high) raise Sharpe ratios, because volatility changes are not offset by proportional expected-return changes. Shown for the market and six equity factors [MM16] | Sizing, weeks–months | Not studied here | Low | VIX is the S&P 500's implied volatility. VSTOXX is the euro-area analogue. Use for **sizing and abstention**, not stock selection. **[INFERENCE]** |
| **Supply-chain / customer links** | Customer-momentum long-short earns over 150 bps/month alpha [CF08]. A later replication finds 122 bps (equal-weighted) and 106 bps (value-weighted) per month, but **smaller and statistically insignificant post-discovery** [P23] | Monthly | Yes, per [P23] | Moderate | Needs a customer-supplier dataset. Not free for EU. **[INFERENCE]** A hand-built map for 5–10 names is feasible. |
| **Earnings-call tone** | Price et al. (2012) study call tone and returns. I fetched only the citation record with no abstract, so **no effect size** [Price12]. Media tone around earnings carries information beyond press releases and calls, S&P 500 sample [ABB21] | Days–weeks | **[UNVERIFIED]** | Low | Transcripts are paid for EU (see §2) |
| **Attention / news volume** | Google search volume (SVI) differs from turnover or news proxies. Higher attention is linked to stronger momentum and to IPO first-day returns [DEG09]. SVI data is Russell 3000 only | Days–weeks | Not studied | Low | **[UNVERIFIED]** for EU names |
| Text changes in filings ("Lazy Prices") | Shorting "changers" and buying "non-changers" earns up to 188 bps/month alpha. No announcement-day reaction, so investors are inattentive [CMN18] | Months | Not studied | Low | US 10-K only. EU analogue would need ESEF annual reports. **[UNVERIFIED]** |
| **Event studies** | Method, not signal. Use it to estimate the abnormal-return window around each event type on *our* universe. | n/a | n/a | n/a | Required, because published effects are US-biased |

## 2. Data sources for EU large caps

| Source | What | Cost / limits | Licence / terms | Point-in-time? |
|---|---|---|---|---|
| **GDELT** (gdeltproject.org) | Global news, machine-translated from 65 languages. DOC API 2.0 searches back to Jan 2017 within a rolling 3-month window (2017 announcement; **[UNVERIFIED]** whether still true) | Free | "Unlimited and unrestricted" academic, commercial or governmental use. Redistribution requires a citation and link [GDELT-ToU] | Article timestamps yes. GDELT's own tone scores may be reprocessed. **[INFERENCE]** Snapshot your queries. |
| **Marketaux** | News with entity-level sentiment. 200k+ entities, 5,000+ sources, 80+ markets, 30+ languages | Free: 100 req/day, **3 articles/request**. Basic: 2,500 req/day, 20 articles [Marketaux] | Terms not fetched. **[UNVERIFIED]** | Per-article `published_at`. Historical depth not verified. The sample response mapped a Netflix article to a German ticker (NFC.DE), so **entity matching is noisy**. |
| **NewsAPI.org** | Headlines and articles | Developer tier is free but **dev/testing only**, 24h delay, 100 req/day, 1-month search. Business is $449/mo [NewsAPI] | Developer plan may not be used in production, "including internally" [NewsAPI] | Free tier unusable for live signals |
| **FMP** | Prices, fundamentals, analyst estimates and ratings endpoints, earnings transcripts, 13F, bulk | Starter $19/mo (annual billing) is US coverage. Premium $49/mo adds UK and Canada. **Ultimate $99/mo adds global coverage and transcripts** [FMP] | Display/redistribution needs a separate data-licence agreement [FMP] | Historical estimate **vintages** not documented. **[UNVERIFIED]** |
| **EODHD** | EOD prices for 120k+ tickers, 30+ yrs. Fundamentals, calendar/news add-on | Free: 20 calls/day, 1 yr. EOD €19.99/mo. Fundamentals €59.99/mo. Calendar & news API add-on €19.99/mo [EODHD] | Terms not fetched. **[UNVERIFIED]** | Prices adjusted. Fundamentals likely restated. **[INFERENCE]** |
| **Alpha Vantage** | Prices, some fundamentals | Free: 25 req/day [AV] | Personal-use entitlement wording for real-time US data [AV] | Not documented |
| **Yahoo / yfinance** | EU prices, delayed quotes, analyst and earnings fields | Free | yfinance is an unofficial wrapper for "research and educational purposes", and Yahoo's API is "for personal use only" [yfinance]. Yahoo may rate-limit, change or charge [Yahoo-ToU]. | **No.** Analyst fields are current snapshots. **[INFERENCE]** Log daily to build your own history. |
| **FRED VIXCLS** | VIX daily close, updated next morning | Free | Copyright CBOE. Citation required [FRED] | Close only, with a one-day lag |
| **Cboe VIX page** | Spot VIX, delayed 20 min | Free | Data delayed at least 20 min [Cboe] | Not for backtests |
| **VSTOXX** (STOXX / Eurex) | Euro-area implied-vol index | Fetch failed (TLS error). Terms **[UNVERIFIED]** | **[UNVERIFIED]** | **[UNVERIFIED]** |
| **ECB Data Portal** | Rates, FX, euro-area macro (SDMX REST) | Free | Open API [ECB-API] | **Yes for revisions**: `updatedAfter` and `includeHistory=true` return previous versions [ECB-API] |
| **Eurostat API** | Euro-area macro, SDMX 2.1/3.0, JSON-stat, CSV | Free | Open API [Eurostat-API] | Release-date handling not checked. **[INFERENCE]** Snapshot on download. |
| **Deutsche Börse Public Dataset (AWS)** | 1-minute Xetra/Eurex bars | Free | **Non-commercial** licence [DB-PDS]. The registry page also carries a "Deprecated" label, so check the date coverage. | Prices, yes |
| **SEC EDGAR APIs** | JSON for 10-K/10-Q/8-K/20-F/6-K, no key | Free | Open API [SEC] | Submission timestamps. Only for EU firms that file with the SEC. |
| **AFM / BaFin / AMF insider registers; ESMA short positions** | PDMR transactions and short positions | Fetch failed (browser required). **[UNVERIFIED]** | **[UNVERIFIED]** | National register publication time is the PIT stamp. |
| **ESEF annual reports (filings.xbrl.org)** | Machine-readable EU annual reports | Fetch failed. **[UNVERIFIED]** | **[UNVERIFIED]** | **[UNVERIFIED]** |
| Earnings transcripts | FMP Ultimate [FMP]. No free EU source found. | $99/mo (annual billing) | See FMP | Transcript timestamp unclear. **[UNVERIFIED]** |
| Single-name options (EU) | No free source found. EODHD's options add-on covers US stocks only [EODHD] | n/a | n/a | n/a |

## 3. What is realistic

- **Large-cap efficiency.** For 1–20 day horizons on liquid EU large caps, I found no fetched evidence of a net-of-cost edge from any single free signal. The best-documented fast signal (LLM news, 34 bps/day) is pre-cost, US-only, and decaying. **[INFERENCE]**
- **Decision timing.** Our decisions are hourly/daily. The 1–2 day LLM drift sits inside that window, but turnover is high, so cost sensitivity is the binding constraint. The Trading 212 DEMO account will not show the real friction (spread, FX, French/Italian transaction taxes). **[UNVERIFIED]** The tax rates are from memory.
- **Scoring.** With 2–3 holdings and about 250 decisions/year, a 95% bootstrap interval will be wide. Do not expect to detect an IC below roughly 0.05 within a year. **[INFERENCE]**

## 4. Implications for our setup

Ranked by value per effort (highest first):

1. **Point-in-time logger.** Store every input with its ingestion timestamp: news, consensus estimates, VSTOXX/VIX, short and PDMR registers. Free sources (Yahoo, ECB revisions aside) do not keep vintages, and this is the only way to get honest backtests of revisions and sentiment later. Effort: low.
2. **Post-cutoff-only LLM evaluation, plus anonymised-headline variant.** Gate every LLM-sentiment backtest on dates after the model's cutoff, and run an anonymised comparison [GL23, GJY26]. Effort: low. It guards against the largest known bias.
3. **Volatility/regime features for sizing and abstention.** Use VIX (FRED), VSTOXX if obtainable, and ECB rates, to scale position size [MM16]. Effort: low. Cost: free.
4. **Earnings-calendar conditioning.** Add days-to-earnings and a post-announcement window flag. Compute surprise vs consensus from the logger. Feed to the analyst stage and use event-study windows from our own data. Effort: low–moderate. PEAD is the longest-documented drift but decayed [Wiki-PEAD, HLW15].
5. **12-1 momentum and 1-week reversal from prices.** Free price data covers both. Use momentum as a base-rate prior [R98] and treat reversal cautiously (high-cost, weak in large caps). Effort: very low.
6. **Revision-adjusted reversal.** Interact the 1-week return with consensus revisions, so reversal fires only when the move is not fundamentally explained [DLS14]. Needs item 1's revision history. Effort: moderate.
7. **GDELT + Marketaux news-volume and tone features.** Both are free. GDELT is licence-clean for commercial use [GDELT-ToU]. Add abnormal news volume and abnormal tone as attention inputs [DEG09, ABB21]. Keep Marketaux entity matching under a manual spot-check. Effort: moderate.
8. **Hand-built peer/supplier map for the 5–10 names.** Feed lagged returns of key customers/suppliers (e.g. a semiconductor-equipment name and its chip-maker customers) [CF08]. Expect smaller effects than published [P23]. Effort: moderate (manual, once).
9. **PDMR insider-purchase scraper for the held names.** Buys only, since sales carry no signal [JMZ99]. Low frequency, so few observations. Effort: moderate–high (register access unverified).
10. **Paid transcripts (FMP Ultimate, about $99/mo) for call-tone features.** Effect size is unquantified in what I could fetch [Price12], and EU coverage is unverified. Defer until items 1–7 show the pipeline is worth feeding. Effort: moderate. Cost: recurring.

## 5. Open questions

- What is the EU large-cap equivalent of the LLT25 drift? No fetched study. Our own post-cutoff log (item 1 + 2) is the only way to answer.
- Do the AFM/BaFin/AMF registers offer machine-readable downloads, and what is each register's publication lag?
- Does FMP Ultimate cover EU names for estimates and transcripts, and does it keep estimate vintages?
- Exact FTT rates for French and Italian names, and the Trading 212 spread/FX costs for each holding.

## References (all fetched 2026-10-20 unless noted)

- [LLT25] Lopez-Lira & Tang, "Can ChatGPT Forecast Stock Price Movements? Return Predictability and Large Language Models", arXiv:2304.07619v6, 2025. https://arxiv.org/abs/2304.07619 (abstract and PDF excerpts)
- [KG24] Kirtac & Germano, "Sentiment trading with large language models", Finance Research Letters 62, 2024. https://arxiv.org/abs/2412.19245
- [GL23] Glasserman & Lin, "Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis", 2023. https://arxiv.org/abs/2309.17322
- [GJY26] Gao, Jiang & Yan, "Detecting Lookahead Bias in LLM Forecasts", arXiv:2512.23847v2, 2026. https://arxiv.org/abs/2512.23847
- [KKX19] Ke, Kelly & Xiu, "Predicting Returns With Text Data", NBER w26186, 2019. https://www.nber.org/papers/w26186
- [T07] Tetlock, "Giving Content to Investor Sentiment: The Role of Media in the Stock Market", SSRN 685145 (2005 abstract via Crossref). https://doi.org/10.2139/ssrn.685145
- [CF08] Cohen & Frazzini, "Economic Links and Predictable Returns", Journal of Finance 63(4), 2008. https://doi.org/10.1111/j.1540-6261.2008.01379.x
- [P23] Pinchuk, "Customer Momentum", arXiv:2301.11394, 2023. https://arxiv.org/abs/2301.11394
- [MP12] McLean & Pontiff, "Does Academic Research Destroy Stock Return Predictability?", SSRN 2080900, 2012. https://doi.org/10.2139/ssrn.2080900
- [CST12] Chordia, Subrahmanyam & Tong, "Have Capital Market Anomalies Attenuated in the Recent Era of High Liquidity and Trading Activity?", SSRN 2029057, 2012. https://doi.org/10.2139/ssrn.2029057
- [Wiki-PEAD] "Post-earnings-announcement drift", Wikipedia (secondary source). https://en.wikipedia.org/wiki/Post%E2%80%93earnings-announcement_drift
- [HLW15] Hung, Li & Wang, "Post-Earnings-Announcement Drift in Global Markets: Evidence from an Information Shock", SSRN 2607782, 2015. https://doi.org/10.2139/ssrn.2607782
- [GL03] Gleason & Lee, "Analyst Forecast Revisions and Market Price Discovery", The Accounting Review 78(1), 2003. https://doi.org/10.2308/accr.2003.78.1.193
- [DLS14] Da, Liu & Schaumburg, "A Closer Look at the Short-Term Return Reversal", Management Science, 2014. https://doi.org/10.1287/mnsc.2013.1766
- [R98] Rouwenhorst, "International Momentum Strategies", Journal of Finance, 1998. https://doi.org/10.1111/0022-1082.95722
- [JMZ99] Jeng, Metrick & Zeckhauser, "Estimating the Returns to Insider Trading: A Performance-Evaluation Perspective", SSRN 146029, 1999. https://doi.org/10.2139/ssrn.146029
- [FGR05] Fidrmuc, Goergen & Renneboog, "Insider Trading, News Releases and Ownership Concentration", ECGI, 2005. https://doi.org/10.67929/ecgi-fin-093-2005
- [JS15] Jank & Smajlbegovic, "Dissecting Short-Sale Performance: Evidence from Large Position Disclosures", SSRN 2631266, 2015. https://doi.org/10.2139/ssrn.2631266
- [CW10] Cremers & Weinbaum, "Deviations from Put-Call Parity and Stock Return Predictability", JFQA, 2010. https://doi.org/10.1017/s002210901000013x
- [XZZ08] Xing, Zhang & Zhao, "What Does Individual Option Volatility Smirk Tell Us About Future Equity Returns?", SSRN 1107464, 2008. https://doi.org/10.2139/ssrn.1107464
- [MM16] Moreira & Muir, "Volatility Managed Portfolios", NBER w22208, 2016 (published JF 2017). https://www.nber.org/papers/w22208
- [Price12] Price, Doran, Peterson & Bliss, "Earnings conference calls and stock returns: The incremental informativeness of textual tone", J. Banking & Finance, 2012 (citation record only). https://doi.org/10.1016/j.jbankfin.2011.10.013
- [ABB21] Ardia, Bluteau & Boudt, "Media abnormal tone, earnings announcements, and the stock market", J. Financial Markets 61, 2022. https://arxiv.org/abs/2110.10800
- [DEG09] Da, Engelberg & Gao, "In Search of Attention", SSRN 1364209, 2009. https://doi.org/10.2139/ssrn.1364209
- [CMN18] Cohen, Malloy & Nguyen, "Lazy Prices", NBER w25084, 2018 (published JF 2020). https://www.nber.org/papers/w25084
- [GDELT-ToU] The GDELT Project, "About / Terms of Use". https://www.gdeltproject.org/about.html. DOC API 2.0 announcement, 2017: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- [Marketaux] Marketaux, pricing. https://www.marketaux.com/pricing
- [NewsAPI] News API, pricing. https://newsapi.org/pricing
- [FMP] Financial Modeling Prep, pricing plans. https://site.financialmodelingprep.com/pricing-plans
- [EODHD] EODHD, pricing (page dated 2025-05-23). https://eodhd.com/pricing
- [AV] Alpha Vantage, premium. https://www.alphavantage.co/premium/
- [yfinance] ranaroussi/yfinance README. https://github.com/ranaroussi/yfinance
- [Yahoo-ToU] Yahoo Developer API Terms of Use, REV 3-2022. https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html
- [FRED] FRED, "CBOE Volatility Index: VIX (VIXCLS)". https://fred.stlouisfed.org/series/VIXCLS
- [Cboe] Cboe, VIX Volatility Products. https://www.cboe.com/tradable_products/vix/
- [ECB-API] ECB Data Portal, API data. https://data.ecb.europa.eu/help/api/data
- [Eurostat-API] Eurostat, API getting started. https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-getting-started
- [DB-PDS] Deutsche Börse Public Dataset, Registry of Open Data on AWS. https://registry.opendata.aws/deutsche-boerse-pds/
- [SEC] SEC, EDGAR APIs. https://www.sec.gov/search-filings/edgar-application-programming-interfaces
