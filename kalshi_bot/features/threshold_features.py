"""
threshold_features.py — Threshold feature layer for 15-min digital options.

Computes z_threshold, p_base, mispricing, and confidence-weighted mispricing
from spot levels, volatility, and market price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from kalshi_bot.config import cfg
from kalshi_bot.prob_model import structural_prob

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass
class ThresholdFeatures:
    z_threshold: float
    p_base: Optional[float]
    mispricing_base: Optional[float]
    confidence_weighted_mispricing: Optional[float]
    spot_confidence: float
    lag_confidence: float


def compute_threshold_features(
    spot_now: float,
    spot_start: float,
    time_remaining_secs: float,
    annualized_vol: float,
    p_market: float,
    spot_confidence: float,
    lag_confidence: float,
) -> ThresholdFeatures:
    """
    Compute threshold features for ranking and filtering.

    confidence_weighted_mispricing is the central ranking signal:
    mispricing_base * spot_confidence * lag_confidence.
    """
    # Floor vol to prevent exploding z when realized vol is tiny (early window, few trades)
    vol_structural = max(annualized_vol, cfg.MIN_STRUCTURAL_VOL)

    z_threshold = 0.0
    p_base = structural_prob(
        spot_now=spot_now,
        spot_start=spot_start,
        time_remaining_secs=time_remaining_secs,
        annualized_vol=vol_structural,
    )

    # Compute z_threshold (use floored vol to match structural_prob)
    try:
        if (spot_now <= 0 or spot_start <= 0 or annualized_vol < 0
                or time_remaining_secs < 0):
            z_threshold = 0.0
        else:
            tau = time_remaining_secs / SECONDS_PER_YEAR
            sigma_tau = vol_structural * math.sqrt(tau)
            if sigma_tau < 1e-12:
                z_threshold = 0.0
            else:
                log_dist = math.log(spot_now / spot_start)
                if abs(log_dist) < 1e-15:  # spot_now == spot_start
                    z_threshold = 0.0
                else:
                    z_threshold = (
                        log_dist - 0.5 * vol_structural ** 2 * tau
                    ) / sigma_tau
    except (ValueError, ZeroDivisionError):
        z_threshold = 0.0

    mispricing_base = (p_base - p_market) if p_base is not None else None
    confidence_weighted_mispricing = (
        mispricing_base * spot_confidence * lag_confidence
        if mispricing_base is not None
        else None
    )

    return ThresholdFeatures(
        z_threshold=z_threshold,
        p_base=p_base,
        mispricing_base=mispricing_base,
        confidence_weighted_mispricing=confidence_weighted_mispricing,
        spot_confidence=spot_confidence,
        lag_confidence=lag_confidence,
    )
