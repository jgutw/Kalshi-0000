# BTC 5-Minute Bot — Decision Logic & Diagnosis

This guide explains how the bot diagnoses each 5-minute market, when it starts looking, what mathematics it uses, and how to use the decision log for improvement.

---

## 1. When Does the Bot Look at a 5-Minute Market?

- **Window boundaries**: Every 5 minutes (300 seconds), aligned to UTC. Example: 13:50:00, 13:55:00, 14:00:00.
- **Start of analysis**: At the *first tick* after a new window begins (when Polymarket sends a price update).
- **Trading window**: The bot only considers trades between **30 seconds** after the window start and **15 seconds** before the window end.
  - **Skip first 30s** (`SKIP_OPEN_SECS`): Avoids price discovery noise; Binance and Polymarket need time to reflect the opening price.
  - **Skip last 15s** (`SKIP_CLOSE_SECS`): Too close to resolution; execution risk.

---

## 2. How It Diagnoses the Market Over the 5-Minute Period

### Data Sources

| Source | What It Provides |
|--------|------------------|
| **Binance** (aggTrade + depth20) | Real-time BTC price, volume, order flow |
| **Polymarket** (orderbook / WS) | YES price (market’s implied probability) |

### Core Idea: Oracle Lag

Polymarket’s price often lags Binance. The bot uses Binance’s real-time signal to estimate the *true* probability (“p_real”), compares it to Polymarket’s price (“p_market”), and trades when the edge is large enough.

---

## 3. Mathematics

### Step 1: Binance → Bias Score (Bullish vs Bearish)

Six signals are fused with Bayesian weights:

| Signal | Weight | Meaning | Math |
|--------|--------|---------|------|
| **OBI** | 35% | Order book imbalance | `(bid_vol - ask_vol) / (bid_vol + ask_vol)` |
| **OFI (Hawkes)** | 30% | Order flow imbalance with decay | Decayed buy vs sell volume (Hawkes process) |
| **VWAP** | 15% | Price vs volume-weighted average | `+1` if price > VWAP, `-1` if < VWAP |
| **EMA** | 10% | Fast vs slow trend | `+1` if EMA5 > EMA20, `-1` else |
| **Heikin-Ashi** | 7% | Smoothed candles | 3 green → +1, 3 red → -1 |
| **MRO** | 3% | Momentum / volume oscillator | Price×volume momentum |

Each signal is converted to a probability `(s + 1) / 2` (range 0–1) and fused with inverse-variance weighting. The final **bias** is `(fused_p - 0.5) * 2` (range -1 to +1).

### Step 2: Bias → p_real (Real Probability)

```text
p_real = sigmoid(logit(0.5) + bias * 0.60)
```

- `logit(0.5) = 0` (neutral).
- Positive bias → p_real > 0.5 (BTC more likely UP).
- The `0.60` scales bias sensitivity.

### Step 3: Logit-Space Tracker (Polymarket Price)

- Polymarket’s raw YES price is smoothed in logit space (EWMA, α=0.15) to reduce noise.
- **Belief vol**: volatility of logit changes, used to adjust min edge (more vol → require larger edge).

### Step 4: Edge & Kelly Sizing

- **Edge**: `ev = p_real - p_market` (for YES) or `1 - p_real - (1 - p_market)` (for NO).
- **Min edge**: `MIN_EDGE_PCT + 0.5 * belief_vol` (default ~5%, increases with Polymarket volatility).
- **Kelly fraction**: `(p_hat - q_market) / (1 - q_market)` × 0.25, capped at 3% of balance.

### Step 5: Filters Before Trading

- **Conviction**: At least 3 of 5 primary signals must align (OBI, OFI, VWAP, EMA, HA).
- **Volatility**: If Binance realized vol (annualized) > 80%, no trade.
- **Sharpe** (after 10+ trades, 20+ total): Must be ≥ 1.2 or trading halts.
- **Circuit breaker**: 3 consecutive losses or 10% daily drawdown → halt.

---

## 4. Decision Log (`btc5m_decisions.jsonl`)

Each tick that reaches `make_decision` is logged with:

| Field | Meaning |
|-------|---------|
| `ts` | Timestamp (ISO) |
| `window_id` | Unix seconds of 5-min boundary |
| `yes_price_raw` | Polymarket YES price |
| `p_real` | Model’s implied probability |
| `p_market` | Market price (smoothed) |
| `ev` | Expected value / edge |
| `bias` | Binance fusion bias (-1 to +1) |
| `signals` | OBI, OFI, VWAP, EMA, HA, MRO values |
| `conviction` | 1–5 (aligned signals) |
| `action` | WAIT, BUY_YES, or BUY_NO |
| `reason` | Why WAIT (e.g. edge, conviction, window_boundary) |
| `vol_scalar` | Position size multiplier from vol regime |

### Using the Log to Improve

1. **WAIT reasons**: If most skips are `edge` or `conviction`, consider lowering `MIN_EDGE_PCT` or `MIN_CONVICTION`.
2. **Correlate signals with outcomes**: Compare `signals` (obi, ofi, vwap, etc.) for winning vs losing trades.
3. **Window timing**: Check if trades cluster near open/close and whether that hurts performance.
4. **bias vs p_real**: See if the 0.60 scaling fits your market; increase for stronger moves, decrease for stability.

---

## 5. Acting on Every 5-Minute Market

The bot is designed to evaluate every 5-minute window. It will:

- Skip if any filter fails (conviction, edge, vol, circuit breaker).
- Take at most **one position per window** (no doubling down).
- Resolve at window end using Binance’s closing price vs `price_to_beat`.

To make it act more often:

- Lower `--min-edge` (e.g. `0.03` for 3%).
- Lower `MIN_CONVICTION` in config (e.g. 2 instead of 3).
- Increase `KELLY_FRACTION` (default 0.25) for larger bets (higher risk).

---

## 6. Polymarket WebSocket vs REST

- **WebSocket** (preferred): Real-time price updates, lower latency.
- **REST fallback**: If the WS connection fails, the bot polls the CLOB orderbook every 2 seconds for the YES mid price. Paper trading works with REST only.
