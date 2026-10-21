# LLM multi-agent trading systems: architecture and measured performance (as of 2026-10-20)

**Tags.** [INFERENCE] is my own reasoning. [UNVERIFIED] means I could not confirm the claim in a primary source.
**Read depth.** Full text: [1]–[4], [6], [7], [10]–[15], [18], [19], [22]. Abstract plus targeted checks: [5], [8], [9], [16], [17], [20], [21], [23]–[25], [28]. Alpha Arena: secondary sources only [26].

## Summary
- Each headline agent result (FinMem, FinAgent, FinCon, TradingAgents, MarketSenseAI) is the authors' own backtest: 3–8 months on 3–8 large caps (MarketSenseAI: 15–24 months on 100 stocks). Most windows sit inside the backbone LLM's training period or a bull market.
- Third parties that re-ran these systems with longer, broader, bias-controlled tests found the edge gone [11], [13]. The live and post-cutoff tests I found are mostly null or negative, with cost treatment varying [10], [20]–[22].
- Memorisation of prices is real [15]. Its effect on sentiment-style signals is smaller and has an ambiguous sign [14], [17].
- No leakage-free study isolates whether debate or multi-agent structure adds value. The only ablations are author-reported [24].

## A. Systems

| System (year) | Architecture | Data / horizon | Eval window; out-of-sample? | Costs | Reported → independent check |
|---|---|---|---|---|---|
| **FinMem** (2023) [2] | Single agent. Profiling, layered memory (working; shallow/intermediate/deep long-term), extended reflection. No debate. | Prices, Alpaca news, 10-Q/10-K. Daily. | Train 2021-08→2022-10; test 2022-10-06→2023-04-10; 5 tickers. Time split, but best of 3 risk settings picked on test return. Inside GPT-4-Turbo's training period [INFERENCE]. | None found | TSLA CR +61.8%, SR 2.68 (B&H −18.6%). FINSABER re-run: TSLA +19.9% (GPT-4o-mini) / +5.3% (GPT-4o); loses to B&H on NFLX and MSFT. 2004–24, 4 tickers: leads only on TSLA. Bias-controlled composite: Sharpe −0.29…+0.03 vs B&H 0.32…0.70, α<0 everywhere, p>0.34 [11]. Total return −71.9% after the GPT-4o cutoff [12]. |
| **FinAgent** (KDD 2024) [3] | Single multimodal agent (GPT-4V). Market intelligence (prices, news, Kline charts), low/high-level reflection, diversified retrieval memory, tools/expert strategies. | AAPL, AMZN, GOOGL, MSFT, TSLA, ETH. Daily. | Train 2022-06→2023-06; test 2023-06-01→2024-01-01. Partly post-cutoff for GPT-4V [INFERENCE]. | Commission is in the reward definition; I did not verify it was charged | TSLA CR +92.3%, SR 2.01 (its best of 6). FINSABER: leads on TSLA only. Composite Sharpe −0.08…0.24; best α +6.6%, p=0.35 [11]. |
| **FinCon** (NeurIPS 2024) [4] | Manager–analyst hierarchy (multi-agent, no debate). Within-episode CVaR risk control; episodic verbal-reinforcement belief updates. GPT-4-Turbo. | Prices, news, 10-K/Q, earnings-call audio. Daily. | Train 2022-01-03→2022-10-04; test 2022-10-05→2023-06-10; 8 stocks + portfolios; median of 5 runs. Overlaps GPT-4-Turbo training [INFERENCE]. | None found | TSLA CR +82.9%, SR 1.97; best portfolio CR +113.8%, SR 3.27. No replication found (FINSABER reused only its stock-selection agent) [11]. Sharpe −62.2% after GPT-4o cutoff [12]. |
| **TradingAgents** (Tauric, 2024–25) [1] | Fundamentals, sentiment, news, technical analysts → bull/bear debate (n rounds, facilitator) → trader → 3-way risk debate → fund manager. Structured reports plus dialogue. gpt-4o / o1-preview. | Prices, multi-source news, Reddit/X sentiment, insider data, filings, 60 indicators. Daily. | 2024-01-01→03-29; Table 1 has 3 tickers (AAPL, GOOGL, AMZN) though text lists five. Post-GPT-4o-cutoff (Oct 2023 [12]) but one bullish 3-month window. | Not described | CR 23.2–26.6%, SR 5.60–8.21, MDD ≤2.1% vs B&H −5.2/+7.8/+17.1%. Authors' footnote: SR exceeds their expected range, attributed to few pullbacks [1]. Survey: consistent with trend-following in a favourable regime [24]. One-year, five-ticker reproduction (2025-01→2026-01): $106.4K gross / $102.3K net vs B&H $104.8K; net Sharpe 0.43→0.22; B&H wins 4 of 5 tickers [13]. Total return −50.2% after GPT-4o cutoff [12]. Repo notes v0.3.1–v0.5.x list look-ahead fixes in data vendors, macro, sentiment, memory, so earlier versions had leak paths [27]. |
| **FinRobot** (2024) [5] | Platform, not a strategy: Financial-CoT agents, LLM-algorithm, LLMOps/DataOps and foundation-model layers. Analyst workflows. | n/a | No trading backtest or performance claim found | n/a | Nothing to replicate. |
| **MarketSenseAI** v1 [6] (2024), v2 [7] (2025) | News, fundamentals (10-K/Q), price-dynamics and macro agents → signal agent. GPT-4, then GPT-4o; v2 adds RAG over filings/earnings calls. | S&P 100 (v2: S&P 500 for 2024). Monthly signals. | v1: 2022-12→2024-03 (1,500 signals: 338 buy / 1,150 hold / 12 sell). v2: 2023–24. Overlaps GPT-4/4o training [INFERENCE]. | **Yes**: 5 bps (v1), 10 bps (v2) | v1 best: +72.9% (71.6% net), Sharpe 2.80. v2: cap-weighted +125.9% vs index +73.5%; α 8.0% (S&P 100 equal-weight), cap-weighted β 1.24. Unreplicated, no code [11]. High β means part of the gain is risk [INFERENCE]. |
| **StockAgent** (2024) [8] | Simulated market of LLM investors (4 personalities, bulletin board, loans, events). GPT-3.5-Turbo, Gemini-Pro. | Synthetic companies A/B [INFERENCE from tables]; 264 simulated days | Built to avoid test-set leakage | n/a | Behavioural study; no real-market profit claim. |
| **2025–26 successors** [28] | TradingGroup (multi-agent, self-reflection, stop-loss, data synthesis); Trading-R1 (RL-trained reasoner); ContestTrade (internal contest, A-shares); HedgeAgents (claims 70% annualised, 400% over 3 yrs); Strat-LLM (live-forward 2025). | — | Abstract-level only; backtests or short live-forward windows | — | No independent checks found. |

