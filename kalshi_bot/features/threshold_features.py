"""
threshold_features.py — Threshold feature layer for 15-min digital options.

Computes z_threshold, p_base, mispricing, and confidence-weighted mispricing
from spot levels, volatility, and market price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scipy.stats import norm

from kalshi_bot.config import cfg
from kalshi_bot.prob_model import structural_prob


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

    p_base = structural_prob(
        spot_now=spot_now,
        spot_start=spot_start,
        time_remaining_secs=time_remaining_secs,
        annualized_vol=vol_structural,
    )

    # Derive z_threshold from p_base via inverse normal CDF; preserve deterministic direction
    if p_base is None:
        z_threshold = 0.0
    elif p_base <= 0.0:
        z_threshold = float("-inf")
    elif p_base >= 1.0:
        z_threshold = float("inf")
    else:
        z_threshold = float(norm.ppf(p_base))

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
