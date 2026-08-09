"""
asset_engine.py — Decision engine for one asset (BTC / ETH / SOL / XRP).

Each AssetEngine:
  · Owns one AssetSignalEngine (Hawkes, OBI, Bayesian fusion)
  · Owns one LogitPriceTracker (smooth market price)
  · Maintains one OpenPosition per 15-min window
  · Calls make_decision() on every Kalshi price update
  · Resolves positions at window rollover using exchange price

One AssetEngine is instantiated per enabled asset; they all run concurrently
inside the same asyncio event loop (see kalshi_bot.py orchestrator).
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional, Tuple

import numpy as np

from .config import cfg, AssetSpec, BLACKOUT_WINDOWS, BLACKOUT_HALF_WIDTH_SECS

if TYPE_CHECKING:
    from .recorder import EventRecorder
from .kalshi_client import KalshiClient
from .lag_tracker import KalshiLagTracker
from .prob_model import SECONDS_PER_YEAR
from .features.threshold_features import compute_threshold_features
from .models.micro_alpha_model import MicroAlphaModel
from .data.synthetic_spot import SyntheticSpotEstimator
from .signal_engine import (
    AssetSignalEngine, LogitPriceTracker,
    vol_position_scalar, kelly_binary,
    entry_variance_scalar, belief_vol_scalar,
    sigmoid, logit,
)
from .sim_state import SimState, OpenPosition
from .strategy.strategy_router import StrategyRouter
from .strategy.lag_arb import LagArbStrategy
from .strategy.close_boundary import CloseBoundaryStrategy
from .strategy.dislocation_reversion import DislocationReversionStrategy
from .recorder import rotate_log_if_needed, DECISIONS_MAX_LINES, DECISIONS_KEEP_LINES

log = logging.getLogger("asset_engine")
# Absolute path so decisions write to project logs/ regardless of cwd
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DECISION_LOG = str(_PROJECT_ROOT / "logs" / "kalshi_decisions.jsonl")


def _tf_log_fields(tf) -> dict:
    """Extract threshold feature fields for decision logging."""
    return {
        "z_threshold": tf.z_threshold,
        "p_base": tf.p_base,
        "mispricing_base": tf.mispricing_base,
        "confidence_weighted_mispricing": tf.confidence_weighted_mispricing,
        "spot_confidence": tf.spot_confidence,
        "lag_confidence": tf.lag_confidence,
    }


def _floor_strike_from_market(market: Optional[dict]) -> Optional[float]:
    """
    Kalshi 15m crypto markets expose the official window-open target as floor_strike.
    Prefer this over synthetic spot — mid-window joins otherwise invent a wrong threshold.
    """
    if not market:
        return None
    raw = market.get("floor_strike")
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _is_in_blackout(now: Optional[datetime] = None) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    for wd, h, m in BLACKOUT_WINDOWS:
        if now.weekday() != wd:
            continue
        ann = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((now - ann).total_seconds()) < BLACKOUT_HALF_WIDTH_SECS:
            return True
    return False


class AssetEngine:
    """
    Manages the full trade lifecycle for one crypto asset on Kalshi 15-min markets.
    Thread-safe in the sense that all state mutations happen on the asyncio event loop.
    """

    def __init__(
        self,
        spec: AssetSpec,
        kalshi: KalshiClient,
        sim: SimState,
        recorder: Optional["EventRecorder"] = None,
        on_window_close: Optional[Callable[[str, str, float, float, str, int, float], None]] = None,
    ):
        self.spec   = spec
        self.kalshi = kalshi
        self.sim    = sim
        self.recorder = recorder
        self._on_window_close = on_window_close

        self.signal       = AssetSignalEngine(spec.symbol)
        self.tracker      = LogitPriceTracker()
        self.lag_tracker  = KalshiLagTracker()
        self.synthetic_spot = SyntheticSpotEstimator(spec.symbol)
        self.alpha_model   = MicroAlphaModel(asset=spec.symbol)
        self.router        = StrategyRouter([
            LagArbStrategy(),
            CloseBoundaryStrategy(),
            DislocationReversionStrategy(),
        ])

        # Reference to all engines (set by orchestrator after construction)
        self._all_engines: Dict[str, AssetEngine] = {}

        # Market state
        self._market: Optional[dict]  = None
        self._ticker: str             = ""
        self._window_id: int          = -1
        self._window_start: Optional[float] = None
        self._close_time_utc: Optional[str]  = None  # Kalshi ISO UTC e.g. "2026-03-18T12:30:00Z"
        self._price_to_beat: Optional[float] = None
        self._price_to_beat_source: str = ""  # "floor_strike" | "window_open_spot" | ""
        self._ptb_floor_refresh_attempted: int = -1  # window_id we already re-fetched for strike

        # Open position for this asset
        self._open_pos: Optional[OpenPosition] = None

        # Staleness tracking for Kalshi price feed
        self._last_price_ts: float = 0.0
        self._last_price_val: Optional[float] = None
        self._price_to_beat_30s_warned: int = -1  # window_id we warned for
        self._kalshi_prob_history: deque = deque(maxlen=100)
        self._spot_history: deque = deque(maxlen=100)

        # Diagnostics
        self._total_ticks: int = 0
        self._wait_counts: dict = {}
        self._wait_last_log: float = time.time()
        self._p_base_min: float = cfg.P_BASE_MIN
        self._p_base_max: float = cfg.P_BASE_MAX

        # Cross-asset consistency: last p_base per window for divergence monitoring
        self._last_p_base: Optional[float] = None
        self._last_p_base_ts: float = 0.0
        self._last_p_base_window_id: int = -1

    # ─── Strategy snapshot helpers ────────────────────────────────────────────

    def _last_price_age(self) -> float:
        """Seconds since last Kalshi price update."""
        if self._last_price_ts <= 0:
            return 999.0
        return time.time() - self._last_price_ts

    def _last_kalshi_spread(self) -> float:
        """Kalshi orderbook spread in probability units."""
        if not self._ticker:
            return 0.0
        try:
            book = self.kalshi.get_orderbook(self._ticker)
            yes_bids = book.get("yes", [])
            no_bids = book.get("no", [])
            best_yes_bid = max((int(r[0]) for r in yes_bids if len(r) >= 2), default=0)
            best_no_bid = max((int(r[0]) for r in no_bids if len(r) >= 2), default=0)
            best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 100
            if best_yes_bid > 0 and best_yes_ask < 100:
                spread_cents = best_yes_ask - best_yes_bid
                return spread_cents / 100.0
        except Exception:
            pass
        return 0.0

    def _kalshi_prob_change_1s(self) -> float:
        """Kalshi prob now minus ~1s ago."""
        now = time.time()
        if not self._kalshi_prob_history or self._last_price_val is None:
            return 0.0
        cutoff = now - 1.5
        old = [(t, p) for t, p in self._kalshi_prob_history if t <= cutoff]
        if not old:
            return 0.0
        _, p_old = max(old, key=lambda x: x[0])
        return self._last_price_val - p_old

    def _spot_return_1s(self) -> float:
        """Log return of synthetic_mid over ~1s."""
        now = time.time()
        sm = self.synthetic_spot.spot_mid
        if sm is None or sm <= 0:
            return 0.0
        if not self._spot_history:
            return 0.0
        cutoff = now - 1.5
        old = [(t, m) for t, m in self._spot_history if t <= cutoff and m and m > 0]
        if not old:
            return 0.0
        _, m_old = max(old, key=lambda x: x[0])
        if m_old <= 0:
            return 0.0
        return math.log(sm / m_old)

    # ─── Window management ────────────────────────────────────────────────────

    def _get_window_id(self) -> int:
        """Unix timestamp of current 15-min window boundary."""
        now_s = int(time.time())
        return now_s - (now_s % cfg.WINDOW_SECS)

    def on_window_advance(self) -> None:
        """Called when the 15-min window rolls over."""
        wid = self._get_window_id()
        if wid == self._window_id:
            return
        # Close previous window: resolve position and log summary
        closed_wid = self._window_id
        exit_spot = 0.0
        trade_count = 0
        window_pnl = 0.0
        if self._open_pos is not None:
            exit_spot, window_pnl, trade_count = self._resolve_position()
        else:
            # No position: still need exit_spot for summary
            if self.signal.prices:
                exit_spot = float(self.signal.prices[-1])
            elif self.synthetic_spot.spot_mid is not None:
                exit_spot = self.synthetic_spot.spot_mid
        price_to_beat = (self._price_to_beat or 0.0) if closed_wid > 0 else 0.0
        if closed_wid > 0 and self._on_window_close and exit_spot > 0:
            outcome = "YES" if exit_spot > price_to_beat else "NO"
            self._on_window_close(
                _fmt_window_id(closed_wid), self.spec.symbol,
                price_to_beat, exit_spot, outcome, trade_count, window_pnl,
            )
        self._window_id    = wid
        self._window_start = float(wid)

        # Refresh market from Kalshi
        m = self.kalshi.find_active_market(self.spec.series_ticker)
        if m:
            self._market = m
            self._ticker = m.get("ticker", "")
            self._close_time_utc = m.get("close_time")
            log.info(f"[{self.spec.symbol}] New window {_fmt_window(wid)} | ticker={self._ticker}")
        else:
            log.warning(f"[{self.spec.symbol}] No active market for {self.spec.series_ticker}")
            self._market = None
            self._ticker = ""
            self._close_time_utc = None

        # Official Kalshi floor_strike first; else capture synthetic spot only at window open
        self._price_to_beat = None
        self._price_to_beat_source = ""
        floor = _floor_strike_from_market(self._market)
        if floor is not None:
            self._price_to_beat = floor
            self._price_to_beat_source = "floor_strike"
        else:
            sm = self.synthetic_spot.spot_mid
            if sm is not None and sm > 0:
                self._price_to_beat = float(sm)
                self._price_to_beat_source = "window_open_spot"
            elif self.signal.prices:
                p = float(self.signal.prices[-1])
                if p > 0:
                    self._price_to_beat = p
                    self._price_to_beat_source = "window_open_spot"

        ptb = f"{self._price_to_beat:.4f}" if self._price_to_beat else "N/A"
        log.info(
            f"[{self.spec.symbol}] price_to_beat={ptb} "
            f"source={self._price_to_beat_source or 'pending'}"
        )

    # ─── Position resolution ──────────────────────────────────────────────────

    def _resolve_position(self) -> Tuple[float, float, int]:
        """
        Kalshi resolves YES if asset price at window end > price at window start.
        Resolution: YES wins → YES payout $1/contract; NO wins → NO payout $1/contract.
        Returns (exit_spot, pnl, trade_count).
        """
        pos = self._open_pos
        if pos is None:
            return (0.0, 0.0, 0)

        # Use same source as price_to_beat: synthetic_spot first, else signal.prices
        exit_spot = None
        if self.synthetic_spot.spot_mid is not None and self.synthetic_spot.spot_mid > 0:
            exit_spot = float(self.synthetic_spot.spot_mid)
        elif self.signal.prices:
            exit_spot = float(self.signal.prices[-1])
        if exit_spot is None:
            log.warning(f"[{self.spec.symbol}] Cannot resolve: no exchange price.")
            self._open_pos = None
            return (0.0, 0.0, 0)

        ptb = pos.price_to_beat
        if ptb is None:
            log.warning(f"[{self.spec.symbol}] Cannot resolve: no price_to_beat.")
            self._open_pos = None
            return (0.0, 0.0, 0)

        # YES position: win when exit_spot > price_to_beat
        # NO position:  win when exit_spot < price_to_beat
        if pos.side == "yes":
            won = exit_spot > ptb
        else:
            won = exit_spot < ptb
        exit_price = 1.0 if won else 0.0

        log.info(
            f"[{self.spec.symbol}] RESOLVE side={pos.side.upper()} | "
            f"ptb={ptb:.4f} exit={exit_spot:.4f} {'UP' if exit_spot > ptb else 'DOWN'} | "
            f"{'WIN' if won else 'LOSS'}"
        )
        window_id_str = _fmt_window_id(pos.window_id)
        pnl = self.sim.record(
            ticker=pos.market_ticker,
            asset=self.spec.symbol,
            entry=pos.entry_price,
            exit_=exit_price,
            contracts=pos.contracts,
            window_id=window_id_str,
            price_to_beat=ptb,
            exit_spot=exit_spot,
            side=pos.side,
        )
        self._open_pos = None
        self.sim.save()
        return (exit_spot, pnl, 1)

    def _maybe_early_exit(self, yes_prob: float) -> bool:
        """
        Cut losses when spot/market clearly turned against the open position.
        Exits at current Kalshi mid (mark-to-market) instead of riding to binary 0/1.
        """
        if not cfg.EARLY_EXIT_ENABLED or self._open_pos is None:
            return False

        pos = self._open_pos
        elapsed = time.time() - pos.entered_at
        if elapsed < cfg.EARLY_EXIT_MIN_HOLD_SECS:
            return False

        ptb = pos.price_to_beat
        spot = self.synthetic_spot.spot_mid
        if ptb is None or spot is None or ptb <= 0 or spot <= 0:
            return False

        bps = cfg.EARLY_EXIT_SPOT_ADVERSE_BPS / 10_000.0
        if pos.side == "yes":
            adverse_spot = spot < ptb * (1.0 - bps)
        else:
            adverse_spot = spot > ptb * (1.0 + bps)

        mtm_exit = yes_prob if pos.side == "yes" else (1.0 - yes_prob)
        mtm_exit = max(0.01, min(0.99, mtm_exit))
        premium = pos.entry_price * pos.contracts
        unrealized = (mtm_exit - pos.entry_price) * pos.contracts
        loss_frac = (-unrealized / premium) if premium > 0 and unrealized < 0 else 0.0

        time_left = self._time_remaining_secs()
        reason = None
        if loss_frac >= cfg.EARLY_EXIT_LOSS_FRACTION:
            reason = f"mtm_loss_{loss_frac:.0%}"
        elif adverse_spot and time_left <= cfg.EARLY_EXIT_MIN_TIME_LEFT_SECS:
            reason = "adverse_spot_late_window"
        elif adverse_spot and loss_frac >= cfg.EARLY_EXIT_MODERATE_LOSS_FRAC:
            reason = f"adverse_spot_mtm_{loss_frac:.0%}"

        if not reason:
            return False

        log.warning(
            f"[{self.spec.symbol}] EARLY EXIT ({reason}) | side={pos.side.upper()} "
            f"entry={pos.entry_price:.3f} mtm={mtm_exit:.3f} | "
            f"ptb={ptb:.4f} spot={spot:.4f} t_left={time_left:.0f}s"
        )
        pnl = self.sim.record(
            ticker=pos.market_ticker,
            asset=pos.asset,
            entry=pos.entry_price,
            exit_=mtm_exit,
            contracts=pos.contracts,
            strategy="early_exit",
            window_id=_fmt_window_id(pos.window_id),
            price_to_beat=ptb,
            exit_spot=float(spot),
            side=pos.side,
        )
        self._open_pos = None
        self.sim.save()
        return True

    # ─── Price update (called by Kalshi WS handler) ───────────────────────────

    def on_price_update(self, yes_prob: float) -> dict:
        """
        Called whenever a new YES probability arrives from Kalshi.
        Returns the decision dict (for logging / acting on).
        """
        now = time.time()
        # Update lag tracker when we have both Binance and Kalshi prices
        if self.signal.prices:
            self.lag_tracker.update(float(self.signal.prices[-1]), yes_prob)

        # Staleness guard
        if self._last_price_val is not None:
            if (abs(yes_prob - self._last_price_val) < 1e-6
                    and now - self._last_price_ts > cfg.PRICE_MAX_AGE_SECS):
                return self._wait("stale_price", yes_prob)

        self._last_price_ts  = now
        self._last_price_val = yes_prob
        self._kalshi_prob_history.append((now, yes_prob))

        # Record Kalshi price tick (non-blocking)
        if self.recorder:
            self.recorder.record({
                "asset": self.spec.symbol,
                "yes_price": yes_prob,
                "ticker": self._ticker,
                "window_id": self._window_id,
            })

        # Window rollover handled in run_price_feed (avoids duplicate on_window_advance → double polling)

        if not self._ticker:
            return self._wait("no_market", yes_prob)

        if self._open_pos is not None and self._maybe_early_exit(yes_prob):
            return self._wait("early_exit_done", yes_prob)

        self._total_ticks += 1
        d = self.make_decision(yes_prob)
        self._log_decision(d, yes_prob)

        if d["action"] != "WAIT":
            self._execute(d, yes_prob)

        return d

    # ─── Decision ─────────────────────────────────────────────────────────────

    def make_decision(self, yes_price_raw: float) -> dict:
        # Telegram / dashboard pause — block new entries only
        try:
            from .runtime_control import entries_paused
            if entries_paused():
                return self._wait("telegram_paused", yes_price_raw)
        except Exception:
            pass

        # Circuit breaker
        halted, halt_reason = self.sim.is_halted(self.spec.symbol)
        if halted:
            return self._wait(f"circuit_breaker({halt_reason})", yes_price_raw)

        # Activity mandate: after long idle, ease soft gates / smaller size (not hard risk)
        activity_probe = self.sim.activity_idle()
        spot_conf_min = (
            min(cfg.SPOT_CONFIDENCE_MIN, getattr(cfg, "ACTIVITY_SPOT_CONF_FLOOR", 0.30))
            if activity_probe else cfg.SPOT_CONFIDENCE_MIN
        )
        lag_absent_min = (
            cfg.LAG_ABSENT_MIN * float(getattr(cfg, "ACTIVITY_LAG_SCALE", 0.70))
            if activity_probe else cfg.LAG_ABSENT_MIN
        )

        # Signal warmup — MUST block all trades; no strategy can bypass this
        if not self.signal.is_ready():
            return self._wait("signal_warmup", yes_price_raw)

        # Already in a position for this window
        if self._open_pos is not None:
            return self._wait("position_open", yes_price_raw)

        # Window timing
        if not self._window_position_ok():
            return self._wait("window_boundary", yes_price_raw)

        # Macro blackout
        if _is_in_blackout():
            return self._wait("macro_blackout", yes_price_raw)

        # Venue dislocation guard (protects against feed anomalies)
        if self.synthetic_spot.dislocation > 0.002:
            return self._wait("venue_dislocation", yes_price_raw)
        # Require enough fresh venues (1≈0.3, 2≈0.6); threshold from config
        spot_conf = self.synthetic_spot.confidence
        if spot_conf < spot_conf_min:
            return self._wait("spot_confidence_low", yes_price_raw)

        # Volatility filter
        rv         = self.signal.get_realized_vol()
        vol_scalar = vol_position_scalar(rv)
        if vol_scalar == 0.0:
            return self._wait(f"vol_too_high({rv:.2f})", yes_price_raw)

        # Get spot_now early (needed for structural model)
        time_remaining = self._time_remaining_secs()
        sm = self.synthetic_spot.spot_mid
        if sm is not None and sm > 0:
            spot_now = float(sm)
        elif self.signal.prices:
            spot_now = float(self.signal.prices[-1])
        else:
            spot_now = None

        # Resolve price_to_beat: official floor_strike preferred; never invent mid-window strike
        if self._price_to_beat_source != "floor_strike":
            floor = _floor_strike_from_market(self._market)
            if (
                floor is None
                and self._ticker
                and self._ptb_floor_refresh_attempted != self._window_id
            ):
                # Refresh THIS ticker only (do not re-discover active market — that can
                # replace a deliberate/test PTB with an unrelated live strike).
                self._ptb_floor_refresh_attempted = self._window_id
                m = self.kalshi.get_market(self._ticker)
                if m:
                    self._market = m
                    floor = _floor_strike_from_market(m)
            if floor is not None:
                self._price_to_beat = floor
                self._price_to_beat_source = "floor_strike"
                log.info(f"[{self.spec.symbol}] price_to_beat from floor_strike={floor:.2f}")

        if self._price_to_beat is None and spot_now is not None and spot_now > 0:
            elapsed = (time.time() - self._window_start) if self._window_start else 9999.0
            if elapsed <= cfg.PTB_CAPTURE_SECS:
                self._price_to_beat = spot_now
                self._price_to_beat_source = "window_open_spot"
                log.info(
                    f"[{self.spec.symbol}] price_to_beat from open spot={spot_now:.2f} "
                    f"(elapsed={elapsed:.0f}s<{cfg.PTB_CAPTURE_SECS:.0f}s)"
                )
            else:
                return self._wait("price_to_beat_unreliable", yes_price_raw)

        # Require price-to-beat for threshold distance
        if self._price_to_beat is None:
            # Warn if price_to_beat still None after 30s into the window
            if (self._window_start is not None
                    and time.time() - self._window_start >= 30
                    and self._price_to_beat_30s_warned != self._window_id):
                log.warning(f"[{self.spec.symbol}] price_to_beat still None after 30s")
                self._price_to_beat_30s_warned = self._window_id
            return self._wait("no_price_to_beat", yes_price_raw)

        spot_start = self._price_to_beat

        if spot_now is None or spot_start is None:
            return self._wait("no_spot_for_structural", yes_price_raw)

        # Clamp time_remaining to avoid tiny tau → exploding z-scores near window open
        time_remaining = max(time_remaining, 60.0)

        # Smooth market price
        p_market = self.tracker.update(yes_price_raw)
        min_edge = self.tracker.adjusted_min_edge
        if activity_probe:
            min_edge *= float(getattr(cfg, "ACTIVITY_EDGE_SCALE", 0.70))

        # Threshold features (central ranking signal)
        tf = compute_threshold_features(
            spot_now=spot_now,
            spot_start=spot_start,
            time_remaining_secs=time_remaining,
            annualized_vol=self.signal.get_realized_vol(),
            p_market=p_market,
            spot_confidence=self.synthetic_spot.confidence,
            lag_confidence=self.lag_tracker.lag_confidence,
        )

        if tf.p_base is None:
            d = self._wait("structural_prob_invalid", yes_price_raw, spot_now=spot_now, spot_start=spot_start)
            d.update(_tf_log_fields(tf))
            return d

        # Diagnostic: time_remaining, tau, vol, z for debugging (DEBUG to avoid log spam)
        tau = time_remaining / (365 * 24 * 3600)
        vol = self.signal.get_realized_vol()
        log.debug(
            f"[{self.spec.symbol}] spot_now={spot_now:.2f} spot_start={spot_start:.2f} "
            f"time_remaining={time_remaining:.1f} tau={tau:.4f} vol={vol:.4f} z={tf.z_threshold:.4f}"
        )

        p_base = tf.p_base
        if p_base < self._p_base_min or p_base > self._p_base_max:
            log.debug(f"[{self.spec.symbol}] p_base={p_base} out of valid range [{self._p_base_min},{self._p_base_max}], skipping")
            d = self._wait("structural_model_invalid", yes_price_raw, p_base=p_base, alpha_micro=0.0, spot_now=spot_now, spot_start=spot_start)
            d.update(_tf_log_fields(tf))
            return d

        # Skip structurally undecided windows (coin-flip p_base → noisy edge estimates)
        if abs(p_base - 0.5) < cfg.P_BASE_CENTER_MIN:
            d = self._wait(
                f"p_base_near_50({p_base:.3f})",
                yes_price_raw, p_base=p_base, alpha_micro=0.0,
                spot_now=spot_now, spot_start=spot_start,
            )
            d.update(_tf_log_fields(tf))
            return d

        # Store for cross-asset divergence monitoring
        self._last_p_base = p_base
        self._last_p_base_ts = time.time()
        self._last_p_base_window_id = self._window_id

        # Cross-asset consistency: compare p_base across assets in same window
        if self._all_engines:
            now = time.time()
            for other_sym, other_eng in self._all_engines.items():
                if other_sym == self.spec.symbol:
                    continue
                if (other_eng._last_p_base is not None
                        and other_eng._last_p_base_window_id == self._window_id
                        and now - other_eng._last_p_base_ts < 30):
                    delta = abs(p_base - other_eng._last_p_base)
                    if delta >= 0.15:  # flag when p_base differs by 15+ percentage points
                        log.info(
                            f"Cross-asset divergence: {self.spec.symbol} p_base={p_base:.3f} "
                            f"{other_sym} p_base={other_eng._last_p_base:.3f} delta={delta:.3f}"
                        )

        # Staleness: skip near-50/50 when data feels unreliable
        if 0.495 <= yes_price_raw <= 0.505:
            if self._last_price_ts > 0:
                age = time.time() - self._last_price_ts
                if age > cfg.PRICE_MAX_AGE_SECS:
                    return self._wait(f"price_stale_at_50({age:.0f}s)", yes_price_raw, spot_now=spot_now, spot_start=spot_start)

        # Skip near 50/50 only when lag and mispricing are both weak
        if (0.47 <= yes_price_raw <= 0.53
                and self.lag_tracker.lag_confidence < 0.4
                and (tf.confidence_weighted_mispricing is None
                     or abs(tf.confidence_weighted_mispricing) < 0.04)):
            d = self._wait("uncertain_near_50", yes_price_raw, p_base=p_base, alpha_micro=0.0, spot_now=spot_now, spot_start=spot_start)
            d.update(_tf_log_fields(tf))
            return d

        # ARCHITECTURE RULE:
        # Never blend probabilities directly (no weighted averages of p values).
        # Always blend in logit space: logit(p_final) = logit(p_base) + alpha.
        # p_base comes from the structural model only.
        # alpha comes from microstructure signals only.
        # Keep these two layers separate. Do not merge them.

        # Clamp away from 0 and 1 before logit to avoid infinities
        p_base_clamped = max(0.001, min(0.999, p_base))

        # Microstructure overlay in logit space (parameterized model)
        raw_features = {
            "obi":                self.signal._obi(),
            "ofi_hawkes":         self.signal._ofi_hawkes(),
            "microprice_dev":     self.signal._microprice_sig(),
            "trade_sign_autocorr": self.signal._trade_sign_autocorr(),
            "lag_signal":         self.lag_tracker.lag_signal,
            "response_gap":       self.lag_tracker.response_gap,
        }
        self.alpha_model.update_stats(raw_features)
        alpha_micro = self.alpha_model.compute(raw_features)
        bias, uncertainty = self.signal.get_bias_score()

        # Blend: structural model anchors, microstructure adjusts
        p_real = sigmoid(logit(p_base_clamped) + alpha_micro)

        # Block entry when p_real is stuck near 0.50 (no valid signal)
        if 0.48 <= p_real <= 0.52:
            return self._wait("p_real_near_50", yes_price_raw, p_base=p_base, alpha_micro=alpha_micro)

        # Portfolio cap: check before strategy (hard risk limit)
        if self._all_engines:
            all_positions = [
                e._open_pos for e in self._all_engines.values()
                if e._open_pos is not None
            ]
            gross = self.sim.gross_open_exposure(all_positions)
            if gross + cfg.MAX_POS_PCT > cfg.PORTFOLIO_GROSS_CAP:
                return self._wait(f"portfolio_cap({gross:.1%})", yes_price_raw, spot_now=spot_now, spot_start=spot_start)

        # Strategy router
        sm = self.synthetic_spot.spot_mid
        if sm and sm > 0:
            self._spot_history.append((time.time(), sm))
        snapshot = {
            "p_base":                          tf.p_base,
            "p_market":                        p_market,
            "lag_confidence":                  self.lag_tracker.lag_confidence,
            "spot_confidence":                 self.synthetic_spot.confidence,
            "confidence_weighted_mispricing":  tf.confidence_weighted_mispricing,
            "z_threshold":                     tf.z_threshold,
            "kalshi_quote_age_secs":           self._last_price_age(),
            "kalshi_spread":                   self._last_kalshi_spread(),
            "dislocation":                     self.synthetic_spot.dislocation,
            "time_remaining_secs":             time_remaining,
            "per_venue_mids":                  self.synthetic_spot.venue_mids(),
            "per_venue_staleness":             self.synthetic_spot.venue_staleness(),
            "synthetic_mid":                   sm,
            "kalshi_prob_change_1s":           self._kalshi_prob_change_1s(),
            "spot_return_1s":                  self._spot_return_1s(),
        }
        signal = self.router.route(snapshot)
        if signal.action == "WAIT":
            # Optional secondary path — disabled under higher_sharpe (R8: majority of losing entries)
            p_real_edge = abs(p_real - yes_price_raw)
            lag_conf = self.lag_tracker.lag_confidence
            if (
                cfg.ALPHA_EDGE_ENABLED
                and p_real_edge >= cfg.ALPHA_EDGE_MIN
                and lag_conf >= cfg.ALPHA_EDGE_LAG_MIN
            ):
                action = "BUY_YES" if p_real > yes_price_raw else "BUY_NO"
                ev = p_real - yes_price_raw
                signal_strategy = "alpha_edge"
            elif (
                cfg.ALPHA_EDGE_ENABLED
                and cfg.ALPHA_EDGE_BAND_LOW <= p_real_edge < cfg.ALPHA_EDGE_MIN
                and lag_conf >= cfg.ALPHA_EDGE_LAG_MIN
            ):
                d = self._wait("alpha_edge_insufficient", yes_price_raw, p_base=p_base, alpha_micro=alpha_micro, spot_now=spot_now, spot_start=spot_start)
                d.update(_tf_log_fields(tf))
                d["strategy"] = signal.strategy
                d["diagnostics"] = signal.diagnostics
                d["raw_features"] = raw_features
                d["alpha_micro"] = alpha_micro
                return d
            else:
                d = self._wait(signal.reason, yes_price_raw, p_base=p_base, alpha_micro=alpha_micro, spot_now=spot_now, spot_start=spot_start)
                d.update(_tf_log_fields(tf))
                d["strategy"] = signal.strategy
                d["diagnostics"] = signal.diagnostics
                d["raw_features"] = raw_features
                d["alpha_micro"] = alpha_micro
                return d
        else:
            action = signal.action
            ev = signal.score if signal.score != 0 else (p_real - p_market)
            signal_strategy = signal.strategy

        if abs(ev) < min_edge:
            return self._wait(f"edge({ev:+.3f}<{min_edge:.3f})", yes_price_raw, p_base=p_base, alpha_micro=alpha_micro, spot_now=spot_now, spot_start=spot_start)

        conv = self.signal.conviction()
        if conv < cfg.MIN_CONVICTION:
            return self._wait(f"conviction({conv}<{cfg.MIN_CONVICTION})", yes_price_raw)

        # Lag gate: only lag_arb requires detectable Kalshi lag (strategy already checks in lag_arb.py)
        lag_conf = self.lag_tracker.lag_confidence
        if signal_strategy == "lag_arb" and lag_conf < lag_absent_min:
            return self._wait(f"lag_absent({lag_conf:.2f})", yes_price_raw, p_base=p_base, alpha_micro=alpha_micro, spot_now=spot_now, spot_start=spot_start)

        # Sharpe gate (after enough trades to be meaningful)
        if (self.sim.total_trades >= cfg.SHARPE_MIN_TRADES
                and len(self.sim.returns_hist) >= 10):
            if self.sim.current_sharpe < cfg.SHARPE_MIN:
                return self._wait(f"sharpe({self.sim.current_sharpe:.2f}<{cfg.SHARPE_MIN})", yes_price_raw, spot_now=spot_now, spot_start=spot_start)

        # Kelly sizing + variance-aware shrink (extreme entries / noisy belief)
        if ev > 0:
            frac   = kelly_binary(p_real, p_market)
            action = "BUY_YES"
            entry_for_size = p_market
        else:
            frac   = kelly_binary(1.0 - p_real, 1.0 - p_market)
            action = "BUY_NO"
            entry_for_size = 1.0 - p_market

        # R12 lesson: all 4 losses were lottery tickets (entry < 0.15)
        min_entry = getattr(cfg, "MIN_ENTRY_PRICE", 0.0)
        max_entry = getattr(cfg, "MAX_ENTRY_PRICE", 1.0)
        if entry_for_size < min_entry:
            return self._wait(
                f"entry_too_cheap({entry_for_size:.3f}<{min_entry:.2f})",
                yes_price_raw, p_base=p_base, alpha_micro=alpha_micro,
                spot_now=spot_now, spot_start=spot_start,
            )
        if entry_for_size > max_entry:
            return self._wait(
                f"entry_too_rich({entry_for_size:.3f}>{max_entry:.2f})",
                yes_price_raw, p_base=p_base, alpha_micro=alpha_micro,
                spot_now=spot_now, spot_start=spot_start,
            )

        frac *= vol_scalar
        frac *= entry_variance_scalar(entry_for_size)
        frac *= belief_vol_scalar(self.tracker.belief_vol)
        max_pos = cfg.MAX_POS_PCT
        if activity_probe:
            max_pos = min(max_pos, float(getattr(cfg, "ACTIVITY_PROBE_SIZE_PCT", 0.025)))
        size_usd = min(self.sim.balance * frac, self.sim.balance * max_pos)
        size_usd = max(0.0, size_usd)

        ok, reason = self.sim.can_trade(size_usd, abs(ev), min_edge, asset=self.spec.symbol)
        if not ok:
            return self._wait(reason, yes_price_raw, p_base=p_base, alpha_micro=alpha_micro, spot_now=spot_now, spot_start=spot_start)

        if size_usd < cfg.MIN_TRADE_USD:
            return self._wait(f"size_too_small(${size_usd:.2f})", yes_price_raw, p_base=p_base, alpha_micro=alpha_micro, spot_now=spot_now, spot_start=spot_start)

        if activity_probe:
            idle_m = self.sim.seconds_since_last_trade() / 60.0
            log.warning(
                f"[{self.spec.symbol}] ACTIVITY PROBE idle={idle_m:.0f}m — "
                f"eased gates, size cap {max_pos:.1%}"
            )
        log.warning(
            f"[{self.spec.symbol}] Pre-trade p_base={p_base:.4f} p_real={p_real:.4f} "
            f"strategy={signal_strategy} size=${size_usd:.2f} ptb_src={self._price_to_beat_source}"
        )
        return {
            "action":        action,
            "size_usd":      size_usd,
            "ev":            ev,
            "p_real":        p_real,
            "p_market":      p_market,
            "p_base":        p_base,
            "spot_now":      spot_now,
            "spot_start":    spot_start,
            "strategy":      ("activity_probe+" + signal_strategy) if activity_probe else signal_strategy,
            "diagnostics":   signal.diagnostics,
            "raw_features":  raw_features,
            "alpha_micro":   alpha_micro,
            "z_threshold":   tf.z_threshold,
            "mispricing_base": tf.mispricing_base,
            "confidence_weighted_mispricing": tf.confidence_weighted_mispricing,
            "spot_confidence": tf.spot_confidence,
            "lag_confidence": tf.lag_confidence,
            "conviction":    conv,
            "bias":          bias,
            "uncertainty":   uncertainty,
            "belief_vol":    self.tracker.belief_vol,
            "price_to_beat_source": self._price_to_beat_source,
            "time_remaining": self._time_remaining_secs(),
            "reason":        "OK",
        }

    # ─── Execution ────────────────────────────────────────────────────────────

    def _execute(self, d: dict, yes_price: float) -> None:
        spot_conf = d.get("spot_confidence")
        if spot_conf is not None and spot_conf < cfg.SPOT_CONFIDENCE_MIN:
            log.warning(
                f"[{self.spec.symbol}] Aborting order: "
                f"spot_confidence={spot_conf:.2f} < {cfg.SPOT_CONFIDENCE_MIN} (feed unreliable)"
            )
            return
        p_real = d.get("p_real")
        if p_real is not None and 0.48 <= p_real <= 0.52:
            log.warning(f"[{self.spec.symbol}] Aborting order: p_real={p_real:.4f} in [0.48,0.52] (no valid signal)")
            return

        side         = "yes" if d["action"] == "BUY_YES" else "no"
        entry_price  = yes_price if side == "yes" else (1.0 - yes_price)
        if entry_price < self._p_base_min:
            log.warning(f"[{self.spec.symbol}] Rejecting entry: price={entry_price:.4f} < {self._p_base_min} (min)")
            return
        min_entry = getattr(cfg, "MIN_ENTRY_PRICE", 0.0)
        max_entry = getattr(cfg, "MAX_ENTRY_PRICE", 1.0)
        if entry_price < min_entry or entry_price > max_entry:
            log.warning(
                f"[{self.spec.symbol}] Rejecting entry: price={entry_price:.4f} "
                f"outside [{min_entry:.2f}, {max_entry:.2f}]"
            )
            return
        # Kalshi: each contract costs entry_price dollars; you receive $1 on win.
        # contracts = floor(size_usd / entry_price)
        contracts = max(1, int(d["size_usd"] / max(entry_price, 0.01)))

        log.info(
            f"[{self.spec.symbol}] >> {d['action']:8} | EV={d['ev']:+.4f} | "
            f"p_real={d['p_real']:.4f} p_mkt={d['p_market']:.4f} | "
            f"bias={d['bias']:+.3f} | conv={d['conviction']}/5 | "
            f"×{contracts} contracts (${d['size_usd']:.2f})"
        )

        pos = OpenPosition(
            window_id     = self._window_id,
            market_ticker = self._ticker,
            asset         = self.spec.symbol,
            side          = side,
            entry_price   = entry_price,
            contracts     = contracts,
            amount_usdc   = entry_price * contracts,
            entered_at    = time.time(),
            price_to_beat = self._price_to_beat,
        )
        self._open_pos = pos  # Set BEFORE order to prevent duplicate orders on rapid ticks
        ok = self.kalshi.place_market_order(
            ticker=self._ticker,
            side=side,
            count=contracts,
        )
        if not ok:
            log.warning(f"[{self.spec.symbol}] Order failed — position NOT recorded.")
            self._open_pos = None
            return

        log.info(
            f"[{self.spec.symbol}] Position open | side={side.upper()} "
            f"entry={entry_price:.4f} ×{contracts} | ptb={self._price_to_beat}"
        )
        self.sim.save()

    # ─── Window timing ────────────────────────────────────────────────────────

    def _window_position_ok(self) -> bool:
        if self._window_start is None:
            return False
        elapsed = time.time() - self._window_start
        if elapsed < cfg.SKIP_OPEN_SECS:
            return False
        if elapsed > cfg.WINDOW_SECS - cfg.SKIP_CLOSE_SECS:
            return False
        return True

    def _time_remaining_secs(self) -> float:
        if self._window_start is None:
            return cfg.WINDOW_SECS / 2
        elapsed = time.time() - self._window_start
        return max(0, cfg.WINDOW_SECS - elapsed)

    # ─── WAIT helper ─────────────────────────────────────────────────────────

    def _wait(
        self,
        reason: str,
        yes_price: float,
        p_base: Optional[float] = None,
        alpha_micro: Optional[float] = None,
        spot_now: Optional[float] = None,
        spot_start: Optional[float] = None,
    ) -> dict:
        bucket = reason.split("(")[0].split("<")[0]
        self._wait_counts[bucket] = self._wait_counts.get(bucket, 0) + 1
        now = time.time()
        if now - self._wait_last_log >= 120:
            total = sum(self._wait_counts.values())
            ranked = sorted(self._wait_counts.items(), key=lambda x: -x[1])[:5]
            parts  = "  ".join(f"{r}={n}" for r, n in ranked)
            log.info(f"[{self.spec.symbol}] WAIT summary ({total} skips) | {parts}")
            self._wait_last_log = now
        time_remaining = self._time_remaining_secs()
        dist_from_threshold = (
            (self.signal.prices[-1] - self._price_to_beat)
            if self.signal.prices and self._price_to_beat is not None else 0.0
        )
        if p_base is not None and alpha_micro is not None:
            p_real = sigmoid(logit(max(0.001, min(0.999, p_base))) + alpha_micro)
        else:
            p_real = None
        out = {
            "action": "WAIT", "size_usd": 0, "ev": 0,
            "p_real": p_real, "p_market": yes_price,
            "conviction": 0, "reason": reason,
            "time_remaining": time_remaining,
            "dist_from_threshold": dist_from_threshold,
            "lag_signal": self.lag_tracker.lag_signal,
            "lag_confidence": self.lag_tracker.lag_confidence,
        }
        if spot_now is not None:
            out["spot_now"] = spot_now
        if spot_start is not None:
            out["spot_start"] = spot_start
        return out

    # ─── Diagnostics ───────────────────────────────────────────────────────────

    def _log_decision(self, d: dict, yes_price_raw: float) -> None:
        try:
            # Fill spot_now from dict or best available (synthetic_spot > signal.prices)
            spot_now = d.get("spot_now")
            if spot_now is None:
                sm = self.synthetic_spot.spot_mid
                if sm is not None and sm > 0:
                    spot_now = float(sm)
                elif self.signal.prices:
                    spot_now = float(self.signal.prices[-1])
            spot_start = d.get("spot_start") if d.get("spot_start") is not None else self._price_to_beat
            rec = {
                "ts":           datetime.now(timezone.utc).isoformat(),
                "asset":        self.spec.symbol,
                "window_id":    _fmt_window_id(self._window_id) if self._window_id > 0 else "",
                "window_id_ts": self._window_id,
                "ticker":       self._ticker,
                "price_to_beat": self._price_to_beat,
                "yes_price_raw": yes_price_raw,
                "action":       d["action"],
                "reason":       d.get("reason", ""),
                "p_real":       d.get("p_real"),
                "p_market":     d.get("p_market"),
                "p_base":       round(d.get("p_base"), 4) if d.get("p_base") is not None else None,
                "spot_now":     round(spot_now, 2) if spot_now is not None else None,
                "spot_start":   round(spot_start, 2) if spot_start is not None else None,
                "alpha_micro":  round(d.get("alpha_micro"), 4) if d.get("alpha_micro") is not None else None,
                "z_threshold":  round(d.get("z_threshold"), 4) if d.get("z_threshold") is not None else None,
                "mispricing_base": round(d.get("mispricing_base"), 4) if d.get("mispricing_base") is not None else None,
                "confidence_weighted_mispricing": round(d.get("confidence_weighted_mispricing"), 4) if d.get("confidence_weighted_mispricing") is not None else None,
                "spot_confidence": round(d.get("spot_confidence"), 4) if d.get("spot_confidence") is not None else None,
                "lag_confidence": d.get("lag_confidence"),
                "strategy":       d.get("strategy"),
                "diagnostics":    d.get("diagnostics"),
                "raw_features":  d.get("raw_features"),
                "alpha_micro":   round(d.get("alpha_micro"), 4) if d.get("alpha_micro") is not None else None,
                "price_to_beat_source": d.get("price_to_beat_source") or self._price_to_beat_source or None,
                "ev":           d.get("ev"),
                "bias":         d.get("bias"),
                "conviction":   d.get("conviction"),
                "belief_vol":   d.get("belief_vol"),
                "time_remaining": d.get("time_remaining"),
                "dist_from_threshold": d.get("dist_from_threshold"),
                "lag_signal":   d.get("lag_signal"),
                "signals":      self.signal.get_components(),
            }
            Path(DECISION_LOG).parent.mkdir(parents=True, exist_ok=True)
            rotate_log_if_needed(DECISION_LOG, DECISIONS_MAX_LINES, DECISIONS_KEEP_LINES)
            with open(DECISION_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            if self.recorder:
                self.recorder.record({"_type": "feature", **rec})
        except Exception as e:
            log.warning(f"Decision log write failed: {e}")

    def print_status(self) -> None:
        pos_str = "none"
        if self._open_pos:
            p = self._open_pos
            held = time.time() - p.entered_at
            pos_str = (f"{p.side.upper()} ×{p.contracts} "
                       f"entry={p.entry_price:.3f} ${p.amount_usdc:.2f} held={held:.0f}s")
        stats = self.sim.asset_stats.get(self.spec.symbol)
        wr = stats.win_rate if stats else 0.0
        log.info(
            f"[{self.spec.symbol}] ticks={self._total_ticks} "
            f"WR={wr:.1%} pos={pos_str} ticker={self._ticker or '?'}"
        )


# ─── Utility ──────────────────────────────────────────────────────────────────

def _fmt_window(wid: int) -> str:
    return datetime.fromtimestamp(wid, tz=timezone.utc).strftime("%H:%M")


def _fmt_window_id(wid: int) -> str:
    """Format window_id as YYYY-MM-DD HH:MM for grouping."""
    return datetime.fromtimestamp(wid, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
