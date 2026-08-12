"""
synthetic_spot.py — Multi-venue synthetic spot estimator with freshness.

Tracks venue prices with timestamps. spot_mid and dislocation use only
venues updated within the last 15 seconds (matches 15-min binary horizon).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

log = logging.getLogger("kalshi_bot.synthetic_spot")
FRESHNESS_SECS = 15.0

# source_count -> confidence (Coinbase/OKX/Kraken/Gemini; Binance optional)
CONFIDENCE_MAP = {
    0: 0.0,
    1: 0.3,
    2: 0.6,
    3: 0.85,
    4: 1.0,
}


class SyntheticSpotEstimator:
    """
    Robust composite spot price across venues with freshness tracking.
    Uses median of fresh venues. Allows 1 venue if fresh (< 15s); else requires >= 2.
    """

    def __init__(self, symbol: str = ""):
        # source -> (price, ts)
        self._venues: Dict[str, tuple[float, float]] = {}
        self._symbol = symbol
        self._first_estimate_logged = False

    def update(self, source: str, price: float, ts: Optional[float] = None) -> None:
        """Add or refresh a venue price. ts defaults to time.time()."""
        if ts is None:
            ts = time.time()
        if price > 0:
            log.debug(f"[{self._symbol}] spot update: source={source} price={price}")
        self._venues[source] = (price, ts)
        fresh = self._fresh_venues()
        fresh_count = len(fresh)
        estimate = self.spot_mid
        log.debug(f"[{self._symbol}] SyntheticSpot venues={fresh_count} estimate={estimate}")
        # Log when first valid estimate produced
        if not self._first_estimate_logged and estimate is not None and estimate > 0:
            log.info(f"[{self._symbol}] Synthetic spot initialized: {estimate}")
            self._first_estimate_logged = True

    def _fresh_venues(self) -> Dict[str, tuple[float, float]]:
        """Venues updated within the last FRESHNESS_SECS."""
        now = time.time()
        cutoff = now - FRESHNESS_SECS
        return {k: v for k, v in self._venues.items() if v[1] >= cutoff and v[0] > 0}

    @property
    def spot_mid(self) -> Optional[float]:
        """Median of venue prices updated within the last 15 seconds.
        Allows 1 venue if fresh (< 15s old); otherwise requires >= 2 venues."""
        fresh = self._fresh_venues()
        vals = []
        for venue, (price, _) in fresh.items():
            if price is None or price <= 0:
                log.warning(f"[{self._symbol}] Venue {venue} fresh but price=0, ignoring")
                continue
            vals.append(price)
        if len(vals) < 1:
            return None
        vals.sort()
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    @property
    def dislocation(self) -> float:
        """(max - min) / min across fresh venues. Returns 0.0 if < 2 venues."""
        fresh = self._fresh_venues()
        vals = [v[0] for v in fresh.values()]
        if len(vals) < 2:
            return 0.0
        mn, mx = min(vals), max(vals)
        if mn <= 0:
            return 0.0
        return (mx - mn) / mn

    @property
    def source_count(self) -> int:
        """Number of venues with a fresh price (updated within last 15 seconds)."""
        return len(self._fresh_venues())

    @property
    def confidence(self) -> float:
        """0.0 to 1.0 based on source_count:
        1 venue = 0.3, 2 = 0.6, 3 = 0.85, 4 = 1.0, 0 = 0.0"""
        n = self.source_count
        return CONFIDENCE_MAP.get(n, 1.0 if n >= 4 else 0.0)

    @property
    def staleness(self) -> float:
        """Seconds since the oldest fresh venue was last updated.
        Returns 0.0 if no venues are fresh."""
        fresh = self._fresh_venues()
        if not fresh:
            return 0.0
        now = time.time()
        oldest_ts = min(v[1] for v in fresh.values())
        return now - oldest_ts

    @property
    def lead_source(self) -> Optional[str]:
        """The venue whose price changed most recently.
        Returns None if no venues are fresh."""
        fresh = self._fresh_venues()
        if not fresh:
            return None
        return max(fresh.keys(), key=lambda k: fresh[k][1])

    def venue_mids(self) -> Dict[str, float]:
        """All venues with a fresh price and their current mid."""
        fresh = self._fresh_venues()
        return {k: v[0] for k, v in fresh.items()}

    def venue_staleness(self) -> Dict[str, float]:
        """All venues and their age in seconds."""
        now = time.time()
        return {k: now - v[1] for k, v in self._venues.items() if v[0] > 0}