## B. Benchmarks, live tests, critiques

| Study | Design | Result | Caveat |
|---|---|---|---|
| **StockBench** (2025) [10] | 20 DJIA stocks, daily, 2025-03-03→06-30 (82 days), 14 LLMs × 3 seeds, post-cutoff. | Passive +0.4%, MDD −15.2%. Best (Kimi-K2) +1.9%, MDD −11.8%. 8 of 14 beat passive return. All lag passive in the downturn window, lead in the upturn. | No costs modelled. Margin (~1.5 pp) ≈ Kimi's seed std (1.7 pp at 20 stocks). Abstract ("most struggle") conflicts with results text. |
| **FINSABER** (KDD 2026) [11] | 2004–2024, 100+ symbols incl. delisted, rolling windows, costs charged. | B&H and ARIMA beat FinMem/FinAgent. Bull/bear Sharpe: FinAgent +0.12/−0.38, FinMem −0.19/−0.97. FinMem commissions 5–9× FinAgent's. | LLM leakage not removed; rule baselines untuned. |
| **FinBen** FinTrade (2024) [9] | 10 stocks, 1 year, 15 LLMs. | GPT-4 CR +28.2%, SR 1.51 vs B&H −4.0%, SR 0.02. | One window; costs unstated. |
| **LiveTradeBench** (2025) [18] | Live 2025-08-18→10-24 (50 days), 21 LLMs, US stocks + Polymarket; single-agent allocator. | Stocks CR +1.8…+6.3%, all positive. Polymarket mostly deeply negative. Market Sharpes uncorrelated; LMArena vs return ρ=0.05. | No passive baseline found; all-positive in a rising window suggests beta [INFERENCE]. |
| **Agent Market Arena** (2025) [19] | Live 2025-08-01→09-30; 4 frameworks × 5 backbones; TSLA, BMRN, BTC, ETH. | Framework drives behaviour more than backbone. TradeAgent on TSLA: −38.7% (GPT-4.1) to +21.9% (Gemini-2.0-flash). | Two months, 4 assets. |
| **DeepFund** (2025) [20] | Live multi-agent fund benchmark, post-cutoff data, 9 LLMs. | Even DeepSeek-V3 and Claude-3.7-Sonnet net-lose. | AMA says its window is 24 days [19]. |
| **Alpha Arena** S1 (Nof1) [26] | $10k real money each, Hyperliquid crypto perps, ≤25× leverage, 2025-10-18→11-03, 6 LLMs, price/indicator inputs only. | [UNVERIFIED] Qwen3-Max +22.3%, DeepSeek +4.9%, Claude −42%, Gemini −46%, Grok-4 −58%, GPT-5 −59%. Winners doubled, then gave back 41–54%. | Official pages gone; sources disagree (GPT-5 −59% to −63%). One run, one regime, not multi-agent. S1.5 (US equities, 2 weeks): "Mystery Model" +12.1% [UNVERIFIED]. |
| **CLQT** (2026) [21] | Point-in-time gate, institutional costs, year-long contamination-controlled backtest + 4-week live paper track. | Net of costs, agents beat defensive baselines, not the index. Allocations contradict stated analysis (gap +0.30 / +0.23). | Abstract-level. |
| **Crypto backtest→live** (2026) [22] | ML/RL/LLM/agent methods: 2025 backtest → paper (2026-06-04…09-21) → 4-week live money. | Backtest: 8/8 ML profitable, 0/6 RL/LLM/agent. Paper reverses this (FinMem −→ +5.3%). Live: median −0.56% vs market +11.6%; all 4 deployed methods α<0. | Mean backtest–paper gap 6.3 pp; live is 4 weeks, 4 methods. |
| **Leakage-safe discovery** (2026) [23] | LLM-discovered strategies, point-in-time universe, realistic costs, deflated for search. | Passive benchmarks certified; every LLM strategy rejected. | Strategy discovery, not agent trading. |
| **Alpha Illusion** (2026) [13] | Position paper + reproduction above. | 35 of 40 system × friction cells unmodelled (FinMem, TA, FinAgent, FinCon, QuantAgent). | Authors' own harness; one reproduction. |

