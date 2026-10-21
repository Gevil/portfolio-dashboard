# Multi-agent debate, aggregation and calibration for LLM forecasting

Tags: **[R]** = reported in the paper only. **[INFERENCE]** = my extrapolation. **[UNVERIFIED]** = not read or not confirmed. Brier lower is better; 0.25 = always 50%. I read the full text of [1]-[7], [11]-[16] and the abstracts only of [8]-[10], [17]-[19]. No live 2026 ForecastBench numbers were readable.

## 1. Bottom line

1. Most of the measured gains come from **independent samples + aggregation + calibration + evidence quality**, not from agents talking to each other. [5] finds majority voting before any debate "accounts for most of the performance gains"; [4] finds debate usually loses to CoT or self-consistency (SC) at equal compute.
2. Debate is a near-martingale for homogeneous agents [5]. It can add value when agents differ in information or model, and it is harmful when agents are sycophantic [6][7].
3. For forecasting specifically, the best-documented recipe is [14]: agentic search, ~10 independent runs, a *search-capable* supervisor that reconciles disagreement, then Platt scaling. A plain LLM judge that reads the forecasts and emits a number was worse than the simple mean.

## 2. Debate

**Du et al. 2023 [1].** GPT-3.5 agents revise answers after reading peers' answers. With 3 agents and 2 rounds [R]: arithmetic 67.0→81.8%, GSM8K 77.0→85.0% (majority vote 81.0), MMLU 63.9→71.1. More agents helped monotonically; more than ~4 rounds gave no further gain. A "stubborn" prompt gave longer debates and better answers. The authors admit agents are "agreeable" and debates can converge on a confidently wrong answer. No majority-vote baseline was run on the factuality tasks. Evidence: one model family, verifiable-answer tasks.

**Liang et al. (MAD) [2].** Degeneration-of-Thought (DoT) is the claim that a self-reflecting LLM cannot revise a confident answer. MAD uses two debaters plus a judge. On Counter-Intuitive AR with GPT-3.5, accuracy was 26.0 baseline, 29.5 SC, 37.0 MAD, 51.0 GPT-4 [R]. Adding debaters hurt: COMET 84.4 (2), 83.1 (3), 82.9 (4). Strong debaters with a weak judge beat the reverse, and the judge favoured the side run by its own backbone model. Small, GPT-3.5-era [R].

**Independent re-evaluations (the strongest evidence).**
- [3] (ICML 2024): debate did not reliably beat SC or Medprompt, and was very hyperparameter-sensitive. Prompting "agree with others X% of the time" moved Multi-Persona by ~15 points on a USMLE subset. The best X was high on medical sets and reversed on counter-intuitive sets.
- [4]: 5 MAD methods × 9 benchmarks × 4 models. No method won against CoT in more than ~20% of the cells, and SC was better at matched tokens. Adding rounds or agents did not reverse this.
- [5] (NeurIPS 2025), Qwen2.5-7B average over 7 benchmarks: single 72.1, MAD (2 rounds) 73.8, MAD (5 rounds) 70.5, **majority vote with no debate 76.9**. Llama-3.1-8B: 62.0 / 69.3 / 67.6 / 72.4. Rounds beyond 2 *lowered* accuracy. Centralized (judge-aggregated) MAD was worst. A proof shows that homogeneous fully connected debate is a belief martingale. Two cheap interventions, "keep the majority answer" (MAD-Conformist) and "adopt the majority with 30% probability" (MAD-Follower), recovered ~76% [R, small models].

**Failure modes.**
- [6] used GPT-4o-mini, Llama-3.1-8B and Mistral-7B. Debate often *lowered* accuracy, including when strong models outnumbered weak ones. Correct→incorrect flips exceeded incorrect→correct flips. Flips were most likely when the agent was isolated, i.e. conformity.
- [7] used Qwen3-32B and Llama-3.3-70B, the size class we run. Sycophancy drives "disagreement collapse". Agent sycophancy score correlates with abandoning correct answers (r=0.90). Three agents collapsed less than two. A centralized judge cut collapse (CommonsenseQA DCR 82→41%). The judge's own sycophancy level mattered little. The best debater mixes were some "troublemaker" plus some "peacemaker" personas.

