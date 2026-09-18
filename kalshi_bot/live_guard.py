"""
live_guard.py — Live-mode bankroll sync and divergence halt.

Paper mode never uses this. In live mode Kalshi available cash + portfolio
value are the source of truth; local sim is forced to follow.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from .config import cfg
from .kalshi_client import KalshiClient
from .sim_state import OpenPosition, SimState

log = logging.getLogger("kalshi_bot.live_guard")


class LiveGuard:
    def __init__(self, kalshi: KalshiClient, sim: SimState):
        self.kalshi = kalshi
        self.sim = sim
        self.last_sync_ts: float = 0.0
        self.last_available: float = 0.0
        self.last_portfolio: float = 0.0
        self.sync_failures: int = 0
        self.bootstrapped: bool = False

    def bootstrap(self) -> Tuple[bool, str]:
        """Call once at live start. Syncs bankroll; fails closed if API down."""
        if cfg.DRY_RUN:
            return True, "paper"
        ok, avail, port = self._fetch()
        if not ok:
            self.sim.set_live_halt("live_balance_unreachable_at_start")
            return False, "Cannot read Kalshi balance — refusing to start live"
        # A vault carried in from a paper sim file is not backed by real cash.
        # Only a vault built during this live book means anything.
        fresh_book = float(self.sim.total_trades or 0) == 0 and not self.sim.trades
        if fresh_book and float(self.sim.vault_balance or 0.0) > 0:
            log.warning(
                "LIVE GUARD: discarding $%.2f carried-over vault on a fresh live book",
                float(self.sim.vault_balance or 0.0),
            )
            self.sim.vault_balance = 0.0
        self._apply_sync(avail, port, open_positions=[])
        # Anchor starting capital to real account on a fresh live book
        if fresh_book:
            self.sim.starting_balance = float(avail + port)
            self.sim.daily_start = float(avail)
            self.sim.peak_balance = float(avail)
            self.sim.peak_equity = float(avail + port)
        self.bootstrapped = True
        self.sim.clear_live_halt()
        log.warning(
            "LIVE GUARD bootstrap: available=$%.2f portfolio=$%.2f total=$%.2f "
            "vault=$%.2f (sizing uses Kalshi available minus vault)",
            avail, port, avail + port, float(self.sim.vault_balance or 0.0),
        )
        return True, f"available=${avail:.2f} portfolio=${port:.2f}"

    def maybe_sync(self, open_positions: Optional[List[OpenPosition]] = None, force: bool = False) -> None:
        if cfg.DRY_RUN:
            return
        now = time.time()
        if not force and (now - self.last_sync_ts) < float(cfg.LIVE_BALANCE_SYNC_SECS):
            return
        ok, avail, port = self._fetch()
        if not ok:
            self.sync_failures += 1
            if self.sync_failures >= int(cfg.LIVE_SYNC_FAIL_HALT):
                self.sim.set_live_halt(
                    f"live_balance_sync_failed_x{self.sync_failures}"
                )
                log.error("LIVE GUARD: balance sync failed %s times — HALTING entries", self.sync_failures)
            return
        self.sync_failures = 0
        self._apply_sync(avail, port, open_positions or [])

    def _fetch(self) -> Tuple[bool, float, float]:
        try:
            shard = int(getattr(cfg, "CRYPTO_EXCHANGE_INDEX", 2))
            detail = self.kalshi.get_balance_detail(exchange_index=shard)
            avail = float(detail.get("available") or 0.0)
            port = float(detail.get("portfolio_value") or 0.0)
            # Sanity: empty dict / failed GET returns zeros with no key history
            raw = detail.get("raw") or {}
            if not raw:
                return False, 0.0, 0.0
            return True, avail, port
        except Exception as e:
            log.warning("LIVE GUARD fetch error: %s", e)
            return False, 0.0, 0.0

    def _apply_sync(
        self,
        available: float,
        portfolio: float,
        open_positions: List[OpenPosition],
    ) -> None:
        self.last_sync_ts = time.time()
        self.last_available = available

        # Kalshi has no sub-accounts: vaulted profit sits in the same cash balance.
        # Reserve it out of the sizing bankroll, or this sync hands skimmed cash
        # straight back to the trader and total_equity (balance + vault) counts it
        # twice — which also understates peak_drawdown and delays the DD breaker.
        # vault == 0 reproduces the pre-reservation behavior exactly.
        vault = max(0.0, float(self.sim.vault_balance or 0.0))
        if vault > available + portfolio:
            log.error(
                "LIVE GUARD: vault $%.2f exceeds account $%.2f — clamping "
                "(withdrawal to bank, or losses ate the vault)",
                vault, available + portfolio,
            )
            vault = max(0.0, available + portfolio)
            self.sim.vault_balance = vault
        tradeable = max(0.0, available - vault)

        local_open = float(sum(p.amount_usdc for p in open_positions))
        # Defense: if get_balance_detail ever hands us cents again, a $9.40
        # ticket looks like $940 and both divergence and peak_equity explode.
        if local_open > 1.0 and portfolio >= local_open * 20:
            log.error(
                "LIVE GUARD: portfolio $%.2f looks like cents vs local_open $%.2f — /100",
                portfolio, local_open,
            )
            portfolio = portfolio / 100.0

        # Divergence must compare premium-to-premium. portfolio_value is MTM and
        # will look like a missing fill whenever a lottery is marked down.
        kalshi_open = local_open
        if open_positions:
            try:
                shard = int(getattr(cfg, "CRYPTO_EXCHANGE_INDEX", 2))
                kalshi_open = float(self.kalshi.get_open_exposure_dollars(shard))
            except Exception as e:
                log.warning("LIVE GUARD exposure fetch failed: %s — skipping divergence", e)
                kalshi_open = local_open

        self.last_portfolio = portfolio

        # Sizing bankroll = withdrawable Kalshi cash the vault has no claim on
        self.sim.balance = tradeable
        self.sim.kalshi_available = float(available)
        self.sim.kalshi_portfolio_value = float(portfolio)

        true_eq = float(available) + float(portfolio)
        peak = float(self.sim.peak_equity or 0.0)
        fresh_book = float(self.sim.total_trades or 0) == 0 and not self.sim.trades
        if (
            fresh_book
            and peak > true_eq * 1.25
            and peak > true_eq + 25.0
        ):
            log.warning(
                "LIVE GUARD: clamping peak_equity $%.2f → $%.2f "
                "(no closed trades; mark was implausible)",
                peak, true_eq,
            )
            self.sim.peak_equity = max(true_eq, float(self.sim.starting_balance or true_eq))
            self.sim.peak_balance = max(float(self.sim.peak_balance or 0.0), available)

        self.sim._touch_peaks()

        # Premium paid vs Kalshi exposure (not MTM). Allow configured band.
        abs_lim = float(cfg.LIVE_DIVERGENCE_HALT_USD)
        pct_lim = float(cfg.LIVE_DIVERGENCE_HALT_PCT) * max(available + portfolio, local_open, 1.0)
        limit = max(abs_lim, pct_lim)
        gap = abs(local_open - kalshi_open)
        if open_positions and gap > limit:
            reason = (
                f"live_open_divergence local_open=${local_open:.2f} "
                f"kalshi_exposure=${kalshi_open:.2f} mtm=${portfolio:.2f} "
                f"gap=${gap:.2f}>${limit:.2f}"
            )
            self.sim.set_live_halt(reason)
            log.error("LIVE GUARD HALT: %s", reason)
        elif available + portfolio <= 0.009 and not open_positions:
            # Flat broke — stop trying to trade
            if available < float(cfg.LIVE_MIN_AVAILABLE_USD):
                self.sim.set_live_halt(f"live_available_too_low(${available:.2f})")
        elif vault > 0 and not open_positions and tradeable < float(cfg.LIVE_MIN_AVAILABLE_USD):
            # Cash exists but the vault owns it. Halt loudly instead of spinning on
            # min-size rejections. Self-clears if the vault is released.
            self.sim.set_live_halt(
                f"live_vault_locked(tradeable=${tradeable:.2f} vault=${vault:.2f})"
            )
        else:
            # Clear divergence-style halts when healthy again (keep explicit operator halts)
            lr = (self.sim.live_halt_reason or "")
            if (
                lr.startswith("live_open_divergence")
                or lr.startswith("live_available_too_low")
                or lr.startswith("live_vault_locked")
            ):
                if tradeable >= float(cfg.LIVE_MIN_AVAILABLE_USD) and gap <= limit:
                    self.sim.clear_live_halt()

        log.info(
            "LIVE SYNC available=$%.2f portfolio=$%.2f vault=$%.2f tradeable=$%.2f "
            "local_open=$%.2f gap=$%.2f",
            available, portfolio, vault, tradeable,
            local_open, gap if open_positions else 0.0,
        )