## C. Look-ahead and memorisation
- **Exact recall.** GPT-4o recalls S&P 500 levels, unemployment and GDP before its Oct-2023 cutoff. Told to ignore post-2010 data, GDP-direction accuracy was 97.6% before and 98.0% after the artificial cutoff, versus ~40% on real post-cutoff data. Masking fails; post-cutoff recall is absent [15].
- **Agents, matched markets.** Same five agents, Q2–Q3 2021 vs Q3–Q4 2024 (market +13.8% vs +13.4%): total-return decay 50.2% (TradingAgents) to 71.9% (FinMem), Sharpe decay 51.5% to 62.2% [12]. Caveat: the authors then sell FactFin/FinLake-Bench.
- **Sentiment signals differ.** Glasserman & Lin (GPT-3.5; in-sample to Sep 2021, out-of-sample Oct 2021–Dec 2022): anonymised headlines *outperform* in-sample. Distraction outweighs look-ahead; out-of-sample look-ahead is not a concern [14]. Chronologically consistent ChronoBERT/ChronoGPT earn Sharpe comparable to Llama, so look-ahead is modest there [17]. Look-Ahead-Bench reports large alpha decay in standard LLMs but tests a vendor's point-in-time models [25]; I did not verify the author's tie to the vendor.
- **Leakage is wider than prices.** TradingAgents' changelog records fixes for look-ahead in macro, sentiment and memory feeds [27].