**Heterogeneous vs homogeneous.**
- [4]: randomly choosing GPT-4o-mini or Llama-3.1-70B per agent call lifted SoM by +6.4% relative over the two single-model averages [R, position paper, 2 models].
- [8] (single author): after 4 rounds, a mixed Gemini-Pro/Mixtral/PaLM-2-M trio reached 91% on GSM8K vs 82% for 3× Gemini-Pro [R].
- [9] attributes ReConcile's gains to model diversity plus confidence-weighted voting [abstract only].
- Counter-evidence: [6] found a weaker model in the room dragged the group down.
- [INFERENCE] Heterogeneity helps when errors are decorrelated and the members are individually decent. It does not rescue a bad member. In [11] one weak member (Coral, Brier 0.38) was individually terrible.

## 3. Aggregation and "wisdom of the silicon crowd"

**Schoenegger et al. 2024 [11].** Twelve LLMs, three samples each, via web UIs at default settings, on 31 binary Metaculus questions (Oct 2023-Jan 2024). The median-of-medians Brier was 0.20 against 0.19 for 925 humans (p=0.85, equivalence only inside wide ±0.08 bounds). Caveats:
- The LLM crowd (0.20) was *worse than its best member*: GPT-4 scored 0.15 and GPT-4+Bing 0.16.
- Mean forecast was 57.4% while only 14/31 resolved yes (acquiescence bias).
- n=31, and the ForecastBench authors note the study's low power [13].
- Study 2: showing the human median to GPT-4 and Claude 2 improved accuracy 17-28%, but a plain average of human and machine did better.

**Halawi et al. 2024 [12].** The test set had 914 questions published after the models' cutoffs. Brier: crowd 0.149, zero-shot GPT-4 0.208, full system 0.179. Ablation: base GPT-4 without retrieval 0.206, with retrieval 0.186, with a fine-tuned reasoner 0.179. The aggregation was a trimmed mean over 6 forecasts, chosen over mean, median, geometric mean and a USC variant on validation. Averaging the system with the crowd (crowd weighted 4×) gave 0.146 vs 0.149. The system beat the crowd only in selective regimes, e.g. crowd forecast in 0.3-0.7 (0.238 vs 0.240). It rarely output low probabilities (hedging). [R].

**ForecastBench [13].** It is a dynamic benchmark with no contamination, and the paper reports 17 LLMs. Superforecasters scored 0.096, the public 0.121, and the best LLM 0.122 (Claude 3.5 Sonnet given the crowd value). The best LLM without the crowd value scored 0.136. Non-agentic news retrieval did not help. LLMs were notably worse on combination (conjunction) questions. Its ensemble baselines (median, geometric mean, log-odds mean) were not extracted by me.

**AIA Forecaster [14] (vendor technical report, not peer reviewed, not independently replicated).** On ForecastBench FB-7-21 the Brier was 0.1076 vs 0.1110 for superforecasters (statistically indistinguishable). The pipeline was agentic search, M independent agents, a supervisor, and Platt scaling. Evidence:
- Ensembling: single forecast 0.1182; mean of 10 0.1140; median 0.1138; trimmed mean 0.1142. Steep gains to ~5 runs, small to ~15.
- Supervisors on FB-7-21: agentic (searches) 0.1125, non-agentic LLM judge 0.1168, best-of-k 0.1191, none 0.1199. The non-agentic judge "overemphasized outlier opinions".
- Search matters: on live closed markets, 0.100 with search vs 0.361 without (n=64). Providing market prices closes ~42% of the search gap.
- Corrections on FB-7-21: none 0.1140; log-odds extremization (fixed √3) 0.1085; Platt 0.1071-0.1104 (in-distribution vs out-of-distribution fit); isotonic 0.1097 (ID) / 0.1134 (OOD); OLS 0.1119. The report shows Platt on the arithmetic mean ≈ generalized log-odds extremization (Baron et al. 2014, √3 from Neyman-Roughgarden), cited secondhand.

## 4. Superforecasting techniques for LLMs

[15] (38 prompts, 4 models, 100 ForecastBench questions, preregistered, temperature 0): after multiple-comparison correction only *Base Rate First*, *Frequency-based reasoning* and *Step-back* beat the control (Brier −0.015 to −0.019, on a ~0.21 baseline). *Bayesian reasoning* and *Propose-Evaluate-Select* were worse (+0.025/+0.028). Pre-mortem, self-critique, simulated debate, "superforecaster persona" and "anti-overconfidence" showed no reliable effect. [R], n=100 questions. **Implication:** keep base-rate and reference-class steps; do not expect pre-mortems or red-teaming to move Brier on their own. Use them to generate disagreement cruxes (my use, untested).

**Retrieval risk [19] (2026 preprint, abstract only, 7-8B models, not replicated).** A single planted article flipped 56% of forecasts across 0.5 and 5 articles flipped 69-73%. Allow-lists, isolate-then-aggregate and perplexity filters each had a cheap bypass. Relevant to our RSS/GDELT triage.

