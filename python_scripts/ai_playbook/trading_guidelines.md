# AI Master Trading Playbook

This document acts as the core memory and strategic guideline for the QuickTrade AI.
It is dynamically injected into the AI's brain before every trade evaluation.
The `daily_debrief.py` script automatically updates these rules based on real trading performance.

## Core Rules
- **Capital Preservation:** Never risk more than the defined maximum. Stop trading if conditions are extremely choppy.
- **Trend Confirmation:** Always require a secondary indicator (like high volume) before buying an EMA crossover.

## Time of Day Observations
- **Mornings (9:30 AM - 10:30 AM EST):** Highest volatility and best momentum. Breakouts are most reliable here.
- **Mid-day (11:00 AM - 2:00 PM EST):** Avoid taking new breakouts. Volume is generally dead, leading to fake-outs.
- **Power Hour (3:00 PM - 4:00 PM EST):** Look for trend continuations but maintain tight trailing stops.

## Market Regime Notes
- (Wait for daily_debrief.py to populate market regime notes based on recent performance)


## Debrief Notes (2026-05-08)
- **Refine After-Hours Breakout Logic:** While one significant win was observed in late after-hours trading, there were also multiple quick small losses. Investigate if this time window inherently increases chop or requires different risk parameters for `Breakout / Momentum` signals.
- **Improve Low-Priced Stock Filtering:** The bot accumulated several small losses on lower-priced stocks (`FFIE`, `SOUN`, `LCID`). Implement stricter filters or tighter stop-loss management specifically for `Breakout / Momentum` signals in stocks priced below $5 to prevent profit erosion.
- **Enhance Trend Confirmation for Momentum:** Analyze the underlying factors that allowed `PLTR` to trend beautifully post-breakout, contrasting them with tickers like `SOUN` and `LCID` that chopped out. Focus on identifying higher-conviction momentum entries.
- **Prioritize Quality over Quantity:** The pattern of one large win offsetting several small losses suggests the bot might be taking too many marginal trades. Emphasize higher probability setups, potentially even reducing trade frequency for better net PnL.
