"""
lag_arb.py — Lag arbitrage strategy.

Fires when Kalshi lags spot and mispricing is meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class StrategySignal:
    strategy: str
    score: float
    action: str  # "BUY_YES", "BUY_NO", or "WAIT"
    reason: str
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class LagArbStrategy:
    """
    Lag arbitrage: trade when Kalshi lags spot and confidence-weighted
    mispricing exceeds threshold.
    """

    def __init__(self):
        self.signal_count = 0
        self.trade_count = 0
        self._lag_sizes: list = []
        self._lag_confidences: list = []

    @property
    def diagnostics(self) -> dict:
        avg_lag = sum(self._lag_sizes) / len(self._lag_sizes) if self._lag_sizes else 0.0
        avg_conf = sum(self._lag_confidences) / len(self._lag_confidences) if self._lag_confidences else 0.0
        return {
            "signal_count": self.signal_count,
            "trade_count": self.trade_count,
            "avg_lag_size": avg_lag,
            "avg_lag_confidence": avg_conf,
        }

    def compute_signal(self, snapshot: dict) -> StrategySignal:
        """
        All seven entry conditions must be true for a non-WAIT signal.
        """
        p_base = snapshot.get("p_base")
        p_market = snapshot.get("p_market")
        lag_confidence = snapshot.get("lag_confidence", 0.0)
        spot_confidence = snapshot.get("spot_confidence", 0.0)
        cwm = snapshot.get("confidence_weighted_mispricing")
        kalshi_quote_age = snapshot.get("kalshi_quote_age_secs", 999.0)
        kalshi_spread = snapshot.get("kalshi_spread", 1.0)
        dislocation = snapshot.get("dislocation", 1.0)
        time_remaining = snapshot.get("time_remaining_secs", 0.0)

        if lag_confidence < 0.15:
            return StrategySignal("lag_arb", 0.0, "WAIT", "lag_absent", self.diagnostics)
        if lag_confidence < 0.30:
            return StrategySignal("lag_arb", 0.0, "WAIT", "lag_confidence_low", self.diagnostics)
        if kalshi_quote_age >= 20:
            return StrategySignal("lag_arb", 0.0, "WAIT", "kalshi_quote_stale", self.diagnostics)
        if kalshi_spread >= 0.06:
            return StrategySignal("lag_arb", 0.0, "WAIT", "kalshi_spread_wide", self.diagnostics)
        if dislocation >= 0.002:
            return StrategySignal("lag_arb", 0.0, "WAIT", "dislocation_high", self.diagnostics)
        if spot_confidence < 0.6:
            return StrategySignal("lag_arb", 0.0, "WAIT", "spot_confidence_low", self.diagnostics)
        if cwm is None or abs(cwm) < 0.03:
            return StrategySignal("lag_arb", 0.0, "WAIT", "mispricing_weak", self.diagnostics)
        if time_remaining <= 60:
            return StrategySignal("lag_arb", 0.0, "WAIT", "time_remaining_low", self.diagnostics)

        self.signal_count += 1
        self._lag_confidences.append(lag_confidence)
        if p_base is not None and p_market is not None:
            self._lag_sizes.append(abs(p_base - p_market))

        score = cwm
        action = "BUY_YES" if score > 0 else "BUY_NO"
        return StrategySignal("lag_arb", score, action, "OK", self.diagnostics)
