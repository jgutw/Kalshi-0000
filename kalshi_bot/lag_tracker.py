"""
lag_tracker.py — Measures whether Kalshi is lagging Binance.

Keeps a rolling buffer of (binance_return, kalshi_prob_change) pairs.
If correlation is high and positive, the lag is real and exploitable.
If correlation is weak or absent, reduce position size or skip.
"""

from collections import deque
from typing import Optional

import numpy as np


class KalshiLagTracker:
    """
    Measures whether Kalshi is actually lagging Binance.

    Keeps a rolling buffer of (binance_return, kalshi_prob_change) pairs.
    If correlation is high and positive, the lag is real and exploitable.
    If correlation is weak or absent, reduce position size or skip.

    lag_signal:     float [-1, 1] — direction and strength of lag
    lag_confidence: float [0, 1]  — how reliable is the lag right now
    response_beta:  float — Kalshi prob change per unit log spot return
    response_gap:   float — underreaction signal (positive = Kalshi underreacted)
    """

    def __init__(self, window: int = 30):
        self.window = window
        self._binance_returns: deque = deque(maxlen=window)
        self._kalshi_changes: deque = deque(maxlen=window)
        self._last_binance_px: Optional[float] = None
        self._last_kalshi_prob: Optional[float] = None
        # Response beta: rolling OLS
        self._resp_spot_returns: deque = deque(maxlen=50)
        self._resp_kalshi_changes: deque = deque(maxlen=50)
        self._response_beta: float = 1.0
        self._last_spot_return: float = 0.0
        self._last_kalshi_change: float = 0.0

    def update(self, binance_price: float, kalshi_prob: float) -> None:
        spot_ret = None
        kalshi_chg = None
        if self._last_binance_px is not None:
            spot_ret = (binance_price - self._last_binance_px) / self._last_binance_px
            self._binance_returns.append(spot_ret)
        if self._last_kalshi_prob is not None:
            kalshi_chg = kalshi_prob - self._last_kalshi_prob
            self._kalshi_changes.append(kalshi_chg)
        if spot_ret is not None and kalshi_chg is not None:
            self.update_response_beta(spot_ret, kalshi_chg)
        self._last_binance_px = binance_price
        self._last_kalshi_prob = kalshi_prob

    def update_response_beta(
        self,
        spot_return: float,
        kalshi_prob_change: float,
    ) -> None:
        """
        Online OLS estimate: beta = cov(kalshi_changes, spot_returns) / var(spot_returns).
        """
        self._resp_spot_returns.append(spot_return)
        self._resp_kalshi_changes.append(kalshi_prob_change)
        self._last_spot_return = spot_return
        self._last_kalshi_change = kalshi_prob_change
        if len(self._resp_spot_returns) < 10:
            return
        s = np.array(self._resp_spot_returns)
        k = np.array(self._resp_kalshi_changes)
        var_s = np.var(s)
        if var_s < 1e-10:
            return
        cov = np.cov(s, k)[0, 1]
        self._response_beta = float(cov / var_s)

    @property
    def response_beta(self) -> float:
        """Estimated Kalshi probability change per unit log spot return."""
        return self._response_beta

    @property
    def response_gap(self) -> float:
        """
        Positive means Kalshi underreacted (potential entry signal).
        Returns 0.0 before enough observations.
        """
        if len(self._resp_spot_returns) < 10:
            return 0.0
        expected = self._response_beta * self._last_spot_return
        return expected - self._last_kalshi_change

    @property
    def lag_confidence(self) -> float:
        if len(self._binance_returns) < 10:
            return 0.0
        b = np.array(self._binance_returns)
        k = np.array(self._kalshi_changes)
        if np.std(b) < 1e-10 or np.std(k) < 1e-10:
            return 0.0
        corr = np.corrcoef(b, k)[0, 1]
        corr = float(corr) if not np.isnan(corr) else 0.0
        # High positive correlation = Kalshi is following Binance = lag exists
        return float(np.clip(corr, 0, 1))

    @property
    def lag_signal(self) -> float:
        """
        Positive = Binance recently moved up but Kalshi hasn't caught up (buy YES).
        Negative = Binance recently moved down but Kalshi hasn't caught up (buy NO).
        """
        if len(self._binance_returns) < 5:
            return 0.0
        recent_binance = float(np.mean(list(self._binance_returns)[-5:]))
        recent_kalshi = float(np.mean(list(self._kalshi_changes)[-5:]))
        # Divergence: Binance moved but Kalshi didn't
        divergence = recent_binance - recent_kalshi * 1000  # scale mismatch
        return float(np.clip(divergence * 100, -1, 1))
