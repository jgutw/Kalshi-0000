# `kalshi_bot` package

Core trading package for the Kalshi 15-minute multi-asset bot.

**Project documentation lives at the repo root:** see [`../README.md`](../README.md).

## Run from project root

```powershell
py -3 run_kalshi_bot.py --mode scan
py -3 run_kalshi_bot.py --mode run --fresh-round --session-tag my_round
py -3 -m kalshi_bot.test_engines
```

## Package map

| Module | Role |
|--------|------|
| `config.py` | Assets, thresholds, active profile |
| `kalshi_bot.py` | CLI + asyncio orchestrator |
| `asset_engine.py` | Per-asset decide / position / early-exit |
| `signal_engine.py` | Microstructure signals + fusion |
| `sim_state.py` | Paper bankroll + circuit breaker |
| `session_meta.py` | Round tags + archive / reset |
| `kalshi_client.py` | Kalshi REST + orders |
| `strategy/` | lag_arb, close_boundary, dislocation, router |
| `models/` | `micro_alpha_model` |
| `data/` | Venue feeds + synthetic spot |
