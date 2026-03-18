"""
signal_engine.py — Multi-feed signal engine.

Reused from the Polymarket bot with three key fixes:
  1. HAWKES_DECAY default lowered from 3.0 → 0.046 (half-life 15s, not 0.23s).
     At the old value, 99% of signal decayed in 1.5s — before the next poll.
  2. OFI_WINDOW_SECS widened to 120s (was 60s) to match 15-min markets.
  3. Per-asset state: each AssetSignalEngine is independent.

ArXiv references:
  [1] arXiv:2408.03594  — Hawkes OFI decay
  [2] arXiv:2507.16701  — OBI importance
  [3] arXiv:2507.22712  — Order book filtration
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Optional, Tuple

import numpy as np

from .config import cfg

log = logging.getLogger("kalshi_bot.signal_engine")


# ═══════════════════════════════════════════════════════════════════════════════
# Synthetic spot (multi-venue composite)
# ═══════════════════════════════════════════════════════════════════════════════

class SyntheticSpot:
    """
    Robust composite spot price across venues.
    Uses median to resist single-venue outliers.
    """

    def __init__(self):
        self._prices: dict = {}  # source → price

    def update(self, source: str, price: float) -> None:
        self._prices[source] = price

    @property
    def mid(self) -> float | None:
        vals = [v for v in self._prices.values() if v > 0]
        if not vals:
            return None
        vals.sort()
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    @property
    def dislocation(self) -> float:
        vals = [v for v in self._prices.values() if v > 0]
        if len(vals) < 2:
            return 0.0
        return (max(vals) - min(vals)) / min(vals)

    @property
    def venue_count(self) -> int:
        return sum(1 for v in self._prices.values() if v > 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Bayesian fusion
# ═══════════════════════════════════════════════════════════════════════════════

class BayesUpdater:
    def __init__(self, prior: float = 0.5, confidence: float = 2.0):
        self.alpha  = prior * confidence
        self.beta_  = (1.0 - prior) * confidence
        self._last_decay: float = time.time()

    @property
    def posterior(self) -> float:
        return self.alpha / (self.alpha + self.beta_)

    def update(self, likelihood: float, weight: float = 1.0) -> float:
        self._decay()
        if likelihood > 0.5:
            self.alpha  += weight * (likelihood - 0.5) * 2
        else:
            self.beta_  += weight * (0.5 - likelihood) * 2
        return self.posterior

    def _decay(self, half_life_secs: float = 3600.0) -> None:
        lam = math.log(2) / half_life_secs
        now = time.time()
        elapsed = now - self._last_decay
        decay = math.exp(-lam * elapsed)
        self.alpha = 1.0 + (self.alpha - 1.0) * decay
        self.beta_  = 1.0 + (self.beta_  - 1.0) * decay
        self._last_decay = now

    @property
    def variance(self) -> float:
        a, b = self.alpha, self.beta_
        n = a + b
        return (a * b) / (n * n * (n + 1))


class BayesFusion:
    def __init__(self):
        self._sources: dict = {}

    def add(self, name: str, prior: float = 0.5, confidence: float = 2.0, weight: float = 1.0) -> None:
        self._sources[name] = (BayesUpdater(prior, confidence), weight)

    def push(self, name: str, likelihood: float) -> float:
        upd, _ = self._sources[name]
        return upd.update(likelihood)

    def fuse(self) -> Tuple[float, float]:
        if not self._sources:
            return 0.5, 1.0
        wsum = total_w = 0.0
        for name, (upd, base_w) in self._sources.items():
            var = upd.variance + 1e-9
            w = base_w / var
            wsum   += upd.posterior * w
            total_w += w
        p = wsum / total_w if total_w > 0 else 0.5
        unc = 1.0 / math.sqrt(total_w) if total_w > 0 else 1.0
        return float(np.clip(p, 0.01, 0.99)), unc


# ═══════════════════════════════════════════════════════════════════════════════
# Logit-space price tracker  (arXiv:2510.15205)
# ═══════════════════════════════════════════════════════════════════════════════

def logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class LogitPriceTracker:
    def __init__(self, alpha: float = None):
        self._alpha = alpha if alpha is not None else cfg.LOGIT_EWM_ALPHA
        self._logit_x: Optional[float] = None
        self._history: deque = deque(maxlen=50)
        self._last_p: Optional[float] = None

    def update(self, p_market: float) -> float:
        x = logit(p_market)
        if self._logit_x is None:
            self._logit_x = x
        else:
            self._logit_x = self._alpha * x + (1 - self._alpha) * self._logit_x
        if self._last_p is not None:
            self._history.append(x - logit(self._last_p))
        self._last_p = p_market
        return self.smooth_p

    @property
    def smooth_p(self) -> float:
        return sigmoid(self._logit_x) if self._logit_x is not None else 0.5

    @property
    def belief_vol(self) -> float:
        if len(self._history) < 5:
            return 0.05
        return float(np.std(list(self._history)))

    @property
    def adjusted_min_edge(self) -> float:
        """Widen required edge when market price is noisy."""
        return cfg.MIN_EDGE_PCT + 0.5 * self.belief_vol


# ═══════════════════════════════════════════════════════════════════════════════
# Per-asset signal engine  (Coinbase primary, OKX secondary, Binance fallback)
# ═══════════════════════════════════════════════════════════════════════════════

VOLUME_WEIGHT_WINDOW_SECS = 30


class AssetSignalEngine:
    """
    Maintains rolling signal state for one asset (BTC / ETH / SOL / XRP).
    Feed data via update_trade_coinbase(), update_trade_okx(), update_trade_binance(),
    update_book_coinbase(), update_book_okx(), update_book_binance().
    Call get_bias_score() to get (bias ∈ [-1, 1], uncertainty).
    """

    def __init__(self, symbol: str, window_secs: int = None):
        self.symbol = symbol.lower()
        self.window = window_secs if window_secs is not None else cfg.OFI_WINDOW_SECS

        # Trade history
        self.trades: list = []
        self.prices: deque = deque(maxlen=500)
        # Store (price, timestamp) pairs for per-return dt annualization
        self._price_times: deque = deque(maxlen=500)

        # VWAP
        self.vwap_n: float = 0.0
        self.vwap_d: float = 0.0

        # Orderbooks (Coinbase — primary, US-available)
        self.book_bids_cb: dict = {}
        self.book_asks_cb: dict = {}

        # Orderbooks (Binance — fallback, blocked for US IPs)
        self.book_bids: dict = {}
        self.book_asks: dict = {}

        # Orderbooks (OKX)
        self.book_bids_okx: dict = {}
        self.book_asks_okx: dict = {}

        # Hawkes state — separate per exchange
        self._h_buy:        float = 0.0
        self._h_sell:       float = 0.0
        self._h_last:       float = time.time()
        self._h_buy_cb:     float = 0.0
        self._h_sell_cb:    float = 0.0
        self._h_last_cb:    float = time.time()
        self._h_buy_okx:    float = 0.0
        self._h_sell_okx:   float = 0.0
        self._h_last_okx:   float = time.time()

        # 30s rolling volume for OBI/OFI merge weight
        self._vol_entries: list = []

        # EMA
        self.ema5:  Optional[float] = None
        self.ema20: Optional[float] = None
        self._k5  = 2 / 6
        self._k20 = 2 / 21

        # Heikin-Ashi candles
        self.ohlcv: deque = deque(maxlen=100)
        self.ha:    deque = deque(maxlen=100)
        self._candle: dict = {"o": None, "h": 0, "l": float("inf"), "v": 0.0}
        self._candle_min: int = -1

        # Volume buy/sell for MRO
        self._vbuy:  deque = deque(maxlen=120)
        self._vsell: deque = deque(maxlen=120)

        # Trade count for first-trade logging
        self._trade_count_cb: int = 0

        # Bayesian fusion
        self.fusion = BayesFusion()
        self.fusion.add("obi",        weight=0.25)
        self.fusion.add("ofi_h",      weight=0.25)
        self.fusion.add("vwap",       weight=0.15)
        self.fusion.add("microprice", weight=0.15)
        self.fusion.add("ema",        weight=0.04)   # Demoted from 0.10; weak secondary
        self.fusion.add("ha",         weight=0.03)   # Demoted from 0.07; weak secondary
        self.fusion.add("mro",        weight=0.03)
        self.fusion.add("autocorr",   weight=0.12)

    # ─── Hawkes helpers ───────────────────────────────────────────────────────

    def _decay_binance(self) -> None:
        now = time.time()
        dt = now - self._h_last
        d = math.exp(-cfg.HAWKES_DECAY * dt)
        self._h_buy  *= d
        self._h_sell *= d
        self._h_last  = now

    def _decay_okx(self) -> None:
        now = time.time()
        dt = now - self._h_last_okx
        d = math.exp(-cfg.HAWKES_DECAY * dt)
        self._h_buy_okx  *= d
        self._h_sell_okx *= d
        self._h_last_okx  = now

    def _decay_coinbase(self) -> None:
        now = time.time()
        dt = now - self._h_last_cb
        d = math.exp(-cfg.HAWKES_DECAY * dt)
        self._h_buy_cb  *= d
        self._h_sell_cb *= d
        self._h_last_cb  = now

    def _prune_vol(self) -> None:
        cutoff = time.time() - VOLUME_WEIGHT_WINDOW_SECS
        self._vol_entries = [e for e in self._vol_entries if e["t"] >= cutoff]

    # ─── Trade ingestion ──────────────────────────────────────────────────────

    def update_trade_binance(self, price: float, qty: float, is_buyer_maker: bool) -> None:
        is_buy = not is_buyer_maker
        self._decay_binance()
        if is_buy:
            self._h_buy  += cfg.HAWKES_ALPHA * qty
            self._vbuy.append(qty); self._vsell.append(0.0)
        else:
            self._h_sell += cfg.HAWKES_ALPHA * qty
            self._vbuy.append(0.0); self._vsell.append(qty)
        self._ingest_trade(price, qty, is_buy, "binance")

    def update_trade_okx(self, price: float, qty: float, side: str) -> None:
        is_buy = side.lower() == "buy"
        self._decay_okx()
        if is_buy:
            self._h_buy_okx  += cfg.HAWKES_ALPHA * qty
            self._vbuy.append(qty); self._vsell.append(0.0)
        else:
            self._h_sell_okx += cfg.HAWKES_ALPHA * qty
            self._vbuy.append(0.0); self._vsell.append(qty)
        self._ingest_trade(price, qty, is_buy, "okx")

    def update_trade_coinbase(self, price: float, qty: float, side: str) -> None:
        """Coinbase: side is BUY or SELL."""
        self._trade_count_cb += 1
        if self._trade_count_cb == 1:
            log.info(f"[{self.symbol.upper()}] First Coinbase trade: price={price}")
        is_buy = side.upper() == "BUY"
        self._decay_coinbase()
        if is_buy:
            self._h_buy_cb  += cfg.HAWKES_ALPHA * qty
            self._vbuy.append(qty); self._vsell.append(0.0)
        else:
            self._h_sell_cb += cfg.HAWKES_ALPHA * qty
            self._vbuy.append(0.0); self._vsell.append(qty)
        self._ingest_trade(price, qty, is_buy, "coinbase")

    def _ingest_trade(self, price: float, qty: float, is_buy: bool, src: str) -> None:
        now = time.time()
        self._vol_entries.append({"t": now, "qty": qty, "src": src})
        self.vwap_n += price * qty
        self.vwap_d += qty
        self.prices.append(price)
        self._price_times.append((price, now))
        if self.ema5 is None:
            self.ema5 = self.ema20 = price
        else:
            self.ema5  = price * self._k5  + self.ema5  * (1 - self._k5)
            self.ema20 = price * self._k20 + self.ema20 * (1 - self._k20)
        self.trades.append({"p": price, "q": qty, "buy": is_buy, "t": now})
        cutoff = now - self.window
        self.trades = [t for t in self.trades if t["t"] >= cutoff]
        self._tick_candle(price, qty)

    # ─── Book ingestion ───────────────────────────────────────────────────────

    def update_book_binance(self, bids: list, asks: list) -> None:
        self.book_bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.book_asks = {float(p): float(q) for p, q in asks if float(q) > 0}

    def update_book_okx(self, bids: list, asks: list) -> None:
        self.book_bids_okx = {float(r[0]): float(r[1]) for r in bids if len(r) >= 2 and float(r[1]) > 0}
        self.book_asks_okx = {float(r[0]): float(r[1]) for r in asks if len(r) >= 2 and float(r[1]) > 0}

    # ─── Candle / Heikin-Ashi ─────────────────────────────────────────────────

    def _tick_candle(self, price: float, volume: float) -> None:
        import datetime as dt_mod
        now_min = dt_mod.datetime.now(dt_mod.timezone.utc).minute
        if now_min != self._candle_min and self._candle["o"] is not None:
            c = self._candle
            self.ohlcv.append({"o": c["o"], "h": c["h"], "l": c["l"], "c": price, "v": c["v"]})
            self._build_ha()
            self._candle = {"o": price, "h": price, "l": price, "v": 0.0}
        elif self._candle["o"] is None:
            self._candle = {"o": price, "h": price, "l": price, "v": 0.0}
        self._candle_min = now_min
        self._candle["h"] = max(self._candle["h"], price)
        self._candle["l"] = min(self._candle["l"], price)
        self._candle["v"] += volume

    def _build_ha(self) -> None:
        if not self.ohlcv:
            return
        c   = self.ohlcv[-1]
        cl  = c["c"]
        ha_c = (c["o"] + c["h"] + c["l"] + cl) / 4
        ha_o = ((self.ha[-1]["o"] + self.ha[-1]["c"]) / 2 if self.ha else (c["o"] + cl) / 2)
        ha_h = max(c["h"], ha_o, ha_c)
        ha_l = min(c["l"], ha_o, ha_c)
        self.ha.append({"o": ha_o, "h": ha_h, "l": ha_l, "c": ha_c})

    # ─── Individual signals ───────────────────────────────────────────────────

    def _obi(self) -> float:
        self._prune_vol()
        vol_cb   = sum(e["qty"] for e in self._vol_entries if e["src"] == "coinbase")
        vol_okx  = sum(e["qty"] for e in self._vol_entries if e["src"] == "okx")
        vol_b    = sum(e["qty"] for e in self._vol_entries if e["src"] == "binance")
        denom    = vol_cb + vol_okx + vol_b + 1e-9
        obi_cb   = _obi_raw(sum(self.book_bids_cb.values()), sum(self.book_asks_cb.values()))
        obi_okx  = _obi_raw(sum(self.book_bids_okx.values()), sum(self.book_asks_okx.values()))
        obi_b    = _obi_raw(sum(self.book_bids.values()), sum(self.book_asks.values()))
        return (obi_cb * vol_cb + obi_okx * vol_okx + obi_b * vol_b) / denom

    def _ofi_hawkes(self) -> float:
        self._decay_coinbase(); self._decay_binance(); self._decay_okx(); self._prune_vol()
        vol_cb   = sum(e["qty"] for e in self._vol_entries if e["src"] == "coinbase")
        vol_okx  = sum(e["qty"] for e in self._vol_entries if e["src"] == "okx")
        vol_b    = sum(e["qty"] for e in self._vol_entries if e["src"] == "binance")
        denom    = vol_cb + vol_okx + vol_b + 1e-9
        ofi_cb   = (self._h_buy_cb - self._h_sell_cb) / (self._h_buy_cb + self._h_sell_cb + 1e-9)
        ofi_okx  = (self._h_buy_okx - self._h_sell_okx) / (self._h_buy_okx + self._h_sell_okx + 1e-9)
        ofi_b    = (self._h_buy - self._h_sell) / (self._h_buy + self._h_sell + 1e-9)
        return (ofi_cb * vol_cb + ofi_okx * vol_okx + ofi_b * vol_b) / denom

    def _vwap_sig(self) -> float:
        if self.vwap_d == 0 or not self.prices:
            return 0.0
        return 1.0 if self.prices[-1] > self.vwap_n / self.vwap_d else -1.0

    def _ema_sig(self) -> float:
        if self.ema5 is None or self.ema20 is None:
            return 0.0
        return 1.0 if self.ema5 > self.ema20 else -1.0

    def _ha_sig(self) -> float:
        if len(self.ha) < 3:
            return 0.0
        last3 = list(self.ha)[-3:]
        if all(h["c"] > h["o"] for h in last3): return  1.0
        if all(h["c"] < h["o"] for h in last3): return -1.0
        return 0.0

    def _mro_sig(self) -> float:
        if len(self.prices) < 6 or len(self._vbuy) < 10:
            return 0.0
        vb = list(self._vbuy); vs = list(self._vsell)
        pv_now = self.prices[-1] * (sum(vb[-5:])   + sum(vs[-5:]))
        pv_old = self.prices[-6] * (sum(vb[-10:-5]) + sum(vs[-10:-5]))
        mro = 100 * (pv_now - pv_old) / (pv_old + 1e-9)
        if mro >  70: return  1.0
        if mro < -70: return -1.0
        return 0.0

    def _microprice_sig(self) -> float:
        """Microprice deviation from mid. Positive = bullish."""
        # Use Coinbase (primary) if available, else OKX, else Binance
        bids, asks = self.book_bids_cb, self.book_asks_cb
        if not bids or not asks:
            bids, asks = self.book_bids_okx, self.book_asks_okx
        if not bids or not asks:
            bids, asks = self.book_bids, self.book_asks
        if not bids or not asks:
            return 0.0
        best_bid = max(bids.keys())
        best_ask = min(asks.keys())
        bid_sz = bids[best_bid]
        ask_sz = asks[best_ask]
        mid = (best_bid + best_ask) / 2
        mp = microprice(best_bid, bid_sz, best_ask, ask_sz)
        deviation = mp - mid
        # Normalize: ~0.01% of mid as full scale; positive deviation = bullish
        dev_normalized = float(np.clip(10000 * deviation / (mid + 1e-9), -1, 1))
        return dev_normalized

    def _trade_sign_autocorr(self) -> float:
        """
        Measures clustering of aggressive buys/sells.
        Returns +1 if recent buys cluster, -1 if sells cluster, 0 if random.
        """
        if len(self.trades) < 20:
            return 0.0
        signs = [1.0 if t["buy"] else -1.0 for t in self.trades[-40:]]
        s = np.array(signs)
        if len(s) < 2:
            return 0.0
        corr = np.corrcoef(s[:-1], s[1:])[0, 1]
        autocorr = float(corr) if not np.isnan(corr) else 0.0
        # Positive autocorr = momentum/clustering; negative = mean-reversion
        # We want directional bias, so multiply by last sign
        direction = signs[-1]
        return float(np.clip(autocorr * direction, -1, 1))

    # ─── Aggregated outputs ───────────────────────────────────────────────────

    def get_bias_score(self) -> Tuple[float, float]:
        """Returns (bias ∈ [-1, 1], uncertainty). Feeds Bayesian fusion."""
        for name, fn in [("obi",        self._obi),
                         ("ofi_h",      self._ofi_hawkes),
                         ("vwap",       self._vwap_sig),
                         ("microprice", self._microprice_sig),
                         ("ema",        self._ema_sig),
                         ("ha",         self._ha_sig),
                         ("mro",        self._mro_sig),
                         ("autocorr",   self._trade_sign_autocorr)]:
            s = fn()
            self.fusion.push(name, (s + 1) / 2)
        fused_p, unc = self.fusion.fuse()
        bias = float(np.clip((fused_p - 0.5) * 2, -1, 1))
        return bias, unc

    def conviction(self) -> int:
        """Number of primary signals agreeing (0–5). Trade when ≥ MIN_CONVICTION."""
        primary = [
            self._obi()        > 0.1,
            self._ofi_hawkes() > 0.05,
            self._vwap_sig()   > 0,
            self._ema_sig()    > 0,
            self._ha_sig()     > 0,
        ]
        bull = sum(primary)
        return max(bull, 5 - bull)

    def get_realized_vol(self) -> float:
        """
        Annualized realized volatility from irregular tick-level log returns.

        Uses actual elapsed time between observations:
            var_rate ≈ mean(r_t^2 / dt_t)
            annualized_vol = sqrt(var_rate * SECONDS_PER_YEAR)
        """
        SECONDS_PER_YEAR = 365 * 24 * 3600
        if len(self._price_times) < 20:
            return 0.3

        pts = list(self._price_times)[-120:]
        prices = np.array([p for p, _ in pts], dtype=float)
        times = np.array([t for _, t in pts], dtype=float)

        if len(prices) < 2:
            return 0.3

        log_prices = np.log(prices)
        rets = np.diff(log_prices)
        dts = np.diff(times)

        # Minimum dt floor of 1s to avoid bursts (10–50ms ticks) inflating annualized vol
        valid = dts >= 1.0
        if valid.sum() < 5:
            return 0.3

        rets = rets[valid]
        dts = dts[valid]

        var_rate = float(np.mean((rets ** 2) / dts))
        ann_vol = math.sqrt(max(var_rate * SECONDS_PER_YEAR, 0.0))

        if not np.isfinite(ann_vol):
            return 0.3

        log.debug(f"[{self.symbol.upper()}] realized_vol={ann_vol:.4f}")
        return float(ann_vol)

    def get_components(self) -> dict:
        return {
            "obi":        round(self._obi(), 4),
            "ofi_hawkes": round(self._ofi_hawkes(), 4),
            "vwap":       round(self._vwap_sig(), 4),
            "microprice": round(self._microprice_sig(), 4),
            "ema":        round(self._ema_sig(), 4),
            "ha":         round(self._ha_sig(), 4),
            "mro":        round(self._mro_sig(), 4),
            "autocorr":   round(self._trade_sign_autocorr(), 4),
        }

    def is_ready(self) -> bool:
        """Enough data to produce reliable signals?"""
        return len(self.prices) >= 20 and len(self.trades) >= 5


# ─── Helpers ─────────────────────────────────────────────────────────────────

def microprice(bid_price: float, bid_size: float,
               ask_price: float, ask_size: float) -> float:
    total = bid_size + ask_size
    return (bid_price * ask_size + ask_price * bid_size) / total \
           if total > 0 else (bid_price + ask_price) / 2


def _obi_raw(bid_vol: float, ask_vol: float) -> float:
    total = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0


def vol_position_scalar(realized_vol: float) -> float:
    if realized_vol > cfg.VOL_HI:
        return 0.0
    elif realized_vol > cfg.VOL_MID:
        return 0.5
    return 1.0


def kelly_binary(p_hat: float, q_market: float) -> float:
    """Kelly for binary prediction markets (arXiv:2412.14144)."""
    if not (0 < q_market < 1):
        return 0.0
    edge = p_hat - q_market
    if edge <= 0:
        return 0.0
    full_kelly = edge / (1.0 - q_market)
    return min(full_kelly * cfg.KELLY_FRACTION, cfg.MAX_POS_PCT)
