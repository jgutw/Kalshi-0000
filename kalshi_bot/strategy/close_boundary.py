"""
close_boundary.py — Close-to-expiry boundary strategy.

Fires in the last 2 minutes when structural mispricing is clear.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict

from .lag_arb import StrategySignal

LATE_WINDOW_ENTRY_SECS = 120
LATE_WINDOW_EXIT_SECS = 15


class CloseBoundaryStrategy:
    """
    Near-expiry repricing: trade when time is short, z_threshold is
    decisive, and structural mispricing exceeds threshold.
    """

    def __init__(self):
        self._bucket_0_30: dict = {"signal_count": 0, "scores": []}
        self._bucket_30_60: dict = {"signal_count": 0, "scores": []}
        self._bucket_60_120: dict = {"signal_count": 0, "scores": []}

    def _bucket(self, time_remaining: float) -> dict:
        if time_remaining <= 30:
            return self._bucket_0_30
        if time_remaining <= 60:
            return self._bucket_30_60
        if time_remaining <= 120:
            return self._bucket_60_120
        return {}

    @property
    def diagnostics(self) -> dict:
        def avg(b):
            return sum(b["scores"]) / len(b["scores"]) if b["scores"] else 0.0
        return {
            "bucket_0_30": {"signal_count": self._bucket_0_30["signal_count"], "avg_score": avg(self._bucket_0_30)},
            "bucket_30_60": {"signal_count": self._bucket_30_60["signal_count"], "avg_score": avg(self._bucket_30_60)},
            "bucket_60_120": {"signal_count": self._bucket_60_120["signal_count"], "avg_score": avg(self._bucket_60_120)},
        }

    def compute_signal(self, snapshot: dict) -> StrategySignal:
        time_remaining = snapshot.get("time_remaining_secs", 0.0)
        p_base = snapshot.get("p_base")
        p_market = snapshot.get("p_market")
        z_threshold = snapshot.get("z_threshold", 0.0)
        kalshi_spread = snapshot.get("kalshi_spread", 1.0)
        kalshi_quote_age = snapshot.get("kalshi_quote_age_secs", 999.0)
        spot_confidence = snapshot.get("spot_confidence", 0.0)

        if time_remaining > LATE_WINDOW_ENTRY_SECS:
            return StrategySignal("close_boundary", 0.0, "WAIT", "time_remaining_high", self.diagnostics)
        if time_remaining <= LATE_WINDOW_EXIT_SECS:
            return StrategySignal("close_boundary", 0.0, "WAIT", "time_remaining_low", self.diagnostics)
        if p_base is None or p_market is None:
            return StrategySignal("close_boundary", 0.0, "WAIT", "no_p_base", self.diagnostics)
        if abs(p_base - p_market) < 0.06:
            return StrategySignal("close_boundary", 0.0, "WAIT", "mispricing_weak", self.diagnostics)
        if abs(z_threshold) < 0.5:
            return StrategySignal("close_boundary", 0.0, "WAIT", "z_threshold_weak", self.diagnostics)
        if kalshi_spread >= 0.04:
            return StrategySignal("close_boundary", 0.0, "WAIT", "kalshi_spread_wide", self.diagnostics)
        if kalshi_quote_age >= 10:
            return StrategySignal("close_boundary", 0.0, "WAIT", "kalshi_quote_stale", self.diagnostics)
        if spot_confidence < 0.7:
            return StrategySignal("close_boundary", 0.0, "WAIT", "spot_confidence_low", self.diagnostics)

        bucket = self._bucket(time_remaining)
        if bucket:
            bucket["signal_count"] += 1
            score = (p_base - p_market) * spot_confidence
            bucket["scores"].append(score)

        score = (p_base - p_market) * spot_confidence
        action = "BUY_YES" if p_base > p_market else "BUY_NO"
        return StrategySignal("close_boundary", score, action, "OK", self.diagnostics)
