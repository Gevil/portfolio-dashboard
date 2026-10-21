# From noisy signals to sized decisions, and evaluating them honestly (small capital, 3–10 names, daily decisions)

Tags: **[SRC]** read this session; **[SIM]** my Monte Carlo (`/tmp/practice/sim1-4.py`, numpy, fixed seeds, assumptions stated); **[INFERENCE]** my reasoning; **[UNVERIFIED]** from memory, source not retrieved. Search was degraded, so several canonical papers (Lo 2002, Ledoit–Wolf Sharpe test, Baker–McHale, Thorp, Joubert/Meyer meta-labeling, Diebold–Mariano, White/Hansen/Romano–Wolf) are **[UNVERIFIED]**.

## 0. Bottom line
1. At IR 0.3–1.0 a few months of paper trading cannot show skill: power of a 5% test at 6 months is 8–17% **[SIM]**. Judge on process metrics (calibration, costs, turnover, ablation CIs); treat P&L as a risk check.
2. Size from a prior and calibrated probabilities, never from the live Sharpe. A 0.5–2 year record carries 4–15% weight in a Bayesian update **[SIM]**.
3. Costs decide viability. Slow the trading (partial adjustment plus no-trade band) and charge an explicit cost model.
4. Equal-weight z-scores with shrinkage beat fitted weights until ~2,500 effective observations **[SIM]**. Same-model LLM agents share errors, so ensembling adds ≤1.3× IC **[SIM]**.
5. Count every configuration tried; report DSR, not raw Sharpe.
6. Ablations need breadth (shadow universe, paired replay) and CI/Bayesian reporting, not significance tests on 3 months of P&L.

## 1. How much data separates skill from luck
H0 SR=0, one-sided 5%, IID normal daily returns, annualised SR (IR vs benchmark is the same maths). Days = (1+SR_d²/2)·((1.645+z_power)/SR_d)². My MinTRL code reproduces the published 2.73-year example (688 days; SR 2 vs 1) **[SRC: Bailey & López de Prado, *The Sharpe Ratio Efficient Frontier*, J. Risk 2012, https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf]** **[SIM]**.

| true IR | days for E[t]=2 | days, 50% power | days, 80% power (years) |
|---|---|---|---|
| 0.3 | 11,200 | 7,577 | 17,314 (68.7) |
| 0.5 | 4,032 | 2,729 | 6,235 (24.7) |
| 0.75 | 1,792 | 1,213 | 2,773 (11.0) |
| 1.0 | 1,008 | 683 | 1,561 (6.2) |

Power of a one-sided 5% test at a fixed horizon **[SIM]**:

| horizon | IR 0.3 | 0.5 | 0.75 | 1.0 |
|---|---|---|---|---|
| 3 mo (63d) | 6.7% | 8.2% | 10.2% | 12.6% |
| 6 mo | 7.6% | 9.8% | 13.3% | 17.4% |
| 12 mo | 8.9% | 12.6% | 18.5% | 26.0% |
| 24 mo | 11.1% | 17.4% | 28.0% | 40.9% |

Luck is cheap. SE(SR_ann)=√(252/T): 2.0 at 3 months, 1.41 at 6, 1.0 at 12. A zero-skill system shows ann. SR ≥1 with probability 31% at 3 months, 24% at 6. A true IR-0.5 system is below zero 40% at 3 months, 36% at 6. Non-normality barely matters at daily frequency (MinTRL for SR 1: 684→709 days with skew −0.5, kurtosis 8) **[SIM]**; autocorrelation and overlapping labels matter more.

**Universe size does not change the days needed; it changes the IC needed for a given IR.** IR ≈ IC·√BR_eff, BR_eff/yr = 252·N/(1+(N−1)ρ), ρ = residual return correlation **[UNVERIFIED: fundamental law; arithmetic checked by me]**. Daily IC needed (ρ=0.3, assumed):

