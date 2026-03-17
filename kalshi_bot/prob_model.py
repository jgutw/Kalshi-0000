"""
prob_model.py — Two-stage probability model.

Stage 1: structural_prob — models the "physics" of the contract (digital option
         P(S_T > S_0) under GBM given spot levels and remaining time).
Stage 2: microstructure overlay — applied in logit space in asset_engine.
"""

import logging
import math
from typing import Optional

from scipy.stats import norm

log = logging.getLogger("kalshi_bot.prob_model")
SECONDS_PER_YEAR = 365 * 24 * 3600


def structural_prob(
    spot_now: float,
    spot_start: float,
    time_remaining_secs: float,
    annualized_vol: float,
) -> Optional[float]:
    """
    P(S_T > S_0 | S_t, sigma, tau) under GBM with negligible drift.

    Formula:
        tau      = time_remaining_secs / SECONDS_PER_YEAR
        sigma_tau = annualized_vol * sqrt(tau)
        log_dist  = log(spot_now / spot_start)
        z         = (log_dist - 0.5 * sigma^2 * tau) / sigma_tau
        p_base    = Phi(z)

    Returns None on invalid inputs rather than silently returning 0.5.
    None must be handled by the caller as a skip signal, not a 50/50.

    Degenerate cases:
        time_remaining <= 0  →  deterministic (1.0 or 0.0)
        sigma_tau < 1e-12    →  deterministic (1.0 or 0.0)
        invalid prices       →  None
    """
    if spot_now <= 0 or spot_start <= 0:
        log.warning(f"Invalid spot: now={spot_now} start={spot_start}")
        return None
    if annualized_vol < 0:
        return None

    if time_remaining_secs <= 0:
        return 1.0 if spot_now > spot_start else 0.0

    tau = time_remaining_secs / SECONDS_PER_YEAR
    sigma_tau = annualized_vol * math.sqrt(tau)

    if sigma_tau < 1e-12:
        return 1.0 if spot_now > spot_start else 0.0

    log_dist = math.log(spot_now / spot_start)
    z = (log_dist - 0.5 * annualized_vol * annualized_vol * tau) / sigma_tau
    return float(norm.cdf(z))
