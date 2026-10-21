# Critique of informed-agents phases 6a–6g (revised for owner constraints, 2026-10-20)

Sources: [IA] informed-agents.md, [DE] decision-and-evaluation.md, [DC] debate-and-calibration.md, [LA] llm-trading-agents.md, [PS] predictive-signals.md, [CI] claude-integration.md, [PT] docs/plans/paper-trading.md, [SK] skills/paper-trading/SKILL.md. INFERENCE = my arithmetic from those reports.

## 0. Verdict

- **Binding constraints are measurement power and overfitting, not compute.** Local GPU costs only latency and availability (busy when the owner games). Opus/Sonnet is a fallback inside 5-hour windows.
- P&L is unreadable in 3 months: power is 7–17% at 3–6 months for IR 0.3–1.0 ([DE §1]). With κ0=0.3 and quarter-Kelly, p=0.55 becomes p̂=0.515 and a ≈13 pp tilt (INFERENCE from [DE §4, §9]), so practice P&L barely differs from buy-and-hold.
- Forecasts are barely readable either. Three names give ≈36 independent 20-day outcomes a year ([DC §7.4]). A 3-month paired ΔIC needs ≈0.04–0.06 even on 30–50 names ([DE §7]); plausible gains are 0.004–0.02 Brier ([DC §3]).
- More compute buys little: ensemble gains flatten past ≈5–15 runs ([DC §3]), and every extra arm is a counted trial ([DE §6]). Spend free compute on more names and logging, not variants.
- 6a is partly built: `scoreboard.py` has the advice log, `p_outperform_20d`, Brier, BSS, reliability, IC; `paper_metrics` has Wilson CIs and version segments ([SK], code grep).

## 1. Components

| Component | Benefit (evidence) | Effort | Load: GPU (latency only) / Opus | Readable ≤3 mo? |
|---|---|---|---|---|
| 6a per-sample log, PIT store, trial ledger | Foundation; Yahoo analyst fields lack vintages ([PS §2]) | S–M | – / 0 | It is the instrument |
| 6b event cards | Weak: LLM news drift is US, pre-cost, Sharpe 6.5→1.2, absent for small models ([PS §1]); poisoning ([DC §4]) | M | ≈30–60 calls (assumed) / 0 | No |
| 6b LLM regime analyst | Low; vol-managed sizing shown for factors only ([PS §1]); code computes it | S | 1 / 0 | No |
| 6b momentum, σ, earnings date | Already in the PM pack ([SK]) | ~0 | – / 0 | As baseline Q |
| 6c N=8 pool, trimmed logit mean | Best supported ([DC §3], vendor, unreplicated); ceiling ≈0.01 Brier ([DC §6]); same-model IC gain ≤1.3× ([DE §2]) | M | 24/day for 3 names, ≈400 for 50 / 0 | Only if ΔBrier ≥0.01–0.04 |
| 6c gated critique | Zero or negative; 27–70B models flip correct answers ([DC §2]) | S | extra / 0 | No |
| 6c shrinkage, clip | Cheap; fitted slope unusable before 150–1,000 events ([DC §8]) | S | – / 0 | No |
| 6c sizer: partial adjust, band, 7 bp | Best P&L evidence: daily full trading loses ≈45% of edge at 5 bp ([DE §5]); band/caps exist ([PT §5]) | M | – / 0 | Turnover, cost yes; P&L no |
| 6d shadow universe | Enabler: N_eff 3.6→8.5, MDE ΔIC .087→.057 at 63 d ([DE §7]) | M | free / 0 | Makes anything measurable |
| 6e Opus blind forecaster | Plausible heterogeneity ([DC §6]); no equity evidence; best LLM 0.122 vs 0.096 supers ([DC §3]) | S–M | – / ≈5 avg | Gross gap only |
| 6e Opus checker | Helps only with search ([DC §3]); output is flags | S | – / ≤0.5 | No |
| 6f earnings conditioning | Cheap; PEAD decayed in large caps ([PS §1]) | S | – / 0 | No |
| 6f peer map, GDELT | Customer momentum insignificant post-discovery ([PS §1]) | M | small / 0 | No |
| 6g paid data | Effect and EU vintages unverified ([PS §1–2]) | money | – | No |