| N | IR 0.3 | 0.5 | 0.75 | 1.0 |
|---|---|---|---|---|
| 3 | .0138 | .0230 | .0345 | .0460 |
| 5 | .0125 | .0209 | .0313 | .0418 |
| 10 | .0115 | .0192 | .0287 | .0383 |

(ρ=0, N=5: .0085/.0141/.0211/.0282 **[SIM]**.) IC 0.03 equals a hit rate of only 50.96% (0.5+asin(IC)/π). Proving a 52% hit rate at 80% power needs 3,862 independent decisions: 36.8 months of 5 independent daily names, or ~61 years with non-overlapping 20-day labels **[SIM]**. Top-quartile managers run ~0.5 IR **[SRC: https://en.wikipedia.org/wiki/Information_ratio]**; IR 1.0 here would be extraordinary.

Overlap rule **[INFERENCE]**: daily decisions graded at horizon h give effective sample ≈ N_eff·T/h; 3 months of paper data is ~3 independent 20d windows per name.

## 2. Signal combination
- **Normalise**: time-series z-score per input and asset from past data only (expanding window, ≥60 obs, lag 1), clip ±3. Cross-sectional z-scores are noisy with 3–10 names **[INFERENCE]**.
- **Ensembling gain**: K signals of equal IC, pairwise correlation ρ: IC_ens = IC·√K/√(1+(K−1)ρ). Simulated: ρ=0.5 → 1.30×; ρ=0.8 → 1.05× **[SIM]**. Same-model LLM analysts have correlated errors **[INFERENCE]**; diversity must come from different information (prices, filings, macro, flows), not more agents or debate rounds.
- **Weights**: K=8 signals (4 informative, 4 useless), noise correlation 0.3, out-of-sample IC **[SIM]**:

| training n | equal | OLS | ridge | 50% OLS + 50% equal |
|---|---|---|---|---|
| 750 | .0229 | .0260 | .0266 | **.0317** |
| 1,250 | .0229 | .0328 | .0335 | **.0337** |
| 2,500 | .0229 | .0397 | .0403 | .0370 |
| 12,500 | .0229 | .0525 | .0526 | .0408 |

Oracle .0576. Below ~2,000 effective observations shrink toward equal weights (a Bayesian prior that all inputs are similar); fitted weights win later. Equal-weight combination "puzzle" literature **[UNVERIFIED]**.
- **Hurdle for new inputs**: data mining means new factors need t>3.0, not 2.0 **[SRC: Harvey, Liu & Zhu, RFS 2016, https://www.nber.org/papers/w20592]**.

## 3. Meta-labeling
A primary model sets the side; a secondary classifier decides act/pass and, via its probability, the size. Claimed benefits: higher F1 through fewer false positives, less overfitting (ML does not pick the side), separate long/short models **[SRC: López de Prado, *Advances in Financial Machine Learning* ch.3, quoted at https://hudsonthames.org/meta-labeling-a-toy-example/, 2019]**. Joubert and Meyer–Barziy–Joubert extensions **[UNVERIFIED]**.

Constraint is sample size. Detecting a meta-model's AUC (Mann–Whitney, SE0=√((n1+n0+1)/(12·n1·n0))) **[SIM]**:

| events n | SE0 | AUC .55 | AUC .58 | AUC .62 |
|---|---|---|---|---|
| 300 | .033 | 44% power | 78% | 99% |
| 600 | .024 | 70% | 95% | 100% |
| 1,200 | .017 | 91% | 100% | 100% |

AUC 0.55 needs ≥600 events (2+ years of 5 names with 5-day non-overlapping labels). Logistic calibration-slope SE (sd(logit p)=0.2): 1.04 at n=100, 0.59 at 300, 0.32 at 1,000, 0.14 at 5,000 **[SIM]**.

**[INFERENCE]**: LLM-forecast backtests leak training-data knowledge, so only live logs are clean and a meta-model cannot be validated for ~2 years. Meanwhile use a fixed shrunk p→size map (section 4). Meta-models: ≤5 features, strong L2, purged validation, counted as trials.

AFML bet-size map m=2Φ(z)−1, z=(p−½)/√(p(1−p)) **[UNVERIFIED]**: p=0.55→0.080, 0.60→0.162, 0.70→0.337 **[SIM]**, slightly more conservative than 2p−1.

## 4. Sizing
- **Kelly**: binary f=p/l−q/g; continuous f=μ/σ². Exact only for known probabilities; estimation error argues for betting less **[SRC: https://en.wikipedia.org/wiki/Kelly_criterion]**. At fraction c of true Kelly, growth = (2c−c²) of max, volatility = c: half-Kelly keeps 75% of growth at 50% of volatility; c=2 gives zero growth **[SIM]**. Fractional-Kelly papers (Thorp; MacLean–Thorp–Ziemba; Baker–McHale) **[UNVERIFIED]**.
- **From p to size**: if r_h~N(μ,σ_h²), P(r_h>0)=Φ(μ/σ_h), so f_full=Φ⁻¹(p)/σ_h. p=0.55, σ_20d=7%: f_full=1.80, quarter-Kelly 0.45 **[SIM]**. A 55% belief is already huge, so shrink p and cap weights.
- **Estimation error** (SR_true~N(0.2,0.25²), σ=20%, long-only cap 1×; annual log growth) **[SIM]**:

| sizing input (c=1) | 0.5-yr record | 2-yr record |
|---|---|---|
| raw SR_hat | 1.42% | 1.80% |
| posterior mean | 2.05% | 2.18% |
| prior only | 2.00% | 2.00% |

With a 3× cap, raw SR_hat at c=1 loses money (−1.31%, 0.5 yr). Bayesian weight on the sample (prior sd 0.3): 4% at 0.5y, 8% at 1y, 15% at 2y, 47% at 10y.
- **Vol targeting** raised Sharpe for equity factors and currency carry **[SRC: Moreira & Muir, JF 2017, https://www.nber.org/papers/w22208]**. Evidence is for factors; single-name transfer is **[INFERENCE]**.

## 5. Turnover control
- Optimal policy with predictable returns and costs: trade partially toward an "aim" portfolio; slower-decaying signals get more weight **[SRC: Gârleanu & Pedersen, JF 2013, https://www.nber.org/papers/w15205]**. Proportional-cost theory gives a no-trade region widening with the cube root of cost (Davis–Norman; Janeček–Shreve) **[UNVERIFIED]**.
- **Simulation** **[SIM]**: one name, σ_d=1.5%, IC 0.03, target weight 0.5·z clipped ±1, 40 years. Gross SR is 0.13 / 0.41 / 0.52 for signal autocorrelation φ = 0.5 / 0.9 / 0.97. Net SR:

| φ | cost bp one-way | trade to target daily | band δ=0.2 | partial κ=0.13 |
|---|---|---|---|---|
| 0.5 | 0 / 5 / 10 | .13 / −.29 / −.71 | .23 / −.03 / −.29 | .12 / .01 / −.11 |
| 0.9 | 0 / 5 / 10 / 25 | .41 / .23 / .04 / −.51 | .40 / .32 / .25 / .02 | .40 / .35 / .31 / .17 |
| 0.97 | 0 / 5 / 10 / 25 | .52 / .42 / .32 / .01 | .51 / .48 / .45 / .37 | .54 / .52 / .49 / .42 |

Daily full trading turns over 43×/yr at φ=0.9: 5 bp removes ~45% of the edge, 10 bp ~90%. κ=0.13 trades 7×/yr. A fast signal (φ=0.5) is untradeable at ≥5 bp under any rule. Cost level matters more than band tuning: measure real Trading 212 spread/slippage and charge it in paper P&L (the demo account may not reproduce it) **[INFERENCE]**.

## 6. Evaluating without fooling yourself
- **PSR**: probability true SR exceeds a reference given T, skew γ3, kurtosis γ4. **MinTRL**: T for 95% confidence **[SRC: Sharpe Ratio Efficient Frontier, link above]**.
- **DSR**: PSR with reference = expected max SR over N trials; without the trial count a backtest is "worthless" **[SRC: Bailey & López de Prado, *The Deflated Sharpe Ratio*, J. Portfolio Mgmt 2014, https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf]**. Equations were unreadable in my fetch; implemented from memory and tested: SR0=√V[(1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(Ne))], γ=0.5772; DSR=Φ[(SR̂−SR0)√(T−1)/√(1−γ3·SR̂+((γ4−1)/4)·SR̂²)], per-period SR, raw kurtosis. In 3,000 runs the best of N=10 or 50 zero-skill variants passes DSR>0.95 0.1–0.2% of the time, versus 40% (N=10) and 92% (N=50) for PSR(0)>0.95: DSR is conservative **[SIM]**.
- **Luck from search**: expected best annualised SR among N zero-skill variants, T=126: N=5→1.69; 10→2.23; 20→2.69; 50→3.22. T=252: N=10→1.57; 20→1.90 **[SIM]**. MinBTL < 2·ln N/E[max_N]² years **[SRC: Bailey, Borwein, López de Prado & Zhu, *Pseudo-Mathematics and Financial Charlatanism*, Notices AMS 2014, https://www.ams.org/notices/201405/rnoti-p458.pdf]**.
- **Dependent trials**: the DSR paper's Appendix 3 covers non-independent N. My proposal **[INFERENCE]**: N_eff=(Σλ)²/Σλ² over eigenvalues of the trial-return correlation matrix (20 trials, ρ=0.3/0.6/0.9 → 7.4/2.6/1.2).
- **Repeated holdouts do not prevent overfitting; PBO needs lots of data [SRC: DSR paper]**.
- **Purged/combinatorial CV**: purge training labels overlapping the test window, embargo after it **[SRC: https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html, citing AFML 2018]**. 6 groups/2 test: 15 splits, 5 paths; 10 groups: 45 splits, 9 paths **[SIM]**. Use only for non-LLM signals with clean history.
- **Peeking**: daily looks from day 20 give a null system |t|>1.96 at some point 18% within 60 days, 25% within 120, 30% within 250 (weekly: 15/21/26%) **[SIM]**. A Gaussian-mixture always-valid test holds Type I <1% but costs power (5.8% for IR 1.0 at 2 years) **[SIM]**. Pre-register looks.
- **Information in months**: expected log-LR (IR s vs 0)=T·s²/504 nats; 6 months at s=0.5 gives 0.06 nats, essentially uninformative. E[logLR]=ln 20 needs 1,510 days (s=1.0), 2,684 (0.75), 6,039 (0.5), 16,776 (0.3) **[SIM]**.
- Comparing correlated Sharpes: bootstrap/HAC (Ledoit–Wolf) **[UNVERIFIED]**.

## 7. Ablations on a few months of live paper data
Paired design: run both variants on the same decisions (replay with the input removed; local LLM cost is small) and test d_t=Score(B)−Score(A); primary score Brier/log loss, secondary net P&L.

Minimum detectable ΔIC (5% one-sided, 80% power) ≈ 2.49·√(2(1−ρ_AB))/√(N_eff·T), ρ_AB = forecast correlation, N_eff=N/(1+(N−1)ρ_resid). Matches simulation (ρ_AB=0.86, N=5 independent, T=504: formula 0.026; ΔIC 0.027 gave 82% power) **[SIM]**.

| N_eff | 63d | 126d | 252d | 504d |
|---|---|---|---|---|
| 3.6 (N=5, ρ=0.1) | .087 | .062 | .044 | .031 |
| 8.5 (N=50, ρ=0.1) | .057 | .040 | .028 | .020 |

An input adding ΔIC≈0.01 is undetectable within 2 years. Simulated power (N=5) for ΔIC .011/.027/.047: T=126: 12/35/71%; T=252: 18/55/93% **[SIM]**. 50 independent names at T=63 and ΔIC 0.026 gave 87%, but with ρ_resid=0.1 the real gain is 2.4×, not 10×. Pure-noise inputs registered as "better" 2.7–3.2% of the time, so the test is not anti-conservative **[SIM]**.

Procedure:
1. Pre-register ≤6 ablations (leave-one-input-group-out; debate vs single analyst; Opus final-maker vs rule-based aggregation).
2. Score a **shadow universe** (30–50 liquid EU names, forecasts only), aggregated to daily d_t, using residual returns.
3. Circular block bootstrap (block ≥2h) or HAC (lag h−1); Diebold–Mariano/Harvey–Leybourne–Newbold **[UNVERIFIED]**.
4. Holm across the ≤6 ablations; report CIs.
5. Bayesian read-out: prior ΔIC~N(0,0.01²); report posterior mean and P(ΔIC>0). Drop an input only if costly or P(ΔIC>0)<0.2.
6. Each ablation counts as a DSR trial.

## 8. Open questions
- Real IC of LLM signals on EU large caps (I assumed 0.02–0.05): measure on the shadow universe first.
- Real Trading 212 costs/fills unverified; 5–10 bp is an assumption.
- Signal autocorrelation φ and residual correlation ρ are assumed; estimate them.

## 9. Adopt these

**Signals.** z=clip((x−EWMA mean)/EWMA sd, ±3), half-life 60d, lag 1. s_i=Σ_k w_k z_k, w=(1−λ)/K+λ·w_ridge, λ=n_eff/(n_eff+1500), n_eff=N_eff·T/h (heuristic fitted to the K=8 sim); λ<0.3 in year one.

**Calibration.** p̂=p_base+κ(p_raw−p_base), κ0=0.3; κ=(κ0/0.2²+κ̂/se²)/(1/0.2²+1/se²) from the logistic-slope fit; ignore κ̂ below ~1,000 events.

**Size.** μ̂_i=σ_h,i·Φ⁻¹(p̂_i); σ from EWMA (λ=0.94) daily vol×√h; covariance shrunk 20% to constant correlation 0.5 (Ledoit–Wolf style, **[UNVERIFIED]**). Aim a_i=c·μ̂_i/σ_h,i², c=0.25, clip 0.25 per name; long-only, gross ≤1. Scale by min(1, σ_target/σ̂_p), σ_target=10%. No live-Sharpe sizing before 2 years.

**Meta-label gate** (≥600 live events): L2 logistic, ≤5 features, label [side·(r_h−cost)>0], purge h, embargo h/4; m=2Φ(z)−1, no trade if m<0.05. Before that, size by |p̂| only.

**Turnover.** w_t=w_{t−1}+0.15·(a_t−w_{t−1}); no trade if |a_t−w_{t−1}|<max(0.25·sd(a_i), €25/NAV). Cost 7 bp one-way (stress 15 bp) on every paper fill. Trade only if |μ̂_i|·H_eff ≥ 3·c, H_eff=1/(1−φ̂) **[INFERENCE]**. Drop signals with φ̂<0.5 unless net SR>0 at 15 bp.

**Evaluation.** Primary: net active return vs owner's buy-and-hold; report IR with HAC t (lag h), PSR(0), DSR (N_eff from a trial ledger), MinTRL. Ledger row for every prompt, model, threshold or horizon change, with daily config hash. Looks at 63/126/252 days; no go/no-go on IR before 2 years. 6-month process gates: Brier skill >0 (90% CI), κ̂ within 0.3 of κ, turnover ≤8×/yr, realised cost ≤ modelled. Kill rule: trailing drawdown >20% (≈2× annual vol target), not t-stats.

**Ablations.** Shadow universe ≥30 names; paired replay; block-bootstrap CI; Holm over ≤6; Bayesian read-out.
