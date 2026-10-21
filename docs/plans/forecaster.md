# Plan: forecaster experiment (refined after the plan critique) — 2026-10-20

Inputs: [`docs/research/informed-agents.md`](../research/informed-agents.md) (design),
[`docs/research/plan-critique.md`](../research/plan-critique.md) (adversarial value-per-effort review) and the five
cited research reports in `docs/research/`.

## Owner constraints
- Local GPU: unlimited use as long as answers arrive in reasonable time (busy mainly when the owner games).
- Anthropic subscription (5-hour windows) is accepted for Opus/Sonnet calls, including as fallback when no lane runs.
  Upgrade to the 5x plan only if the experiment below justifies it. (Terms note recorded once in
  `claude-integration.md` §2; owner's decision.)
- Paper trading only (Trading 212 DEMO).

**Consequence:** compute is no longer the constraint. **Measurement power and overfitting are.** Free compute goes
into *more names and better logging*, not into more agent variants.

## Decisions
| Component | Decision | Why |
|---|---|---|
| Per-sample forecast log, point-in-time inputs, trial ledger (6a) | **Build** | It is the measuring instrument; builds on the existing scoreboard |
| Shadow universe of 50 liquid EU names, forecasts only (6d) | **Build** | Only way to get enough graded outcomes in months instead of years |
| Local pool N=8, independent, trimmed logit mean, fixed shrinkage (6c core) | **Build** | Best-supported aggregation; free compute |
| Opus blind forecaster (6e) | **Build** | Most useful Opus role; directly gradable |
| Cost model 7 bp (stress 15 bp), partial adjustment, no-trade band | **Build** | Best evidence for net results |
| Event cards: log from week 1, do not feed into forecasts yet | **Build (logging only)** | Point-in-time data cannot be recreated later |
| Debate / critique rounds | **Drop** | Evidence: zero or negative; sycophancy at 27–70B |
| LLM regime analyst | **Drop** (use code: VIX/curve) | Code does it better |
| Opus as judge / final decider / weekly reviewer | **Drop** | Anchoring and sycophancy; judges underperform the mean |
| Last-5-outcomes feedback block in prompts | **Drop** from forecasters | Outcome-chasing on tiny samples |
| Four-view slicing, evidence checker, Kelly sizing from p, peer map, GDELT, paid data | **Defer** to the week-12 readout | Not measurable before then |
| Hourly intraday bucket `i1` | **Keep as a cheap control** (orchestrator, against the critique's "kill") | Owner explicitly wants the measurement; it costs nothing extra once built and answers "does trading more often pay?" |

## First build (~10 working days)
1. **Forecast log + trial ledger** (~3 d): one row per sample (arm, source, model, seed, packet hash, as-of, latency);
   daily config hash; frozen packets; test that no input is time-stamped after the as-of.
2. **Shadow universe** (~4 d): 50 liquid EU names picked by an index rule (e.g. largest by Euro Stoxx / AEX / DAX
   membership) **before** looking at returns; packet builder reused from the paper PM; label = beat the universe median
   over 5 and 20 sessions.
3. **Arms** (~3 d), all on the same Friday-close packet:
   | Arm | What |
   |---|---|
   | R | constant 0.5 (no skill) |
   | Q | code: 12-1 momentum + 1-week reversal rank (free features, no LLM) |
   | A | one local sample |
   | B | local pool N=8, trimmed logit mean, shrink κ₀ = 0.3 |
   | D | Opus blind on a fixed 30-name subset |
   | E | fixed blend 0.65·B + 0.35·D |

## Experiment (pre-registered, frozen for 12 weeks)
- Comparisons (Holm over 4): B vs A, B vs Q, D vs B, E vs B. Primary metric: paired Brier difference on the 5-day
  label; secondary: IC, calibration slope.
- Looks at weeks 4 (sanity only), 8, 12; first real verdict at week 12; absolute skill vs R around week 26.
- **Fallback policy** (frozen before launch): local first; if no lane before the deadline → Opus → Sonnet → hold.
  Every row tagged by source; graded per source; sources never pooled in calibration; cohorts with > 10 % fallback are
  reported separately.
- Opus budget: ~33 blind calls per week (30 shadow + 3 held names), ≤ 15/day, ≤ 20 per 5-hour window, preferably at
  weekends; measure real usage in week 1.

## What the results decide
| Result at week 12 | Then |
|---|---|
| B beats A and is ≥ Q | The pool drives the practice buckets' forecasts; add one deferred feature group at a time |
| Q ≥ B | LLMs add nothing over free features → keep LLMs as information extractors only, simplify |
| E beats B by ≥ 0.01 Brier (90 % CI excludes 0) and D alone has IC > 0, and usage exceeds the windows | Upgrade to the 5x plan is justified |
| Anything else | Stay on the current plan; P&L alone never justifies spending more |

## Risks to watch
Overfitting through many versions (config frozen 12 weeks, ≤ 4 comparisons); lane availability (frozen packets,
missingness checked by weekday); Opus anchoring (always blind); hindsight in the universe choice (index rule);
an inert sizer at €1,000 (check shadow orders); demo fills are not real friction (cost model applied).

## Open universe (owner decision 2026-10-20, strategy version `s2`)
The practice buckets are no longer limited to the owner's 3 holdings. Goal: make money, measured by the benchmark.

- **Investable universe = the forecast universe** (~100 liquid names, chosen by an index rule before looking at
  returns): Euro Stoxx 50 + AEX + DAX 40 members, plus ~30 US large caps through their **EUR Xetra listings** on
  Trading 212 (e.g. `NVDd_EQ`) — all EUR, so no 0.15 % FX fee. Each name is verified on Trading 212 by ISIN, EUR and
  venue; names without a working Yahoo EUR series are dropped.
- **Separate paper registry** (`data/paper_universe.json`: id, ISIN, Yahoo symbol, T212 ticker, venue, kind, sector):
  these names are **not** added to the real watchlist, so the real dashboard, alerts and digest stay unchanged.
- **Flow:** code builds packets for every name → forecaster pool gives p(beat universe median, 5d/20d) per name →
  code ranks → the PM sees current holdings + the top ~15 candidates (with forecasts and evidence) and sets target
  weights → the risk engine enforces limits → demo orders.
- **Risk limits for €1,000 (code-enforced):** ≤ 10 positions; ≤ 20 % per stock, ≤ 40 % per ETF; ≤ 40 % per sector;
  ≥ 2 % cash; minimum position €50; daily turnover and order caps as now; partial adjustment and no-trade band.
- **Benchmarks:** primary = a broad world index ETF in EUR (MSCI World UCITS) held buy-and-hold; secondary = S&P 500
  (SXR8) and the owner's real-weights buy-and-hold (season `s1`), all shown side by side.
- **Seasons:** `s1` (3 tickers) is closed and kept for comparison; `s2` starts fresh with its own inception date, so
  versions are never mixed. The intraday bucket uses the same universe and limits with its own version `i2`.

**Status 2026-10-21 (Phase 6):** open universe built in code and unit-tested, not yet deployed or enabled: `paperTrading.universeSource: "paper_universe"`
(registry loader merges the seed, daily Trading 212 mapping, paper_universe pricing everywhere, risk limits `maxPositions` / `maxSectorPct` /
`minPositionEur`), the s2 daily PM and the i2 intraday agent + slot schedule, EUNL / SXR8 / real-weights benchmarks, `POST /api/paper/new-season`. The
owner switches by setting the source and calling new-season on an empty bucket; with no forecasts yet the PM holds. See `skills/paper-trading` ("Open universe").
