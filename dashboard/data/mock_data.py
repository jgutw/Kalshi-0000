"""
dashboard/data/mock_data.py — Realistic fake data for all schemas.

Simulates full 15-minute windows (900 seconds) for BTC, ETH, SOL, XRP.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta
from typing import List

from .schemas import (
    DecisionEvent,
    PortfolioSnapshot,
    StateSnapshot,
    TradeEvent,
)

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "NEAR", "ZEC"]

# Approximate spot levels for mock mode (must not reuse BTC prices for alts).
MOCK_SPOT = {
    "BTC": 65_000.0,
    "ETH": 1_900.0,
    "SOL": 76.0,
    "XRP": 1.05,
    "DOGE": 0.07,
    "BNB": 600.0,
    "HYPE": 55.0,
    "NEAR": 1.60,
    "ZEC": 510.0,
}

WAIT_REASONS = [
    "uncertain_near_50",
    "venue_dislocation",
    "lag_absent",
    "window_boundary",
    "circuit_breaker_cooldown",
    "edge(0.02<0.03)",
    "conviction(2<3)",
    "kalshi_quote_stale",
]
RAW_FEATURE_KEYS = [
    "obi", "ofi_hawkes", "microprice_dev",
    "trade_sign_autocorr", "lag_signal", "response_gap",
]


def _random_walk(start: float, steps: int, drift: float = 0.0, vol: float = 0.02) -> List[float]:
    """Correlated random walk for probabilities."""
    path = [start]
    for _ in range(steps - 1):
        step = drift + random.gauss(0, vol)
        path.append(max(0.05, min(0.95, path[-1] + step)))
    return path


def _smooth_walk(start: float, steps: int, low: float = -2.0, high: float = 2.0) -> List[float]:
    """Smooth z_threshold over time."""
    path = [start]
    for _ in range(steps - 1):
        step = random.gauss(0, 0.15)
        path.append(max(low, min(high, path[-1] + step)))
    return path


def _make_raw_features() -> dict:
    return {
        "obi": round(random.uniform(-0.5, 0.5), 4),
        "ofi_hawkes": round(random.uniform(-0.3, 0.3), 4),
        "microprice_dev": round(random.uniform(-0.02, 0.02), 4),
        "trade_sign_autocorr": round(random.uniform(-0.2, 0.2), 4),
        "lag_signal": round(random.uniform(-0.1, 0.3), 4),
        "response_gap": round(random.uniform(-0.01, 0.05), 4),
    }


def _mock_spot_now(asset: str, i: int = 0) -> float:
    base = MOCK_SPOT.get(asset, 100.0)
    # Scale noise with price level (~few bps)
    noise = base * random.uniform(-0.0008, 0.0008) + i * base * 0.00001
    return round(base + noise, 6 if base < 10 else 4 if base < 1000 else 2)


def generate_decisions() -> List[DecisionEvent]:
    """At least 30 DecisionEvents per asset, ~15/15/70 BUY_YES/BUY_NO/WAIT split."""
    events: List[DecisionEvent] = []
    base_ts = datetime.now(timezone.utc) - timedelta(minutes=45)

    for asset in ASSETS:
        n = 35
        p_market_path = _random_walk(0.50, n, drift=0.001, vol=0.015)
        p_base_path = [p + random.gauss(0, 0.02) for p in p_market_path]
        p_base_path = [max(0.05, min(0.95, x)) for x in p_base_path]
        z_path = _smooth_walk(0.0, n)

        actions = (["BUY_YES"] * 5 + ["BUY_NO"] * 5 + ["WAIT"] * (n - 10))
        random.shuffle(actions)

        for i in range(n):
            ts = (base_ts + timedelta(seconds=i * 25)).isoformat()
            action = actions[i]
            reason = "" if action != "WAIT" else random.choice(WAIT_REASONS)
            p_m = p_market_path[i]
            p_b = p_base_path[i]
            alpha = random.gauss(0, 0.02)
            p_r = max(0.05, min(0.95, p_b + alpha)) if p_b else None

            events.append(DecisionEvent(
                ts=ts,
                asset=asset,
                window_id=1773689400 + (i // 4) * 900,
                action=action,
                reason=reason,
                p_base=round(p_b, 4),
                p_real=round(p_r, 4) if p_r else None,
                p_market=round(p_m, 4),
                ev=round(p_r - p_m, 4) if p_r else None,
                z_threshold=round(z_path[i], 4),
                lag_confidence=round(random.uniform(0.2, 0.8), 4),
                spot_confidence=round(random.uniform(0.5, 1.0), 4),
                confidence_weighted_mispricing=round(
                    (p_b - p_m) * 0.7 * 0.5 if p_b else 0, 4
                ),
                alpha_micro=round(alpha, 4),
                strategy=random.choice(["lag_arb", "close_boundary", "dislocation_reversion"]),
                diagnostics={"signal_count": random.randint(1, 20)},
                raw_features=_make_raw_features(),
                time_remaining_secs=round(900 - i * 25 + random.uniform(-5, 5), 1),
                spot_now=_mock_spot_now(asset, i),
                spot_start=MOCK_SPOT.get(asset, 100.0),
                kalshi_quote_age_secs=round(random.uniform(1, 25), 1),
                kalshi_spread=round(random.uniform(0.02, 0.08), 4),
            ))

    return events


def generate_trades() -> List[TradeEvent]:
    """At least 8 closed TradeEvents, mix wins/losses, PnL -$15 to +$20."""
    events: List[TradeEvent] = []
    base_ts = datetime.now(timezone.utc) - timedelta(hours=2)
    balance = 1000.0
    wins = 0
    losses = 0

    trade_specs = [
        ("BTC", "yes", 0.55, 0.0, -6.0),
        ("BTC", "yes", 0.52, 1.0, 4.8),
        ("ETH", "no", 0.48, 1.0, 5.2),
        ("ETH", "yes", 0.58, 0.0, -8.4),
        ("SOL", "yes", 0.62, 1.0, 18.0),
        ("SOL", "no", 0.42, 0.0, -12.0),
        ("XRP", "yes", 0.50, 1.0, 10.0),
        ("XRP", "no", 0.55, 0.0, -11.0),
    ]

    for i, (asset, side, entry, exit_val, pnl) in enumerate(trade_specs):
        ts = (base_ts + timedelta(minutes=i * 18)).isoformat()
        balance += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        wr = wins / (wins + losses) if (wins + losses) > 0 else 0.0

        is_win = pnl > 0
        time_rem = random.uniform(45, 850) if is_win else random.uniform(45, 400)
        quote_age = random.uniform(2, 8) if is_win else random.uniform(12, 22)
        spot_conf = random.uniform(0.7, 1.0) if is_win else random.uniform(0.4, 0.6)
        lag_conf = random.uniform(0.4, 0.8) if is_win else random.uniform(0.25, 0.35)
        disloc = random.uniform(0.0002, 0.0008) if is_win else random.uniform(0.0008, 0.0018)
        cwm = random.uniform(0.03, 0.08) if is_win else random.uniform(-0.02, 0.03)

        contracts = 10
        events.append(TradeEvent(
            ts=ts,
            asset=asset,
            ticker=f"KX{asset}15M-TEST",
            side=side,
            entry=entry,
            exit=exit_val,
            contracts=contracts,
            amount_usdc=round(entry * contracts, 2),
            pnl=pnl,
            balance=balance,
            win_rate=round(wr, 4),
            strategy=random.choice(["lag_arb", "close_boundary"]),
            reason="OK" if is_win else "lag_confidence_low",
            time_remaining_at_entry=round(time_rem, 1),
            kalshi_spread_at_entry=round(random.uniform(0.02, 0.06), 4),
            kalshi_quote_age_at_entry=round(quote_age, 1),
            spot_confidence_at_entry=round(spot_conf, 4),
            lag_confidence_at_entry=round(lag_conf, 4),
            dislocation_at_entry=round(disloc, 6),
            confidence_weighted_mispricing_at_entry=round(cwm, 4),
        ))

    return events


def generate_state_snapshots() -> dict:
    """One StateSnapshot per asset, most recent. Include one halted state."""
    base_ts = datetime.now(timezone.utc).isoformat()
    snapshots = {}

    for i, asset in enumerate(ASSETS):
        halted = asset == "XRP"
        start = MOCK_SPOT.get(asset, 100.0)
        snapshots[asset] = StateSnapshot(
            ts=base_ts,
            asset=asset,
            window_id=1773689400,
            time_remaining_secs=round(900 - i * 100, 1),
            spot_now=_mock_spot_now(asset, i),
            spot_start=start,
            synthetic_confidence=round(0.3 + i * 0.2, 2),
            dislocation=round(random.uniform(0.0005, 0.0025), 6),
            z_threshold=round(random.uniform(-1.0, 1.0), 4),
            p_base=round(0.48 + i * 0.02, 4),
            alpha_micro=round(random.gauss(0, 0.01), 4),
            p_real=round(0.49 + i * 0.02, 4),
            p_market=round(0.50 + i * 0.01, 4),
            mispricing_base=round(-0.02 + i * 0.01, 4),
            confidence_weighted_mispricing=round(0.01 + i * 0.01, 4),
            lag_confidence=round(random.uniform(0.3, 0.7), 4),
            spot_confidence=round(0.5 + i * 0.15, 4),
            active_strategy="lag_arb" if i % 2 == 0 else "close_boundary",
            router_action="WAIT" if i == 2 else ("BUY_YES" if i % 2 == 0 else "BUY_NO"),
            wait_reason="uncertain_near_50" if i == 2 else "",
            open_position_side="yes" if i == 1 else None,
            open_position_entry=0.55 if i == 1 else None,
            open_position_contracts=10 if i == 1 else None,
            unrealized_pnl=2.5 if i == 1 else None,
            halt_state=halted,
            halt_reason="consec_loss_cooldown" if halted else "",
            kalshi_quote_age_secs=round(random.uniform(3, 18), 1),
            kalshi_spread=round(random.uniform(0.025, 0.065), 4),
        )

    return snapshots


def generate_portfolio() -> PortfolioSnapshot:
    """One PortfolioSnapshot: balance ~1000, 8 trades, 5 wins, 3 losses."""
    return PortfolioSnapshot(
        ts=datetime.now(timezone.utc).isoformat(),
        balance=1002.6,
        starting_balance=1000.0,
        peak_balance=1018.0,
        total_trades=8,
        wins=5,
        losses=3,
        win_rate=0.625,
        sharpe=0.82,
        var_95=0.012,
        consec_losses=0,
        halt_state=False,
        halt_reason="",
        asset_stats={
            "BTC": {"wins": 1, "losses": 2, "total_pnl": -7.6},
            "ETH": {"wins": 1, "losses": 1, "total_pnl": -3.2},
            "SOL": {"wins": 1, "losses": 1, "total_pnl": 6.0},
            "XRP": {"wins": 2, "losses": 0, "total_pnl": 7.4},
        },
        vault_balance=0.0,
        total_equity=1002.6,
        skimmable_profit=2.6,
    )
