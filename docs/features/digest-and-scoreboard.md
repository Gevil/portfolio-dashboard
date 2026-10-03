# Digest and scoreboard

[← Documentation index](../README.md)

![Digest tab](../media/screenshots/digest.webp)

## Digest run

The digest is a scheduled batch (twice a day when `DIGEST_ENABLED=1`) that runs the AI analysis for every
alertable ticker, detects rating changes, retries failures and sends one consolidated push. The *Digest run*
card shows state, tickers done, errors, last batch and the next scheduled run; **Run digest now** starts a
batch manually. (In the demo instance the scheduler is disabled, hence the grey `DISABLED` chip.)

## Latest advice

One card per ticker: rating (BUY / HOLD / SELL), score, confidence, the lane and model that answered, and what
changed *since* the previous call (rating, action, score delta). **Full report** opens the
[report viewer](analysis-and-reports.md); *History* lists earlier calls.

## Failed & skipped

Tickers that failed or were skipped in the most recent runs with the reason (e.g. `lane_down`, budget
exhausted), so a silent gap in the digest is always explained.

## Scoreboard

> Investment output is a research aid, not advice. The scoreboard exists to measure how much to trust it.

Every advice row is graded against what happened afterwards:

- **Horizons** T+5 and T+20 trading days, measured from the next executable close of the EUR listing.
- **Excess return** versus the benchmark, converted to EUR (so FX does not flatter or hurt the call).
- **Verdicts** per call (hit / miss / neutral / pending) and **hit rate** with a 95 % **Wilson interval**.
- Breakdowns by horizon, rating, action, phase, model and a naïve **baseline**.
- **Insufficient sample** – any group with fewer than the configured minimum (n = 30 here) is flagged instead of
  being presented as a trustworthy percentage.
- **Was it right?** `+` / `−` buttons record your own verdict next to the computed one.

The numbers in the screenshots come from fictional advice rows graded against real market prices; they say
nothing about the quality of the pipeline.
