"""
micro_alpha_model.py — Parameterized microstructure alpha in log-odds units.

Produces alpha_micro from standardized features. Does not use Bayesian fusion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict

from kalshi_bot.config import MICRO_ALPHA_OVERRIDES

DEFAULT_COEFFICIENTS = {
    "obi": 0.30,
    "ofi_hawkes": 0.35,
    "microprice_dev": 0.25,
    "trade_sign_autocorr": 0.20,
    "lag_signal": 0.40,
    "response_gap": 0.30,
}

FEATURE_NAMES = list(DEFAULT_COEFFICIENTS.keys())


@dataclass
class MicroAlphaConfig:
    coefficients: Dict[str, float] = field(default_factory=dict)
    feature_means: Dict[str, float] = field(default_factory=dict)
    feature_stds: Dict[str, float] = field(default_factory=dict)


def _default_means() -> Dict[str, float]:
    return {k: 0.0 for k in FEATURE_NAMES}


def _default_stds() -> Dict[str, float]:
    return {k: 1.0 for k in FEATURE_NAMES}


class MicroAlphaModel:
    """
    Produces alpha_micro in log-odds units from standardized features.
    """

    def __init__(self, asset: str = "DEFAULT"):
        self.config = MicroAlphaConfig(
            coefficients=dict(DEFAULT_COEFFICIENTS),
            feature_means=_default_means(),
            feature_stds=_default_stds(),
        )
        overrides = MICRO_ALPHA_OVERRIDES.get(asset, {})
        for k, v in overrides.items():
            if k in self.config.coefficients:
                self.config.coefficients[k] = v

    def compute(self, features: dict) -> float:
        """
        Z-score each feature and accumulate alpha.
        Clamp result to [-1.5, 1.5].
        """
        alpha = 0.0
        for name, coeff in self.config.coefficients.items():
            val = features.get(name, 0.0)
            mean = self.config.feature_means.get(name, 0.0)
            std = max(self.config.feature_stds.get(name, 1.0), 1e-8)
            z = (val - mean) / std
            alpha += coeff * z
        return float(max(-1.5, min(1.5, alpha)))

    def update_stats(self, features: dict) -> None:
        """
        Online EMA update of feature_means and feature_stds.
        EMA alpha = 0.01 per call.
        """
        for name in self.config.coefficients:
            val = features.get(name, 0.0)
            mean = self.config.feature_means.get(name, 0.0)
            std = self.config.feature_stds.get(name, 1.0)
            new_mean = 0.99 * mean + 0.01 * val
            new_var = 0.99 * (std ** 2) + 0.01 * (val - mean) ** 2
            self.config.feature_means[name] = new_mean
            self.config.feature_stds[name] = max(math.sqrt(new_var), 1e-8)