## 2. KEEP / DEFER / KILL

**KEEP:** 6a on top of the scoreboard; 6c N=8 pool, fixed seeds, no debate, fixed shrinkage; cost model, partial adjustment, band; 6d shadow universe (50 names, compute is free); 6e Opus blind forecaster; **logging** (not using) event cards from week 1, since PIT cannot be recreated.

**DEFER to the week-12 readout:** event cards in forecasts; four-view slicing (extra trial, one model family); evidence checker; quarter-Kelly from p; peer map, GDELT; meta-labeling (≥600 events, [DE §3]); 6g.

**KILL:** LLM regime analyst (use code); critique and debate (evidence, not cost); Opus as judge, decider or weekly reviewer ([DC §3, §6]); the `feedback` block in forecaster packets (outcome-chasing on n=5); outside 6a–6g, the hourly `i1` bucket ([IA §1.6]).

## 3. Minimal first build (≤2 weeks) and experiment

**Work (≈10 days)**
1. 6a-lite (3 d): `forecast_log` row per sample (arm, source, model, seed, prompt/packet hash, as-of, latency); daily config hash in a trial ledger; frozen packets; test that no input is stamped after the as-of.
2. 6d-lite (4 d): 50 liquid EU names chosen by index rule before looking at returns; reuse `paper_pm.build_pack`. Label: beat the universe median over 5 and 20 sessions, close-to-close from the as-of close (base rate 0.5, beta removed).
3. Arms (3 d), same Friday-close packet: **R** constant 0.5; **Q** code rank of 12-1 momentum and 1-week reversal; **A** one local sample; **B** local pool N=8, trimmed logit mean, κ0=0.3; **D** Opus blind on a fixed 30-name subset; **E** 0.65·B+0.35·D, fixed in advance. Only the weekly cohort is analysed (no forking paths); held names also get B daily in shadow, never driving orders.

**Experiment**
- Pre-registered, Holm over 4: B vs A, B vs Q, D vs B, E vs B on the 30-name subset. Primary: paired ΔBrier, 5-day label. Secondary: IC, reliability slope, spread vs |error|.
- Week-block bootstrap plus posterior P(Δ>0), prior N(0, 0.01²) ([DE §7]). Looks at weeks 4 (sanity: completeness, failures, source mix), 8, 12. First inferential look: week 12.
- Readability (INFERENCE): week 12 ≈ 12 cohorts × 30 names ≈ 100 effective outcomes → MDE ≈0.01–0.02 ΔBrier at 5 days, ≈0.02–0.04 at 20 days (8 mature cohorts). Absolute skill vs R needs ≈week 26.
- **Next step** (pool feeds held-name forecasts, 6f): B beats A by ≥0.005 with P(Δ<0)≥0.8, B no worse than R, B's IC ≥ Q's. If Q ≥ B the LLM adds nothing over free features; keep LLMs as extractors ([LA]).
- **5x plan:** E beats B by ≥0.01 with a 90% CI excluding 0 at week ≥12 (prefer 20), **and** D alone IC>0 with P≥0.9, **and** a workload exceeding current windows. P&L never justifies it.

## 4. Where Opus adds the most

| Use | Value | Gradable | Budget |
|---|---|---|---|
| **Blind forecaster** | Highest: independent, no anchoring | Yes | 30 shadow + 3 held/week, ≤15/day, weekends |
| Evidence checker | Low; no search | No | 0 until week 12, then ≤3/week |
| Weekly review | Lowest; sycophancy | No | 0 |
| Nothing | Is arm B | – | – |

API list price ≈$0.12–0.35 per call ([CI §3], assumption) ≈ $17–50/month at 33 calls/week; measure real thinking tokens on the first 10 calls. Subscription allowances are unpublished ([CI §3], UNVERIFIED), so the 15/day cap is a guess to adjust after week 1.
Terms note (once): [CI §2] flags Consumer Terms §3(7) (automated access outside an API key) and §3(9) (securities) for the subscription path; [IA §5] recommends an API key. Owner decides; §5 is subject to it.