## 5. Calibration

- **Verbalized vs sampled.** For RLHF models, verbalized confidence beat token probabilities on QA (relative ECE reduction ~50%) [17]. Verbalized confidence is still overconfident, and consistency across samples plus better aggregation helps [18]. Neither is forecasting-specific. For forecasts, everything above uses verbalized probabilities. The sampled analogue is the spread of the N verbalized values.
- **Hedging vs overconfidence.** Frontier chat models hedge toward 50% [14][12]. RL-trained reasoners can be overconfident (39% of predictions in the 0-10%/90-100% tails with unguarded GRPO) [16]. A 14B model trained with outcome RL reached Brier 0.190, ECE 0.062 (base 0.215/0.089; o1 0.202/0.093; market 0.151/0.043) with a 7-sample median [16][R].
- **Method ranking.** Platt/log-odds scaling ≥ isotonic > OLS in [14]. Isotonic degraded out of distribution.
- **Not found:** the effect of sampling temperature on forecast accuracy [UNVERIFIED]. [14]'s Jensen argument says independent errors average out.

## 6. When a frontier model over weaker debaters helps or hurts

- **Helps:** as an *independent, blind* member of the pool (real heterogeneity), and as an *agentic* reconciler that searches to resolve flagged cruxes [14]. Strong debaters with a weaker judge worked better than the reverse [2]; weaker judges can pick well from persuasive expert debate [10][abstract].
- **Hurts:** as a no-search judge that reads forecasts and outputs a number [14]. Also when it sees a tally of weaker agents' views and anchors (conformity [6]). Judges prefer their own backbone's output [2]. [11] Study 2: exposure helps the receiving model, but averaging beats exposure.
- [INFERENCE] Final probability should be computed by formula (logit-blend with a capped weight), not by the frontier model's free-text synthesis.
- **Domain caveat [INFERENCE].** None of the cited work covers 20-day relative equity returns. Prices already embed public news, and [14] shows market prices partly substitute for search on market questions. A realistic ceiling is a Brier within ~0.01 of the base-rate forecast. The scoreboard baseline must be the base rate, not 0.25. Extremizing, validated where forecasts carry real signal, may add error here.

## 7. Open questions

