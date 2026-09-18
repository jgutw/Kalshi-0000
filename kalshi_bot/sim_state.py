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

# C7 instrumentation: entry-time fields copied onto each closed-trade row.
# Chosen so loss attribution (strategy / edge / crowding / sizing) needs only
# kalshi_trades.jsonl — the decisions log is too sparse to join reliably.
DECISION_SNAPSHOT_FIELDS = (
    "strategy", "ev", "p_real", "p_base", "p_market",
    "confidence_weighted_mispricing", "lag_confidence", "spot_confidence",
    "conviction", "belief_vol", "alpha_micro", "z_threshold",
    "time_remaining", "realized_vol", "min_edge",
    "entry_for_size", "is_lottery",
    "portfolio_gross_at_entry", "concurrent_open",
    "size_usd", "price_to_beat_source",
)


def _decision_snapshot(decision: dict) -> dict:
    """Flat, JSON-safe subset of the entry decision."""
    out: dict = {}
    for key in DECISION_SNAPSHOT_FIELDS:
        val = decision.get(key)
        if val is None:
            continue
        if isinstance(val, (str, bool, int)):
            out[key] = val
        elif isinstance(val, float):
            out[key] = round(val, 6)
    return out


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
    fees_usdc:      float = 0.0
    order_id:       str = ""
    is_lottery:     bool = False      # entry < LOTTERY_ENTRY_MAX (sleeve accounting)
    decision:       Optional[dict] = None   # C7 entry snapshot, copied onto the trade row


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

    # Circuit breaker cooldown (global legacy + per-asset when enabled)
    _halted_at:    Optional[float] = field(default=None, repr=False)
    consec_losses_by_asset: dict = field(default_factory=dict)
    _halted_at_by_asset: dict = field(default_factory=dict)

    # Per-asset stats
    asset_stats:   dict  = field(default_factory=dict)   # symbol → AssetStats

    # Profit vault (taken cash — not used for new trades)
    vault_balance: float = 0.0
    peak_equity:   float = field(default_factory=lambda: cfg.SIM_BALANCE)
    last_trade_ts: float = 0.0   # unix time of last closed trade (0 = none yet)
    session_started_ts: float = field(default_factory=time.time)

    # Live guard mirrors (Kalshi is source of truth when DRY_RUN=False)
    live_halt_reason: str = ""
    kalshi_available: float = 0.0
    kalshi_portfolio_value: float = 0.0

    # Hourly fill-quota ranking (not persisted)
    _quota_bids: dict = field(default_factory=dict, repr=False)
    _quota_claimed_at: float = field(default=0.0, repr=False)

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
        """Daily drawdown on trading balance (legacy daily loss gate)."""
        if self.daily_start <= 0:
            return 0.0
        return (self.daily_start - self.balance) / self.daily_start

    @property
    def total_equity(self) -> float:
        """
        Trading balance + vault (+ live open mark).

        Paper: balance already is the sim book; vault is reserved out of it.
        Live: LiveGuard sets balance = Kalshi available minus vault, so
        balance + vault = cash. Open event contracts are extra
        (kalshi_portfolio_value). Omit them and Telegram equity collapses to
        leftover cash whenever tickets are open.
        """
        eq = float(self.balance) + float(self.vault_balance)
        if not cfg.DRY_RUN:
            eq += max(0.0, float(self.kalshi_portfolio_value or 0.0))
        return eq

    @property
    def peak_drawdown(self) -> float:
        """
        Peak-to-trough drawdown (fraction 0–1).
        Default: equity-based so vault skims do not freeze trading.
        """
        if getattr(cfg, "DRAWDOWN_USE_EQUITY", True):
            peak = float(self.peak_equity or 0.0)
            cur = self.total_equity
        else:
            peak = float(self.peak_balance or 0.0)
            cur = float(self.balance)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - cur) / peak)

    @property
    def skimmable_profit(self) -> float:
        """Profit above session starting capital still sitting in the trading book."""
        return max(0.0, float(self.balance) - float(self.starting_balance))

    def seconds_since_last_trade(self) -> float:
        """Seconds since last closed trade; uses session start if none yet."""
        anchor = self.last_trade_ts if self.last_trade_ts > 0 else self.session_started_ts
        if anchor <= 0:
            return 0.0
        return max(0.0, time.time() - anchor)

    def activity_idle(self) -> bool:
        """True when activity mandate should loosen soft gates."""
        if not getattr(cfg, "ACTIVITY_MANDATE_ENABLED", False):
            return False
        idle_for = float(getattr(cfg, "ACTIVITY_IDLE_SECS", 3600.0))
        return self.seconds_since_last_trade() >= idle_for

    @staticmethod
    def _trade_epoch(trade: dict) -> float:
        raw = trade.get("ts")
        if not raw:
            return 0.0
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            return 0.0

    def fills_last_seconds(
        self,
        secs: float,
        open_positions: Optional[List[OpenPosition]] = None,
    ) -> int:
        """Closed trades + still-open entries inside the lookback."""
        now = time.time()
        n = 0
        for t in self.trades:
            ts = self._trade_epoch(t if isinstance(t, dict) else {})
            if ts > 0 and (now - ts) <= secs:
                n += 1
        for pos in open_positions or []:
            entered = float(getattr(pos, "entered_at", 0.0) or 0.0)
            if entered > 0 and (now - entered) <= secs:
                n += 1
        return n

    def quota_hungry(self, open_positions: Optional[List[OpenPosition]] = None) -> bool:
        if not getattr(cfg, "FILL_QUOTA_ENABLED", False):
            return False
        need = int(getattr(cfg, "MIN_FILLS_PER_HOUR", 1) or 0)
        if need <= 0:
            return False
        return self.fills_last_seconds(3600.0, open_positions) < need

    def publish_quota_bid(self, symbol: str, score: float) -> None:
        self._quota_bids[str(symbol)] = (time.time(), float(score))

    def is_winning_quota_bid(self, symbol: str) -> bool:
        now = time.time()
        collect = float(getattr(cfg, "QUOTA_COLLECT_SECS", 1.2))
        fresh = {
            s: (ts, sc)
            for s, (ts, sc) in self._quota_bids.items()
            if now - float(ts) <= collect + 1.5
        }
        if str(symbol) not in fresh:
            return False
        oldest = min(ts for ts, _ in fresh.values())
        if now - oldest < collect and len(fresh) < 5:
            return False
        best = max(sc for _, sc in fresh.values())
        return fresh[str(symbol)][1] + 1e-12 >= best

    def try_claim_quota(self) -> bool:
        now = time.time()
        if now - float(self._quota_claimed_at or 0.0) < 45.0:
            return False
        self._quota_claimed_at = now
        return True

    def _touch_peaks(self) -> None:
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        eq = self.total_equity
        if eq > self.peak_equity:
            self.peak_equity = eq

    def gross_open_exposure(self, open_positions: List[OpenPosition]) -> float:
        """Sum of all open position sizes as fraction of working capital."""
        total = sum(p.amount_usdc for p in open_positions)
        if not cfg.DRY_RUN:
            # Live: balance is Kalshi *available*; include open premium in denominator
            return total / max(float(self.balance) + total, 1.0)
        return total / max(self.balance, 1.0)

    def set_live_halt(self, reason: str) -> None:
        self.live_halt_reason = str(reason or "live_halt")
        log.error("LIVE HALT: %s", self.live_halt_reason)

    def clear_live_halt(self) -> None:
        if self.live_halt_reason:
            log.info("LIVE HALT cleared (was: %s)", self.live_halt_reason)
        self.live_halt_reason = ""

    # ─── Circuit breaker ──────────────────────────────────────────────────────

    def is_halted(self, asset: Optional[str] = None) -> Tuple[bool, str]:
        """
        Returns (halted: bool, reason: str).
        Consecutive-loss halt auto-lifts after COOLDOWN_MINUTES.
        Daily-loss halt lifts at next UTC midnight (via _maybe_reset_daily).
        When PER_ASSET_CIRCUIT_BREAKER and asset is set, only that asset's streak applies.
        """
        self._maybe_reset_daily()
        if not cfg.DRY_RUN and self.live_halt_reason:
            return True, self.live_halt_reason
        if not cfg.DRY_RUN and float(self.balance) < float(getattr(cfg, "LIVE_MIN_AVAILABLE_USD", 2.0)):
            return True, f"live_available_too_low(${self.balance:.2f})"
        if self.daily_dd >= cfg.MAX_DAILY_LOSS_PCT:
            return True, f"daily_loss {self.daily_dd:.1%}"
        if (
            getattr(cfg, "DRAWDOWN_HALT_ENABLED", True)
            and self.peak_drawdown >= getattr(cfg, "MAX_DRAWDOWN_PCT", 0.25)
        ):
            basis = "equity" if getattr(cfg, "DRAWDOWN_USE_EQUITY", True) else "trading"
            return True, (
                f"max_drawdown_{basis} {self.peak_drawdown:.1%}>={cfg.MAX_DRAWDOWN_PCT:.0%}"
            )

        if cfg.PER_ASSET_CIRCUIT_BREAKER and asset:
            streak = int(self.consec_losses_by_asset.get(asset, 0))
            halted_at = self._halted_at_by_asset.get(asset)
            if streak >= cfg.MAX_CONSEC_LOSSES:
                if halted_at is None:
                    self._halted_at_by_asset[asset] = time.time()
                    halted_at = self._halted_at_by_asset[asset]
                elapsed = time.time() - halted_at
                cooldown = cfg.COOLDOWN_MINUTES * 60
                if elapsed < cooldown:
                    remaining_min = (cooldown - elapsed) / 60
                    return True, f"consec_loss_cooldown({remaining_min:.0f}m left)"
                log.info(
                    f"Circuit breaker [{asset}] cooldown expired after {elapsed/60:.0f}m — resuming"
                )
                self.consec_losses_by_asset[asset] = 0
                self._halted_at_by_asset.pop(asset, None)
            return False, ""

        if self.consec_losses >= cfg.MAX_CONSEC_LOSSES:
            if self._halted_at is None:
                self._halted_at = time.time()
            if not cfg.DRY_RUN and getattr(cfg, "LIVE_BREAKER_MANUAL_RESUME", True):
                # Live: no timed resume. A cooldown would put real money back
                # into whatever regime produced the streak. Sticky via
                # live_halt_reason, cleared only by clear_breaker_halt().
                reason = (
                    f"consec_losses({self.consec_losses}>={cfg.MAX_CONSEC_LOSSES}) "
                    "— send /resume after reviewing"
                )
                if self.live_halt_reason != reason:
                    self.set_live_halt(reason)
                return True, reason
            elapsed = time.time() - self._halted_at
            cooldown = cfg.COOLDOWN_MINUTES * 60
            if elapsed < cooldown:
                remaining_min = (cooldown - elapsed) / 60
                return True, f"consec_loss_cooldown({remaining_min:.0f}m left)"
            log.info(f"Circuit breaker cooldown expired after {elapsed/60:.0f}m — resuming")
            self.consec_losses = 0
            self._halted_at = None

        return False, ""

    def clear_breaker_halt(self, source: str = "operator") -> str:
        """
        Operator reset of the consecutive-loss breaker. Returns a description of
        what was cleared, or "" if nothing was tripped.

        Only touches breaker state: a divergence or balance halt raised by
        LiveGuard stays put, because those are not the operator's to wave off.
        """
        cleared: list[str] = []
        if self.consec_losses:
            cleared.append(f"streak {self.consec_losses}")
        if self._halted_at is not None:
            cleared.append("global cooldown")
        benched = [a for a in self._halted_at_by_asset]
        if benched:
            cleared.append("benched " + ",".join(sorted(benched)))
        if (self.live_halt_reason or "").startswith("consec_losses"):
            cleared.append("live halt")
            self.live_halt_reason = ""

        self.consec_losses = 0
        self._halted_at = None
        self.consec_losses_by_asset = {}
        self._halted_at_by_asset = {}

        if cleared:
            log.warning("Circuit breaker reset by %s: %s", source, "; ".join(cleared))
        return "; ".join(cleared)

    def _maybe_reset_daily(self) -> None:
        """Reset daily loss counter at UTC midnight."""
        now_utc = datetime.now(timezone.utc)
        last_reset_day = datetime.fromtimestamp(self.daily_start_ts, tz=timezone.utc).date()
        if now_utc.date() > last_reset_day:
            log.info(f"New UTC day — resetting daily_start (was ${self.daily_start:.2f})")
            self.daily_start    = self.balance
            self.daily_start_ts = time.time()

    # ─── Trade recording ──────────────────────────────────────────────────────

    def can_trade(self, size_usd: float, edge: float, min_edge: float, asset: Optional[str] = None) -> Tuple[bool, str]:
        halted, reason = self.is_halted(asset)
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
        fees: float = 0.0,
        skip_balance_apply: bool = False,
        decision: Optional[dict] = None,
    ) -> float:
        """Record a closed trade. Returns P&L."""
        # Kalshi: each contract costs entry_price, pays $1 on win.
        # P&L = (exit_price - entry_price) × contracts - fees.
        fee_amt = max(0.0, float(fees or 0.0))
        pnl = (exit_ - entry) * contracts - fee_amt
        amount_usdc = round(float(entry) * int(contracts), 4)
        trade = {
            "ts":       datetime.now().isoformat(),
            "ticker":   ticker,
            "asset":    asset,
            "entry":    round(entry, 4),
            "exit":     round(exit_, 4),
            "contracts": contracts,
            "amount_usdc": amount_usdc,  # premium risked at entry
            "fees":     round(fee_amt, 4),
            "pnl":      round(pnl, 4),
            "strategy": strategy,
            "window_id": window_id,
            "price_to_beat": price_to_beat,
            "exit_spot": exit_spot,
            "side":     side,
            "live":     (not cfg.DRY_RUN),
        }
        # C7: attach the entry-time decision context so every closed trade can be
        # attributed offline without joining against kalshi_decisions.jsonl.
        if decision and getattr(cfg, "DECISION_SNAPSHOT_ENABLED", True):
            trade["decision"] = _decision_snapshot(decision)
        self.trades.append(trade)
        self.total_trades += 1
        # Live mode: bankroll is re-synced from Kalshi; don't double-apply local PnL.
        if not skip_balance_apply and cfg.DRY_RUN:
            self.balance += pnl
        elif not skip_balance_apply and not cfg.DRY_RUN:
            # Still apply locally then sync will overwrite from Kalshi shortly
            self.balance += pnl

        # Binary: exit 1.0 = win, exit 0.0 = loss (explicit check; pnl can have float quirks)
        won = exit_ >= 0.5
        if won:
            self.wins        += 1
            self.consec_losses = 0
            self._halted_at   = None
            self.consec_losses_by_asset[asset] = 0
            self._halted_at_by_asset.pop(asset, None)
        else:
            self.losses        += 1
            self.consec_losses += 1
            asset_streak = int(self.consec_losses_by_asset.get(asset, 0)) + 1
            self.consec_losses_by_asset[asset] = asset_streak
            if self.consec_losses >= cfg.MAX_CONSEC_LOSSES and self._halted_at is None:
                self._halted_at = time.time()
                manual = not cfg.DRY_RUN and getattr(cfg, "LIVE_BREAKER_MANUAL_RESUME", True)
                log.warning(
                    f"Circuit breaker: {self.consec_losses} consecutive losses — "
                    + ("entries stay halted until /resume" if manual
                       else f"cooldown {cfg.COOLDOWN_MINUTES:.0f}m")
                )
            if (
                cfg.PER_ASSET_CIRCUIT_BREAKER
                and asset_streak >= cfg.MAX_CONSEC_LOSSES
                and asset not in self._halted_at_by_asset
            ):
                self._halted_at_by_asset[asset] = time.time()
                log.warning(
                    f"Circuit breaker [{asset}]: {asset_streak} consecutive losses — "
                    f"cooldown {cfg.COOLDOWN_MINUTES:.0f}m"
                )

        ref = entry * contracts
        if ref > 0:
            self.returns_hist.append(pnl / ref)

        self.last_trade_ts = time.time()
        self._touch_peaks()

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

        # Auto profit-skim after each close (no-op unless vault auto is enabled)
        skimmed = self.maybe_auto_skim()
        if skimmed > 0:
            log.info(
                f"VAULT auto-skim ${skimmed:.2f} | trading=${self.balance:.2f} "
                f"vault=${self.vault_balance:.2f} equity=${self.total_equity:.2f}"
            )
        self._touch_peaks()

        pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
        log.info(f"{'WIN' if won else 'LOSS'} [{asset}] PnL=${pnl_str}  "
                 f"Balance=${self.balance:.2f}  WR={self.win_rate:.1%}  "
                 f"Streak={self.consec_losses}")

        # JSONL log (include window_id for grouping)
        try:
            Path(TRADE_LOG).parent.mkdir(parents=True, exist_ok=True)
            out = {
                **trade,
                "balance": self.balance,
                "vault": self.vault_balance,
                "equity": self.total_equity,
                "win_rate": self.win_rate,
            }
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

    def take_cash(self, amount: float, reason: str = "manual") -> float:
        """
        Move profit from trading balance into the vault.
        Only skims up to skimmable_profit (never dips below starting_balance).
        Returns dollars actually moved.
        """
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return 0.0
        amount = min(max(0.0, amount), self.skimmable_profit)
        if amount < 0.01:
            return 0.0

        self.balance -= amount
        self.vault_balance += amount
        # Skim must not invent trading drawdown vs an old peak
        self.peak_balance = max(self.balance, self.peak_balance - amount)
        # Equity unchanged → peak_equity unchanged; still refresh for safety
        self._touch_peaks()

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "amount": round(amount, 4),
            "reason": reason,
            "balance_after": round(self.balance, 4),
            "vault_after": round(self.vault_balance, 4),
            "equity_after": round(self.total_equity, 4),
            "peak_balance_after": round(self.peak_balance, 4),
            "peak_equity_after": round(self.peak_equity, 4),
        }
        try:
            from .vault import _append_skim
            _append_skim(event)
        except Exception:
            pass
        log.info(
            f"TAKE CASH ${amount:.2f} ({reason}) | "
            f"trading=${self.balance:.2f} vault=${self.vault_balance:.2f} "
            f"peak_bal=${self.peak_balance:.2f}"
        )
        return amount

    def maybe_auto_skim(self) -> float:
        """If auto vault is on and profit >= trigger, skim skim_amount once."""
        try:
            from .vault import load_vault_config
            vcfg = load_vault_config()
        except Exception:
            return 0.0
        if not vcfg.auto_enabled or vcfg.skim_amount <= 0:
            return 0.0
        if self.skimmable_profit < vcfg.profit_trigger:
            return 0.0
        return self.take_cash(vcfg.skim_amount, reason="auto")

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
                    "consec_losses_by_asset": self.consec_losses_by_asset,
                    "_halted_at_by_asset": self._halted_at_by_asset,
                    "var_95":           self.var_95,
                    "cvar_95":          self.cvar_95,
                    "vol_regime":       self.vol_regime,
                    "vault_balance":    self.vault_balance,
                    "peak_equity":      self.peak_equity,
                    "total_equity":     self.total_equity,
                    "last_trade_ts":    self.last_trade_ts,
                    "session_started_ts": self.session_started_ts,
                    "live_halt_reason": self.live_halt_reason,
                    "kalshi_available": self.kalshi_available,
                    "kalshi_portfolio_value": self.kalshi_portfolio_value,
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
            s.consec_losses_by_asset = d.get("consec_losses_by_asset", {})
            s._halted_at_by_asset = d.get("_halted_at_by_asset", {})
            s.var_95           = d.get("var_95",           0.0)
            s.cvar_95          = d.get("cvar_95",          0.0)
            s.vol_regime       = d.get("vol_regime",       "normal")
            s.vault_balance    = float(d.get("vault_balance", 0.0) or 0.0)
            s.peak_equity      = float(d.get("peak_equity", s.balance + s.vault_balance) or (s.balance + s.vault_balance))
            s.last_trade_ts    = float(d.get("last_trade_ts", 0.0) or 0.0)
            s.session_started_ts = float(d.get("session_started_ts", time.time()) or time.time())
            s.live_halt_reason = str(d.get("live_halt_reason") or "")
            s.kalshi_available = float(d.get("kalshi_available") or 0.0)
            s.kalshi_portfolio_value = float(d.get("kalshi_portfolio_value") or 0.0)
            s.returns_hist     = d.get("returns_hist",     [])
            s.trades           = d.get("trades",           [])
            # Restore per-asset stats
            for k, v in d.get("asset_stats", {}).items():
                if isinstance(v, dict):
                    s.asset_stats[k] = AssetStats(**v)
            s._touch_peaks()
            return s
        except FileNotFoundError:
            s = cls()
            s.daily_start_ts = time.time()
            s.session_started_ts = time.time()
            s.peak_equity = s.balance
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
        # Rebuild from trade log (preserve vault; trading book = start + pnl - vault)
        start = self.starting_balance
        vault = float(self.vault_balance or 0.0)
        wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
        losses = len(trades) - wins
        total_pnl = sum(float(t.get("pnl", 0)) for t in trades)
        self.total_trades = len(trades)
        self.wins = wins
        self.losses = losses
        self.balance = start + total_pnl - vault
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
