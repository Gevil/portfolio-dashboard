# Digest and scoreboard

[← Documentation index](../README.md)

![Digest tab](../media/screenshots/digest.webp)

## Digest run

The digest is a scheduled batch (08:45 and 16:00 local time when `DIGEST_ENABLED=1`) that runs the AI analysis for every
analysable holding, detects rating changes, retries failures and sends one consolidated push. The *Digest run*
card shows state, tickers done, errors, last batch and the next scheduled run; **Run digest now** starts a
batch manually. (In the demo instance the scheduler is disabled, hence the grey `DISABLED` chip.)

- **Trading days only.** A slot where none of the analysable holdings' venues has a session that day (weekend or exchange
  holiday, from the venue calendars) is not run. It is recorded as `deferred: no session today` and counts as a successful
  run, not a failure. Other deferrals (no serving lane, no digest budget left) are recorded the same way (`deferred: ...`,
  ok) and retried after a while; a real failure stays a failure.
- **Hysteresis.** A changed rating, action or score band is first only *pending*. It becomes the published state when the same
  candidate appears on the next run, or at once when the score moved at least 10 points beyond the edge of the band it
  crossed into. A→B→A never pushes; A→B→B pushes once.
- **Drafts.** Only answers from the publishing lanes (`DIGEST_PUBLISH_LANES`, default: the first lane in the preference
  order) can publish. An answer from any other lane is a *draft*: it is logged for the scoreboard but never confirms a
  change or triggers a push.
- **Push policy.** Only a batch with a confirmed change (or a failure) is pushed: priority 3, or 4 for a confirmed move
  into the sell family (sell / reduce / trim / avoid) or a score move of 15 points or more. Never priority 5. A quiet batch
  is only stored in the alert center unless `DIGEST_PUSH_NO_CHANGE=1`. The "digest did not run" notice goes to the ops topic.
- **Event-driven deep runs.** At a scheduled slot a held ticker gets a *deep* analysis instead of a quick one when its
  earnings date is today or yesterday, a new 8-K / 6-K filing appeared since its last run, or triage holds a high-relevance
  verdict (≥ 0.85) for it. At most one deep run per ticker and day; it falls back to quick when the digest budget cannot
  hold it. The trigger (`earnings`, `filing`, `triage`) is shown on the push line and the advice card.
- **Czech tax tag.** With `taxResidency: "CZ"`, a confirmed move into the sell family of a holding that still has lots
  younger than 3 years adds a tax note to the push and the advice row (see [Settings](settings.md#portfolio-policy-and-tax)).
- **Odds line.** Each ticker shows `p(outperform 20d)` and the expected excess return versus the performance benchmark
  when the model provided them.

## Latest advice

One card per ticker: rating (BUY / HOLD / SELL), score, confidence, the lane and model that answered, and what
changed *since* the previous call (rating, action, score delta). The advice data (`/api/advice`) carries the **published**
call, an unconfirmed candidate as `pending`, a `draft` flag, and `p20` / expected excess (from the model's
`p_outperform_20d` and `expected_excess_20d_pct`). **Full report** opens the
[report viewer](analysis-and-reports.md); *History* lists earlier calls.

## Failed & skipped

Tickers that failed or were skipped in the most recent runs with the reason (e.g. `lane_down`, budget
exhausted), so a silent gap in the digest is always explained.

## Scoreboard

> Investment output is a research aid, not advice. The scoreboard exists to measure how much to trust it.

Every advice row is graded against what happened afterwards (engine `decision-signal-v3`):

- **Horizons** T+5 and T+20 trading days, measured from the next executable close of the EUR listing.
- **Benchmark.** The EUR series of the **performance benchmark** (`performanceBenchmark`, e.g. `SXR8`, a total-return
  comparison that includes reinvested dividends when the benchmark is an accumulating ETF). If none is configured, the S&P 500
  index converted to EUR is used.
- **Verdicts** per call (hit / miss / neutral / pending) for *directional* actions (buy / add / sell / reduce / trim /
  avoid), and **hit rate** with a 95 % **Wilson interval**. **Hold / watch rows are not graded hit or miss**: they only enter
  the probabilistic scores.
- **Probabilistic scores**, from the model's 20-day probability of beating the benchmark: **Brier score**, the **Brier skill
  score (BSS)** against a constant base-rate forecast, **log score**, a **reliability** table (five probability bins: forecast
  vs observed frequency) and the **information coefficient (IC)**, the rank correlation of the score with the realised excess
  return. Rows without a probability are excluded and counted.
- **Baselines.** Naive rules graded on the same anchors: `always_hold` (never hit/miss, p = 0.5), `trend_10m` (buy when
  the price is above its 10-month ≈ 210-session average, else sell) and `momentum_12_1` (buy when the return from 12 to 1 months
  ago is positive, else sell), plus the buy-and-hold row. The LLM has to beat these to deserve trust.
- Breakdowns by horizon, rating, action, phase and model (a *draft* flag is kept per row).
- **Insufficient sample** – any group with fewer than the configured minimum (n = 30 here) is flagged instead of
  being presented as a trustworthy percentage.
- **History kept.** Rows graded under the earlier engine (v2) are regraded; their old results stay under `v2` on the row.
- **Was it right?** `+` / `−` buttons record your own verdict next to the computed one.

The numbers in the screenshots come from fictional advice rows graded against real market prices; they say
nothing about the quality of the pipeline.
