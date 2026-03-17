"""
dislocation_reversion.py — One-venue shock detection and fade.

Fires rarely when exactly one venue deviates and Kalshi followed the noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from .lag_arb import StrategySignal


@dataclass
class DislocationReversionStrategy:
    """
    Detect single-venue outlier and fade when Kalshi followed it.
    """

    def compute_signal(self, snapshot: dict) -> StrategySignal:
        per_venue_mids = snapshot.get("per_venue_mids") or {}
        per_venue_staleness = snapshot.get("per_venue_staleness") or {}
        synthetic_mid = snapshot.get("synthetic_mid")
        dislocation = snapshot.get("dislocation", 0.0)
        kalshi_prob_change_1s = snapshot.get("kalshi_prob_change_1s", 0.0)
        spot_return_1s = snapshot.get("spot_return_1s", 0.0)
        spot_confidence = snapshot.get("spot_confidence", 0.0)

        if synthetic_mid is None or synthetic_mid <= 0:
            return StrategySignal("dislocation_reversion", 0.0, "WAIT", "no_synthetic_mid", {})

        outlier_detected = False
        outlier_direction = 0.0

        deviations = []
        for venue, mid in per_venue_mids.items():
            if mid <= 0:
                continue
            dev = abs(mid - synthetic_mid) / synthetic_mid
            deviations.append((venue, dev, mid - synthetic_mid))

        high_dev = [(v, d, s) for v, d, s in deviations if d > 0.0015]
        low_dev = [(v, d, s) for v, d, s in deviations if d < 0.0005]

        if len(high_dev) == 1 and len(low_dev) >= 1 and len(high_dev) + len(low_dev) == len(deviations):
            outlier_detected = True
            outlier_direction = 1.0 if high_dev[0][2] > 0 else -1.0

        if not outlier_detected:
            return StrategySignal("dislocation_reversion", 0.0, "WAIT", "no_outlier", {})

        if dislocation <= 0.001:
            return StrategySignal("dislocation_reversion", 0.0, "WAIT", "dislocation_low", {})
        if spot_confidence < 0.85:
            return StrategySignal("dislocation_reversion", 0.0, "WAIT", "spot_confidence_low", {})

        kalshi_dir = 1.0 if kalshi_prob_change_1s > 0 else (-1.0 if kalshi_prob_change_1s < 0 else 0.0)
        if kalshi_dir != 0 and kalshi_dir != outlier_direction:
            return StrategySignal("dislocation_reversion", 0.0, "WAIT", "kalshi_mismatch", {})

        if abs(spot_return_1s) >= 0.0005:
            return StrategySignal("dislocation_reversion", 0.0, "WAIT", "spot_unstable", {})

        score = dislocation * spot_confidence * 0.5
        action = "BUY_YES" if outlier_direction < 0 else "BUY_NO"
        return StrategySignal("dislocation_reversion", score, action, "OK", {})