1. Does one gated critique round beat plain N-sample averaging for equity-relative questions? No source tests this.
2. What is our local model's hedging or overconfidence direction? Unknown until measured.
3. Do persona/evidence-slice diversity and a frontier member decorrelate errors enough, given a single Qwen family? Unknown.
4. Effective sample size: 3 holdings × ~12 non-overlapping 20-day windows ≈ 36 independent outcomes per year [INFERENCE]. With per-question Brier SD ≈ 0.1, SE ≈ 0.016 at n=36, so only ~0.03+ differences are detectable. Protocol A/B on our own data is not feasible for a year; use public question sets (Halawi's, ForecastBench) for the protocol A/B and our own data only for monitoring.

## 8. Implications for our setup: recommended protocol

Principle: **independent-first, debate-gated, formula-aggregated, calibration-last.** Default path has no debate.

**Per ticker × decision (daily full run; hourly only when new evidence arrives):**
0. **Evidence packet, once** (≤3k tokens). Price/technical summary, fundamentals snapshot, ≤8 de-duplicated news items from ≥2 independent source domains (poisoning guard [19]), and own-scoreboard base rate for "outperform_20d". Cache the shared prefix.
1. **Round 0: N=8 independent forecasts**, temperature ~0.8 [INFERENCE]. Four views (fundamentals, event/news, technical/flow, base-rate contrarian) × 2 seeds, each seeing a different evidence slice plus the shared base rate. Each output has reference class → base rate → for/against → one crux → p, ≤900 tokens, no peer visibility. Ask for the "outperform" question in half the samples and "underperform" (1−p) in the other half to cancel acquiescence [INFERENCE].
2. **Aggregate:** trimmed logit-mean p0 (drop min and max), spread s = SD of the logits.
3. **Gate.** Run critique only if s > ~0.5 logit, or if p0 would cross our action threshold. [INFERENCE: assume ~30% of decisions.]
4. **Round 1 (one round only; ≥2 rounds hurt in [5]).** 4 agents see the *cruxes and numbers* of the others, not a vote tally. Prompt: "stubborn", change p only on a new fact from the packet, with |Δlogit| ≤ 0.5 [1][5][6]. Log p before and after.
5. **Optional frontier step** (only when gated, or weekly per name). (a) Blind forecast from the evidence packet only, before seeing local output. (b) A second call sees the local cruxes plus p0 and lists contradicted or missing facts, with search if available (supervisor role [14]). Final logit = 0.65·local + 0.35·frontier [INFERENCE starting weight; tune on public data].
6. **Calibrate and clip.** See below. Clip to [0.05, 0.95].
7. **Log everything:** per-sample p, pre/post-debate p, frontier p, spread, flipped-answer counts.

**Calibration schedule:**
- n<150 resolved questions: no fitted extremization. Use p'=sigmoid(logit(p)−b) with b set from the observed base rate, slope fixed at 1.0.
- n≥150: logistic recalibration (slope, intercept) with L2 prior at (1, 0).
- n≥1000: consider isotonic [14].
- Pre-fit slope on public ForecastBench-style questions using the same local pipeline, and apply it to equities only after checking whether the slope stays >1.
- Report Brier skill vs base rate and 5-bin ECE, with block bootstrap by week.

**Cost (assumptions [UNVERIFIED]):** 27B NVFP4 decode ≈100 tok/s single-stream and ≈4× aggregate with concurrency 4-8, on a lane shared with other work. The one given datum is ~3 s for a 4k-token prompt, so prefill is cheap.

| Step | Output tokens | Sequential | Batched |
|---|---|---|---|
| Round 0 (8 samples) | ~7.2k | ~75 s | ~20-30 s |
| Gated critique (4 × 600, ~30% of runs) | ~2.4k | ~25 s | ~8 s |
| Frontier (blind + supervisor, ~1.5k out, ~6-8k in) | n/a (subscription) | ~30-60 s [INFERENCE] | n/a |
| **Total local per decision** | **~7-10k** | **~75-100 s** | **~25-40 s** |

3 holdings once daily is ~5 min of local time. At 5-10 names with hourly re-runs, cut to N=4, no critique, and re-run only on event triggers. A full pass for 10 names is ~15 min sequential [INFERENCE].

**Validation before trusting it:** run arms A (single), B (N=8 mean), C (+critique), D (+frontier), E (+calibration) on ≥300 resolved public questions with the same local models, using time-split data to avoid leakage. Ship C or D only if each beats B by more than its bootstrap CI. Otherwise ship B plus calibration, which is what the literature best supports.

## References
[1] Du et al., Improving Factuality and Reasoning... through Multiagent Debate, 2023. https://arxiv.org/abs/2305.14325
[2] Liang et al., Encouraging Divergent Thinking... through Multi-Agent Debate, 2023/EMNLP 2024. https://arxiv.org/abs/2305.19118
[3] Smit et al., Should we be going MAD?, ICML 2024. https://arxiv.org/abs/2311.17371
[4] Zhang et al., Stop Overvaluing Multi-Agent Debate, 2025. https://arxiv.org/abs/2502.08788
[5] Choi, Zhu, Li, Debate or Vote, NeurIPS 2025. https://arxiv.org/abs/2508.17536
[6] Wynn et al., Talk Isn't Always Cheap, 2025. https://arxiv.org/abs/2509.05396
[7] Yao et al., Peacemaker or Troublemaker, 2025. https://arxiv.org/abs/2509.23055
[8] Hegazy, Diversity of Thought... Multi-Agent Debate, 2024. https://arxiv.org/abs/2410.12853
[9] Chen et al., ReConcile, 2023. https://arxiv.org/abs/2309.13007
[10] Khan et al., Debating with More Persuasive LLMs..., 2024. https://arxiv.org/abs/2402.06782
[11] Schoenegger et al., Wisdom of the Silicon Crowd, 2024. https://arxiv.org/abs/2402.19379
[12] Halawi et al., Approaching Human-Level Forecasting with Language Models, 2024. https://arxiv.org/abs/2402.18563
[13] Karger et al., ForecastBench, ICLR 2025. https://arxiv.org/abs/2409.19839
[14] Alur et al., AIA Forecaster: Technical Report, 2025. https://arxiv.org/abs/2511.07678
[15] Schoenegger et al., Prompt Engineering LLMs' Forecasting Capabilities, 2025. https://arxiv.org/abs/2506.01578
[16] Turtel et al., Outcome-based RL to Predict the Future, 2025. https://arxiv.org/abs/2505.17989
[17] Tian et al., Just Ask for Calibration, 2023. https://arxiv.org/abs/2305.14975
[18] Xiong et al., Can LLMs Express Their Uncertainty?, 2023. https://arxiv.org/abs/2306.13063
[19] Lu & Zhang, The Corroboration Illusion, 2026. https://arxiv.org/abs/2609.22246