## What actually holds up
1. **Backtest alpha from short, hand-picked windows does not hold.** It fails under 20 years × 100+ symbols with survivorship control and costs [11], and post-cutoff live tests are mostly null or negative [10], [20]–[22]. Strongest evidence.
2. **Memorisation of numeric history is established; its strategy impact is task-dependent.** Treat in-cutoff backtests of agents that see prices or dates as uninformative [15]. For headline sentiment the damage is smaller [14], [17].
3. **Costs hit high-turnover agents.** FinMem trades 5–9× more than FinAgent [11]. TradingAgents makes 11 LLM and 20+ tool calls per prediction [1], and its net Sharpe halves in one reproduction [13]. Monthly selection (MarketSenseAI) loses ~1 pp to costs [6].
4. **Multi-agent debate is unproven.** Ablations are author-reported (removing coordination: Sharpe −15–30%) [24]. Debate wins <20% of 36 non-trading configurations (Zhang et al., as cited in [13]). AMA shows framework design matters but cannot isolate debate from prompts or risk style [19]. No source compares equal-compute single vs multi-agent under leakage control.
5. **Regime dependence, with conflicting sources.** StockBench: lower drawdown but lag in downturns [10]. FINSABER: too conservative in bulls, heavy losses in bears [11]. Treat risk-control claims as unverified.
6. **Weakly surviving results:**
   - News-headline signals: small-cap and negative-news drift, decaying as adoption rises [16].
   - Input ablations: dropping news and fundamentals lowers return, tested on 2 models [10].
   - Fundamentals-driven monthly selection (MarketSenseAI): the only design with two years of cost-aware evidence, but author-only, unreplicated and high-β.
7. **Live money is anecdote.** Alpha Arena's 17 days at ≤25× leverage cannot separate skill from luck, and results flipped sign between backtest and paper in [22].

## Implications for our setup
- **Cutoff.** Only data after qwen3.8-27b's training cutoff (date not verified by me) is out-of-sample. The Trading 212 demo run is the right design; log prompt, inputs and timestamp for every decision. Use backtests as smoke tests only [15].
- **Power.** 3 names with T+5/T+20 grading from hourly decisions give heavily overlapping samples; expect months before skill separates from luck, and deflate for every variant tried [23]. Grade `p_outperform_20d` with Brier/IC against always-hold, momentum, last-return and equal-weight baselines, as FINSABER does [11].
- **Costs.** Model spread, FX and the broker's fee schedule explicitly (I did not verify Trading 212's). Hourly cadence makes turnover the main failure mode [11], [13]; prefer daily decisions and trade only when the signal clears a cost threshold.
- **Architecture.** Start with one agent and calibrated numeric outputs. Add analyst/debate/risk roles as ablations at equal token budgets with ≥3 seeds (seed noise ≈1.7 pp for Kimi-K2 in [10]). Test Opus-as-judge as its own ablation; no source tested a debate→judge pipeline.
- **LLM as feature extractor.** News and filings → structured scores → simple calibrated model with hard risk limits. This has the best support [13], [16] and avoids the stated-vs-executed gap [21].
- **Memorisation.** Anonymise tickers in replays (cheap; borderline help out-of-sample [14]) but do not rely on it, since masking fails to stop recall [15].
- **Regimes.** Report results by up/down/sideways regime; ranks flip in both StockBench and FINSABER [10], [11].
- **Expectation.** "Match buy-and-hold net of costs with lower drawdown" is a realistic target; the evidence does not support expecting alpha.

## Open questions
1. What is qwen3.8-27b's cutoff, and does NVFP4 quantisation change recall?
2. Do debate rounds help once calibration and costs are controlled, with local models at equal compute?
3. What are Trading 212's real spreads and fees on the EU-listed names, and what hit rate covers them at hourly cadence?
4. Does any study cover EU equities? I found none.
5. Do Alpha Arena's figures reproduce? Official pages are gone and secondary figures disagree.

