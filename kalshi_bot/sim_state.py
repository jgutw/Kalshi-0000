"""
sim_state.py — Simulation state with cooldown-based circuit breaker.

Key fix vs old bot:
  OLD: is_halted() returned True forever after MAX_CONSEC_LOSSES.
       Only a manual restart could resume trading.
  NEW: When halted by consecutive losses, bot enters a COOLDOWN period.
       After COOLDOWN_MINUTES (default 60), is_halted() returns False again
       and consecutive loss counter resets. Daily loss halt still requires
       the next UTC day to reset (same behavior as before).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .config import cfg

log = logging.getLogger("sim_state")
TRADE_LOG = "logs/kalshi_trades.jsonl"


# ═══════════════════════════════════════════════════════════════════════════════
# Open position
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class OpenPosition:
    """One open position per 15-min window per asset."""
    window_id:      int
    market_ticker:  str
    asset:          str      # "BTC" / "ETH" / "SOL" / "XRP"
    side:           str      # "yes" or "no"
    entry_price:    float    # 0–1 probability
    contracts:      int      # number of contracts bought
    amount_usdc:    float    # dollars committed
    entered_at:     float    # unix timestamp
    price_to_beat:  Optional[float]   # exchange price at window open


# ═══════════════════════════════════════════════════════════════════════════════
# Per-asset stats (embedded in SimState)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AssetStats:
    symbol: str
    wins: int   = 0
    losses: int = 0
    total_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        n = self.wins + self.losses
        return self.wins / n if n > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# SimState
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SimState:
    balance:           float = field(default_factory=lambda: cfg.SIM_BALANCE)
    starting_balance:  float = field(default_factory=lambda: cfg.SIM_BALANCE)
    peak_balance:      float = field(default_factory=lambda: cfg.SIM_BALANCE)
    daily_start:       float = field(default_factory=lambda: cfg.SIM_BALANCE)
    daily_start_ts:    float = field(default_factory=time.time)

    total_trades:   int   = 0
    wins:           int   = 0
    losses:         int   = 0
    consec_losses:  int   = 0
    trades:         list  = field(default_factory=list)
    returns_hist:   list  = field(default_factory=list)

    # Risk metrics
    var_95:        float  = 0.0
    cvar_95:       float  = 0.0
    vol_regime:    str    = "normal"

    # Circuit breaker cooldown
    # When consec_losses >= MAX_CONSEC_LOSSES, record the halt timestamp.
    # is_halted() returns True until now > _halted_at + COOLDOWN_MINUTES * 60.
    _halted_at:    Optional[float] = field(default=None, repr=False)

    # Per-asset stats
    asset_stats:   dict  = field(default_factory=dict)   # symbol → AssetStats

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        n = self.wins + self.losses
        return self.wins / n if n > 0 else 0.0

    @property
    def current_sharpe(self) -> float:
        r = np.array(self.returns_hist)
        if len(r) < 2 or np.std(r) == 0:
            return 0.0
        return float(np.mean(r) / np.std(r))

    @property
    def daily_dd(self) -> float:
        if self.daily_start <= 0:
            return 0.0
        return (self.daily_start - self.balance) / self.daily_start

    def gross_open_exposure(self, open_positions: List[OpenPosition]) -> float:
        """Sum of all open position sizes as fraction of balance."""
        total = sum(p.amount_usdc for p in open_positions)
        return total / max(self.balance, 1.0)

    # ─── Circuit breaker ──────────────────────────────────────────────────────

    def is_halted(self) -> Tuple[bool, str]:
        """
        Returns (halted: bool, reason: str).
        Consecutive-loss halt auto-lifts after COOLDOWN_MINUTES.
        Daily-loss halt lifts at next UTC midnight (via _maybe_reset_daily).
        """
        # Daily loss check
        if self.daily_dd >= cfg.MAX_DAILY_LOSS_PCT:
            return True, f"daily_loss {self.daily_dd:.1%}"

        # Consecutive loss check with cooldown
        if self.consec_losses >= cfg.MAX_CONSEC_LOSSES:
            if self._halted_at is None:
                self._halted_at = time.time()
            elapsed = time.time() - self._halted_at
            cooldown = cfg.COOLDOWN_MINUTES * 60
            if elapsed < cooldown:
                remaining_min = (cooldown - elapsed) / 60
                return True, f"consec_loss_cooldown({remaining_min:.0f}m left)"
            else:
                # Cooldown expired — reset and resume
                log.info(f"Circuit breaker cooldown expired after {elapsed/60:.0f}m — resuming")
                self.consec_losses = 0
                self._halted_at = None

        return False, ""

    def _maybe_reset_daily(self) -> None:
        """Reset daily loss counter at UTC midnight."""
        now_utc = datetime.now(timezone.utc)
        last_reset_day = datetime.fromtimestamp(self.daily_start_ts, tz=timezone.utc).date()
        if now_utc.date() > last_reset_day:
            log.info(f"New UTC day — resetting daily_start (was ${self.daily_start:.2f})")
            self.daily_start    = self.balance
            self.daily_start_ts = time.time()

    # ─── Trade recording ──────────────────────────────────────────────────────

    def can_trade(self, size_usd: float, edge: float, min_edge: float) -> Tuple[bool, str]:
        self._maybe_reset_daily()
        halted, reason = self.is_halted()
        if halted:
            return False, reason
        if edge < min_edge:
            return False, f"edge {edge:.3f} < {min_edge:.3f}"
        if size_usd > self.balance * cfg.MAX_POS_PCT + 0.01:
            return False, "size > limit"
        return True, "OK"

    def record(
        self,
        ticker: str,
        asset: str,
        entry: float,
        exit_: float,
        contracts: int,
        strategy: str = "kalshi15m",
        window_id: str = "",
        price_to_beat: Optional[float] = None,
        exit_spot: Optional[float] = None,
        side: Optional[str] = None,
    ) -> float:
        """Record a closed trade. Returns P&L."""
        # Kalshi: each contract costs entry_price cents, pays $1 on win.
        # P&L = (exit_price - entry_price) × contracts.
        pnl = (exit_ - entry) * contracts
        trade = {
            "ts":       datetime.now().isoformat(),
            "ticker":   ticker,
            "asset":    asset,
            "entry":    round(entry, 4),
            "exit":     round(exit_, 4),
            "contracts": contracts,
            "pnl":      round(pnl, 4),
            "strategy": strategy,
            "window_id": window_id,
            "price_to_beat": price_to_beat,
            "exit_spot": exit_spot,
            "side":     side,
        }
        self.trades.append(trade)
        self.total_trades += 1
        self.balance += pnl

        # Binary: exit 1.0 = win, exit 0.0 = loss (explicit check; pnl can have float quirks)
        won = exit_ >= 0.5
        if won:
            self.wins        += 1
            self.consec_losses = 0
            self._halted_at   = None   # clear cooldown timer on win
        else:
            self.losses        += 1
            self.consec_losses += 1
            if self.consec_losses >= cfg.MAX_CONSEC_LOSSES and self._halted_at is None:
                self._halted_at = time.time()
                log.warning(f"Circuit breaker: {self.consec_losses} consecutive losses — "
                            f"cooldown {cfg.COOLDOWN_MINUTES:.0f}m")

        ref = entry * contracts
        if ref > 0:
            self.returns_hist.append(pnl / ref)

        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

        # Per-asset stats
        s = self.asset_stats.setdefault(asset, AssetStats(asset))
        if isinstance(s, dict):
            s = AssetStats(**s)
            self.asset_stats[asset] = s
        if pnl > 0:
            s.wins += 1
        else:
            s.losses += 1
        s.total_pnl += pnl

        self._update_risk()

        pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
        log.info(f"{'WIN' if won else 'LOSS'} [{asset}] PnL=${pnl_str}  "
                 f"Balance=${self.balance:.2f}  WR={self.win_rate:.1%}  "
                 f"Streak={self.consec_losses}")

        # JSONL log (include window_id for grouping)
        try:
            Path(TRADE_LOG).parent.mkdir(parents=True, exist_ok=True)
            out = {**trade, "balance": self.balance, "win_rate": self.win_rate}
            if not out.get("window_id"):
                out.pop("window_id", None)
            if out.get("price_to_beat") is None:
                out.pop("price_to_beat", None)
            if out.get("exit_spot") is None:
                out.pop("exit_spot", None)
            if out.get("side") is None:
                out.pop("side", None)
            with open(TRADE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(out) + "\n")
        except OSError:
            pass

        return pnl

    def _update_risk(self) -> None:
        if len(self.returns_hist) < 20:
            return
        r = np.array(self.returns_hist[-200:])
        var = float(np.percentile(r, 5))
        self.var_95  = abs(var)
        tail = r[r <= var]
        self.cvar_95 = abs(float(tail.mean())) if len(tail) > 0 else 0.0
        ann_vol = float(np.std(r) * np.sqrt(365 * 96))
        self.vol_regime = "high" if ann_vol > cfg.VOL_HI else "low" if ann_vol < 0.10 else "normal"

    # ─── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: str = None) -> None:
        path = path or cfg.SIM_FILE
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        asset_stats_raw = {}
        for k, v in self.asset_stats.items():
            if isinstance(v, AssetStats):
                asset_stats_raw[k] = {"symbol": v.symbol, "wins": v.wins,
                                      "losses": v.losses, "total_pnl": v.total_pnl}
            else:
                asset_stats_raw[k] = v
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "balance":          self.balance,
                    "starting_balance": self.starting_balance,
                    "peak_balance":     self.peak_balance,
                    "daily_start":      self.daily_start,
                    "daily_start_ts":   self.daily_start_ts,
                    "total_trades":     self.total_trades,
                    "wins":             self.wins,
                    "losses":           self.losses,
                    "consec_losses":    self.consec_losses,
                    "_halted_at":       self._halted_at,
                    "var_95":           self.var_95,
                    "cvar_95":          self.cvar_95,
                    "vol_regime":       self.vol_regime,
                    "returns_hist":     self.returns_hist[-500:],
                    "trades":           self.trades[-100:],
                    "asset_stats":      asset_stats_raw,
                }, f, indent=2)
        except OSError as e:
            log.warning(f"SimState.save failed: {e}")

    @classmethod
    def load(cls, path: str = None) -> "SimState":
        path = path or cfg.SIM_FILE
        try:
            with open(path, encoding="utf-8-sig") as f:
                d = json.load(f)
            s = cls()
            s.balance          = d.get("balance",          cfg.SIM_BALANCE)
            s.starting_balance = d.get("starting_balance", cfg.SIM_BALANCE)
            s.peak_balance     = d.get("peak_balance",     s.balance)
            s.daily_start      = d.get("daily_start",      s.balance)
            s.daily_start_ts   = d.get("daily_start_ts",   time.time())
            s.total_trades     = d.get("total_trades",     0)
            s.wins             = d.get("wins",             0)
            s.losses           = d.get("losses",           0)
            s.consec_losses    = d.get("consec_losses",    0)
            s._halted_at       = d.get("_halted_at")
            s.var_95           = d.get("var_95",           0.0)
            s.cvar_95          = d.get("cvar_95",          0.0)
            s.vol_regime       = d.get("vol_regime",       "normal")
            s.returns_hist     = d.get("returns_hist",     [])
            s.trades           = d.get("trades",           [])
            # Restore per-asset stats
            for k, v in d.get("asset_stats", {}).items():
                if isinstance(v, dict):
                    s.asset_stats[k] = AssetStats(**v)
            return s
        except FileNotFoundError:
            s = cls()
            s.daily_start_ts = time.time()
            return s

    def reconcile_from_trade_log(self, trade_log_path: str | Path = None) -> bool:
        """
        If kalshi_trades.jsonl has more records than total_trades, rebuild sim state
        from the trade log. Reconciles after bot restart when trades were written
        but sim wasn't saved (e.g. crash, or loaded stale backup).
        Returns True if reconciliation was performed.
        """
        path = Path(trade_log_path or TRADE_LOG)
        if not path.exists():
            return False
        trades = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return False
        old_total = self.total_trades
        if len(trades) <= old_total:
            return False
        # Rebuild from trade log
        start = self.starting_balance
        wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
        losses = len(trades) - wins
        total_pnl = sum(float(t.get("pnl", 0)) for t in trades)
        self.total_trades = len(trades)
        self.wins = wins
        self.losses = losses
        self.balance = start + total_pnl
        self.peak_balance = max(self.peak_balance, self.balance)
        # Per-asset stats
        by_asset = {}
        for t in trades:
            a = t.get("asset", "?")
            if a not in by_asset:
                by_asset[a] = AssetStats(a)
            pnl = float(t.get("pnl", 0))
            if pnl > 0:
                by_asset[a].wins += 1
            else:
                by_asset[a].losses += 1
            by_asset[a].total_pnl += pnl
        self.asset_stats = by_asset
        # Returns history for risk metrics
        self.returns_hist = []
        for t in trades:
            ref = float(t.get("entry", 0)) * int(t.get("contracts", 0))
            pnl = float(t.get("pnl", 0))
            if ref > 0:
                self.returns_hist.append(pnl / ref)
        self._update_risk()
        # Consec losses from tail
        self.consec_losses = 0
        for t in reversed(trades):
            if float(t.get("pnl", 0)) > 0:
                break
            self.consec_losses += 1
        if self.consec_losses >= cfg.MAX_CONSEC_LOSSES:
            self._halted_at = time.time()
        else:
            self._halted_at = None
        self.trades = trades[-100:]
        log.warning(
            f"SimState reconciled from trade log: {len(trades)} trades "
            f"(sim had {old_total})"
        )
        return True
