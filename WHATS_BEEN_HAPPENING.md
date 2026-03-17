# What the Bot Has Been Doing (and Why It Felt Broken)

## Architecture Before Rearchitecture

When you run `python polymarket_bot.py --mode run`, **5 things run at once**:

| Component | Interval | What it does | Why same market repeats |
|-----------|----------|--------------|-------------------------|
| **BTC Engine** | Real-time WebSocket | Connects to Binance + Polymarket, trades ONE 5-min BTC market when Binance lag vs Polymarket price creates edge | N/A — trades one market per window |
| **Arbitrage thread** | Every 60 sec | Scans 200 Polymarket markets for YES+NO mispricings (BUY_BOTH or 99¢ resolution) | Same 200 markets every scan. Polymarket API returns similar order. Trump impeachment at 96.6¢, weather buckets, etc. appear every time. |
| **Weather thread** | Every 300 sec | Scans 6 cities × 4 days for NWS forecast vs Polymarket temp buckets | Same cities, same date ranges. NYC 60°F bucket, Chicago 39°F, etc. show up every scan. |
| **Events thread** | Every 120 sec | Scans for headline keywords (approved, denied, etc.) | Same markets with those keywords. |
| **Maker thread** | Every 60 sec | Quotes near-50/50 markets with $3k+ liquidity | Same liquid markets. |

## Why You Kept Seeing the Same Market

- The arb engine finds **Trump impeachment at YES=0.966** every 60 seconds because:
  1. It's still an active market
  2. The bot scans the same market list each time
  3. **There is no position tracking** — the bot does not remember it already "bought" this market
  4. So it "buys" again every minute

- Same for weather (same buckets), events (same headlines), maker (same quotes).

## Why total_trades = 0 After 24 Hours

- `place_order()` in DRY_RUN mode only logs `[SIM] BUY $30 on token`
- It **never**:
  - Tracks positions
  - Calls `record_trade()` when a position would resolve
- So no trade is ever "closed" and recorded in `paper_trades.jsonl` or `simulation.json`
- The dashboard stays at 0 trades because **no trades are being recorded**

## What Actually Worked

- **BTC Engine**: Connects to Binance + Polymarket WebSockets, gets real-time data, runs `make_decision()` on every Polymarket price tick. If it found a 5-min market and the WebSockets stayed connected, it was processing.
- **Arb/Weather/Events**: Correctly *detect* opportunities. The logic is fine. The problem is (a) no position tracking so same market repeats, (b) no trade recording so nothing shows in dashboard.

## Summary

The bot was **detecting** real opportunities and **logging** simulated buys, but:
1. It kept "buying" the same markets because it doesn't track what it already owns
2. It never recorded closed trades, so the dashboard showed nothing

The **BTC 5/15-min engine** is different: it trades ONE market per 5-min window, doesn't have the "same market spam" problem, and is the cleanest to focus on.