## References
[1] TradingAgents (v7), arXiv 2412.20138, 2025 — https://arxiv.org/abs/2412.20138
[2] FinMem, arXiv 2311.13743, 2023 — https://arxiv.org/abs/2311.13743
[3] A Multimodal Foundation Agent for Financial Trading (FinAgent), arXiv 2402.18485, 2024 — https://arxiv.org/abs/2402.18485
[4] FinCon, arXiv 2407.06567, 2024 — https://arxiv.org/abs/2407.06567
[5] FinRobot, arXiv 2405.14767, 2024 — https://arxiv.org/abs/2405.14767
[6] Can LLMs Beat Wall Street? (MarketSenseAI), arXiv 2401.03737, 2024 — https://arxiv.org/abs/2401.03737
[7] MarketSenseAI 2.0, arXiv 2502.00415, 2025 — https://arxiv.org/abs/2502.00415
[8] When AI Meets Finance (StockAgent), arXiv 2407.18957, 2024 — https://arxiv.org/abs/2407.18957
[9] FinBen, arXiv 2402.12659, 2024 — https://arxiv.org/abs/2402.12659
[10] StockBench (v2), arXiv 2510.02209, 2025/26 — https://arxiv.org/abs/2510.02209
[11] Can LLM-based Financial Investing Strategies Outperform the Market in Long Run? (FINSABER), arXiv 2505.07078, 2025/26 — https://arxiv.org/abs/2505.07078
[12] Profit Mirage, arXiv 2510.07920, 2025 — https://arxiv.org/abs/2510.07920
[13] The Alpha Illusion, arXiv 2605.16895, 2026 — https://arxiv.org/abs/2605.16895
[14] Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis (Glasserman & Lin), arXiv 2309.17322, 2023 — https://arxiv.org/abs/2309.17322
[15] The Memorization Problem: Can We Trust LLMs' Economic Forecasts? (Lopez-Lira et al.), arXiv 2504.14765, 2025 — https://arxiv.org/abs/2504.14765
[16] Can ChatGPT Forecast Stock Price Movements? (Lopez-Lira & Tang), arXiv 2304.07619, 2023 — https://arxiv.org/abs/2304.07619
[17] Chronologically Consistent Large Language Models, arXiv 2502.21206, 2025 — https://arxiv.org/abs/2502.21206
[18] LiveTradeBench, arXiv 2511.03628, 2025 — https://arxiv.org/abs/2511.03628
[19] When Agents Trade (Agent Market Arena), arXiv 2510.11695, 2025 — https://arxiv.org/abs/2510.11695
[20] Time Travel is Cheating (DeepFund), arXiv 2505.11065, 2025 — https://arxiv.org/abs/2505.11065
[21] CLQT, arXiv 2606.29771, 2026 — https://arxiv.org/abs/2606.29771
[22] Can AI Make Money in Crypto?, arXiv 2609.34510, 2026 — https://arxiv.org/abs/2609.34510
[23] What survives honest evaluation?, arXiv 2608.27734, 2026 — https://arxiv.org/abs/2608.27734
[24] Toward Reliable Evaluation of LLM-Based Financial Multi-Agent Systems, arXiv 2603.27539, 2026 — https://arxiv.org/abs/2603.27539
[25] Look-Ahead-Bench, arXiv 2601.13770, 2026 — https://arxiv.org/abs/2601.13770
[26] Alpha Arena (secondary): TradeRank leaderboard 2026 https://www.traderank.ai/alpha-arena-leaderboard; Upscale 2026 https://upscale.trade/blog/ai-trading-alpha-arena; iWeaver 2026 https://www.iweaver.ai/blog/alpha-arena-ai-trading-season-1-results/; GNcrypto 2025 https://www.gncrypto.news/news/qwen-wins-alpha-arena-season-1-with-22-percent-returns/
[27] TradingAgents GitHub README/changelog, 2026-10 — https://github.com/TauricResearch/TradingAgents
[28] TradingGroup 2508.17565 (2025); Trading-R1 2509.11420 (2025); ContestTrade 2508.00554 (2025); HedgeAgents 2502.13165 (2025); Strat-LLM 2605.06024 (2026) — https://arxiv.org/abs/<id> (substitute each ID)
