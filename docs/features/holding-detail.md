# Holding detail and charts

[← Documentation index](../README.md)

Select a row on the Overview to open the detail pane next to the table.

![Holding detail with 3M chart and EMA20](../media/screenshots/holding-detail.webp)

## Price chart

- **Ranges**: 1D, 1W, 1M, 3M, 1Y. The series is the *EUR listing* of the holding (one authoritative series per
  holding); US live feeds are only shown as a labelled `usLive` reference.
- **Overlays**: EMA20, EMA50 and RSI14 (computed server-side from closed daily bars).
- **Analysis markers**: diamonds mark days on which an AI analysis was produced (legend: *Hold analysis*).
- **Advanced chart** opens the TradingView advanced-chart widget in a modal; **Open on TradingView** links out.

![Chart, 3 months](../media/screenshots/chart-3m.webp)

## Cards under the chart

| Card | Content |
|---|---|
| **Position** | Shares, value, EUR cost basis, average cost per share, P/L (EUR and %), weight, day P/L, max drawdown. |
| **Return attribution** | The holding's EUR return over a selectable range (1M … ALL) split into the local-currency return, the FX effect (against the holding's `exposureCurrency`) and the cross term; see [Overview](overview.md#performance-attribution-and-risk). Hidden when the holding has no attribution row. |
| **Lots** | Shown for holdings with dated lots: each lot with its remaining shares, age and, for a `taxResidency: "CZ"` setup, the unlock date of the 3-year time test (purchase date + 3 years + 1 day) and whether it is exempt today. From `/api/tax`; see [Settings](settings.md#portfolio-policy-and-tax). |
| **Valuation & signals** | P/E, forward P/E, dividend yield, ROE, sector/industry, analyst target and recommendation from the provider/fundamentals caches, with plain-language signals (e.g. "P/E very high", "Analysts target +24 % upside"). Data that cannot be sourced is shown as missing rather than guessed. |
| **News & alerts** | Recent alerts and news for this ticker, with the rule that fired. |
| **AI analysis** | *Quick / Standard / Deep* mode selector, **Analyse** button and the analysis history (rating, score, mode, one-line summary, price target, the 20-day outperformance probability and expected excess when present, the event trigger of a deep digest run, and a tax note on a confirmed sell-family change). See [AI analysis and reports](analysis-and-reports.md). |

On a phone the same content opens as a full-height drawer – see [Mobile and themes](mobile-and-themes.md).
