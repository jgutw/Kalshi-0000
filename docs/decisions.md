# Design Decisions

## Post-Mortem Fixes (vs Polymarket Bot)

These changes address issues discovered during paper trading of the predecessor Polymarket 5-min bot:

| Issue | Old behavior | New behavior |
|-------|--------------|--------------|
| Circuit breaker | Permanent halt after 3 consecutive losses; required manual restart | 60 min cooldown; auto-resumes; win resets streak immediately |
| Stale 50/50 price | Gamma outcomePrices polled every 2s; never refreshed mid-window | Kalshi REST orderbook polled every 1s; `PRICE_MAX_AGE_SECS` guard rejects price unchanged >30s when near 50/50 |
| Hawkes decay | `HAWKES_DECAY=3.0` → half-life 0.23s; 99% decayed before next poll | `HAWKES_DECAY=0.046` → half-life 15s, matching 15-min market cadence |
| Single asset | BTC only | BTC, ETH, SOL, XRP run concurrently |
| Polling | 2s REST poll | 1s REST poll |