## 5. Fallback behaviour (model mix changes the forecaster)

One Opus sample is a different instrument from a local N=8 pool (other bias, no spread). Mixing them corrupts calibration and arm comparisons.

1. **Trigger:** lane fails a health or queue check and the deadline is near (cohort: Monday 08:00; held names: before the PM window). Local gets first chance. Trigger depends on lane state only, never on outcomes or forecast content.
2. **Order:** Opus, then Sonnet when the window is exhausted, then `hold_default` (row marked missing, PM holds). Never fill a gap after labels start.
3. **Tag every row:** `source` ∈ {local_pool, opus_blind, sonnet_blind}, `role` ∈ {scheduled, fallback}, model, n_samples, prompt hash. One blind Opus call per name per cohort serves as arm D and fallback, saving quota.
4. **Grade per source.** Brier, reliability, κ̂, IC per source; never pool sources in a calibration fit. Each keeps a fixed κ0 until ≥150 resolved forecasts. Sonnet and Opus are separate sources, descriptive while n is small.
5. **Common support:** compare arms on the same names and cohorts with the same source. A cohort is source-pure at ≥90% from one source, else "mixed", reported apart. If fallback exceeds 25% of cohorts, declare the primary comparison underpowered, do not pool.
6. **Bridging:** where local and Opus both exist for a name and cohort, report the per-source Brier gap as a quality estimate, not a calibration merge.
7. **Sizing:** source-specific p̂; E exists only when B and D both exist. Band and partial adjustment damp a source flip; log turnover caused by flips.
8. **Quota priority:** held-name fallback > scheduled D > shadow fallback; fixed ID order; ≤20 calls per 5-hour window (guess, tune after week 1).
9. Freeze this policy in the trial ledger before launch; any change is a trial.

## 6. Risks the design underplays

1. **Overfitting via versions:** N, views, κ, gate, blend weight, prompt text. Best of 10 zero-skill variants at 6 months has Sharpe ≈2.2 ([DE §6]). Freeze config 12 weeks, ≤4 comparisons.
2. **Lane availability:** the lane holds ≈31.5 of 32 GiB and can go half-dead with instant RSTs on new connections (memory notes); throughput is unmeasured ([DC §8]). Freeze packets so late runs use the same inputs; check missingness against day of week.
3. **Sycophancy:** showing p0 to Opus anchors ([DC §6]). Keep Opus blind; never feed rationales back.
4. **Leakage:** qwen3.8's cutoff is unknown ([LA] open question 1); past-date replays are contaminated ([LA §C]); a themed universe chosen after the AI rally ([PT §7.4]) embeds hindsight. Ablate forward only.
5. **Inert sizer at €1,000:** 3% band, €25 minimum and 15%/day adjustment may block most tilts; check shadow orders first.
6. **Demo fills are not real friction** ([PT §11]); charge 7 bp, stress 15 bp.

## 7. Go/no-go per later phase

| Phase | Go | No-go |
|---|---|---|
| 6a complete | All rows carry packet hash, as-of, source, config hash; byte-exact replay; leak test passes | >2% missing rows |
| 6c to production | §3 next-step rule at week 12; invalid output <5%; digest on time | B ≤ A and B ≤ Q |
| 6d continues | ≥45 of 50 names forecast per cohort | Packets need paid data |
| 6e continues | Opus failure <5%, source-pure cohorts ≥75%, D not worse than R by >0.02 Brier | Usage limits hit owner work |
| 6b cards in forecasts | After week 12; audit of 50 cards ≥90% correct; injection test; one ablation with P(ΔIC>0)≥0.6 | Audit <90% or P<0.4 |
| 6f features | One group at a time, ≤2 per quarter | Pool IC not positive (P<0.8) at weeks 12–16 |
| 6g paid data | ≥6 months of logs, P(ΔIC>0.02)≥0.8, vintages and EU coverage verified | Earlier |
| Kelly sizer drives orders | Forecast gate passed, then ≥8 weeks shadow orders, turnover ≤8×/yr, cost ≤ modelled | Gate missed; drawdown >20% ([DE §9]) |
| 5x plan | §3 rule | P&L-only or in-noise evidence |
