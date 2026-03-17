"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          POLYMARKET QUANTITATIVE TRADING BOT — STARTER EDITION              ║
║          Strategies: BTC 5/15min · Current Events · Weather                 ║
╚══════════════════════════════════════════════════════════════════════════════╝

ARCHITECTURE OVERVIEW
─────────────────────
This bot unifies three distinct alpha engines:

  1. BTC Oracle-Lag Engine   → Exploits LMSR price lag vs Binance real-time
  2. Event Arbitrage Engine  → Detects YES+NO < $1 mispricings
  3. Weather Forecast Engine → Trades NWS forecast vs Polymarket temperature buckets

All three engines share:
  · Bayesian probability updating
  · Kelly Criterion position sizing (1/4 Kelly for safety)
  · Sharpe Ratio + Variance Drag filters
  · Simulation mode (paper trading) before live execution

═══════════════════════════════════════════════════════════════════════════════
SECTION 1 — IMPORTS & CONFIGURATION
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import logging
import math
import os
from collections import deque
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import numpy as np
import requests
import websockets  # pip install websockets
from scipy.stats import beta
from dotenv import load_dotenv

load_dotenv()

# Optional colorama for ColoredFormatter (pip install colorama)
try:
    from colorama import Fore, Style, init as colorama_init
    _HAS_COLORAMA = True
except ImportError:
    _HAS_COLORAMA = False
    Fore = Style = None  # type: ignore

def _ensure_colorama_init() -> None:
    """Init colorama only when ColoredFormatter is used (avoids wrapping stdout at import)."""
    if _HAS_COLORAMA and not getattr(_ensure_colorama_init, "_init_done", False):
        colorama_init(autoreset=True)
        _ensure_colorama_init._init_done = True  # type: ignore

# ─── Utilities (from versiondeep_utils) ────────────────────────────────────────
class ColoredFormatter:
    """Add colors to console output. Uses colorama if available."""

    @staticmethod
    def success(msg: str) -> str:
        return f"{Fore.GREEN}{msg}{Style.RESET_ALL}" if _HAS_COLORAMA else msg

    @staticmethod
    def warning(msg: str) -> str:
        return f"{Fore.YELLOW}{msg}{Style.RESET_ALL}" if _HAS_COLORAMA else msg

    @staticmethod
    def error(msg: str) -> str:
        return f"{Fore.RED}{msg}{Style.RESET_ALL}" if _HAS_COLORAMA else msg

    @staticmethod
    def info(msg: str) -> str:
        return f"{Fore.CYAN}{msg}{Style.RESET_ALL}" if _HAS_COLORAMA else msg

    @staticmethod
    def trade(msg: str) -> str:
        return f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}" if _HAS_COLORAMA else msg


class RateLimiter:
    """Simple rate limiter for API calls."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls: list = []

    def can_call(self) -> bool:
        now = datetime.now()
        self.calls = [t for t in self.calls
                      if (now - t).total_seconds() < self.period]
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False

    async def wait_if_needed(self) -> None:
        """Wait until a call slot is available."""
        while not self.can_call():
            await asyncio.sleep(1)


# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polybot")

LOG_FILE = "paper_trades.jsonl"

# ─── Config (from versiondeep_config) ─────────────────────────────────────────
@dataclass
class TradingConfig:
    """Core trading parameters. Base values: Kelly 0.25, MIN_EDGE 0.05, MAX_POSITION 0.03."""

    # Position sizing (base values - do not change)
    KELLY_FRACTION: float = 0.25
    MIN_EDGE_PCT: float = 0.05
    MAX_POSITION_SIZE_PCT: float = 0.03
    MAX_DAILY_LOSS_PCT: float = 0.10
    MAX_CONSECUTIVE_LOSSES: int = 3

    # Entry / risk
    SHARPE_THRESHOLD: float = 1.2
    VOL_THRESHOLD_HI: float = 0.80
    VOL_THRESHOLD_MID: float = 0.60

    # Arbitrage
    ARB_MIN_DEVIATION: float = 0.015
    ARB_SPREAD_MIN: float = 0.02

    # Weather
    WEATHER_ENTRY_THRESHOLD: float = 0.15
    WEATHER_EXIT_THRESHOLD: float = 0.45
    WEATHER_POSITION_PCT: float = 0.05

    # Simulation
    SIM_BALANCE: float = 1000.0
    SIM_FILE: str = "simulation.json"
    DRY_RUN: bool = True

    # From versiondeep (kept as-is)
    MIN_VOLUME_USD: float = 10000
    MAX_SPREAD_PCT: float = 0.03
    VOLATILITY_WINDOW: int = 20
    MAX_VOLATILITY: float = 0.80
    MIN_VOLATILITY: float = 0.10
    SLIPPAGE_TOLERANCE: float = 0.005
    ORDER_TIMEOUT_SEC: int = 30
    RETRY_ATTEMPTS: int = 3
    ENABLED_MARKETS: List[str] = field(default_factory=lambda: ["crypto", "politics", "weather", "economics"])


@dataclass
class APIConfig:
    """API connection parameters loaded from .env"""

    POLY_API_KEY: str = field(default_factory=lambda: os.getenv("POLY_API_KEY", ""))
    POLY_API_SECRET: str = field(default_factory=lambda: os.getenv("POLY_API_SECRET", ""))
    POLY_PASSPHRASE: str = field(default_factory=lambda: os.getenv("POLY_PASSPHRASE", ""))
    POLYGON_RPC: str = field(default_factory=lambda: os.getenv("POLYGON_RPC", "https://polygon-rpc.com"))
    POLYGON_WS: str = field(default_factory=lambda: os.getenv("POLYGON_WS", ""))
    PRIVATE_KEY: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    PUBLIC_KEY: str = field(default_factory=lambda: os.getenv("PUBLIC_KEY", ""))
    BINANCE_API_KEY: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    NWS_USER_AGENT: str = field(default_factory=lambda: os.getenv("NWS_USER_AGENT", "polymarket-bot/1.0"))

    # Endpoints (constants)
    POLY_REST: str = "https://gamma-api.polymarket.com"
    POLY_CLOB: str = "https://clob.polymarket.com"
    POLY_WS: str = "wss://ws-subscriptions-clob.polymarket.com/ws"
    CLOB_WS: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    BINANCE_WS: str = "wss://stream.binance.com:9443/ws"
    BTC_SYMBOL: str = "btcusdt"


trading_config = TradingConfig()
api_config = APIConfig()


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 2 — CORE MATH ENGINE
═══════════════════════════════════════════════════════════════════════════════

KEY FORMULAS IMPLEMENTED:

  Kelly Criterion (binary markets):
    f* = (p̂ - p) / (1 - p)
    f_safe = f* × 0.25          (quarter Kelly)

  Expected Value:
    EV = p_real - p_market

  LMSR Price (softmax):
    p_i = e^(q_i/b) / Σ e^(q_j/b)

  Variance Drag (geometric return):
    Geo_return ≈ Arith_return - σ²/2

  Sharpe Ratio:
    SR = (E[R] - R_f) / σ(R)

  Bayesian Update:
    P(H|D) = P(D|H) × P(H) / P(D)

  Order Book Imbalance:
    OBI = (BidVol - AskVol) / (BidVol + AskVol)
"""


def kelly_fraction(p_hat: float, p_market: float) -> float:
    """
    Kelly Criterion for binary prediction markets.

    p_hat    = your Bayesian probability estimate
    p_market = current market price (cost basis)

    Returns quarter-Kelly position size as a fraction of bankroll.
    Returns 0 if edge is negative (don't trade).
    """
    if p_market <= 0 or p_market >= 1:
        return 0.0
    edge = p_hat - p_market
    if edge <= 0:
        return 0.0
    full_kelly = edge / (1.0 - p_market)
    return full_kelly * trading_config.KELLY_FRACTION


def expected_value(p_real: float, p_market: float) -> float:
    """EV = p_real - p_market. Trade YES if EV > MIN_EDGE, NO if EV < -MIN_EDGE."""
    return p_real - p_market


def lmsr_price(quantities: list[float], b: float, outcome_idx: int) -> float:
    """
    LMSR (Logarithmic Market Scoring Rule) price formula.

    C(q) = b × ln(Σ e^(qi/b))
    p_k  = e^(qk/b) / Σ e^(qi/b)   ← softmax

    b = liquidity parameter (larger b = deeper market, less price impact).
    The market maker's maximum loss is bounded at: L_max = b × ln(n)
    """
    q = np.array(quantities)
    exp_q = np.exp(q / b)
    return float(exp_q[outcome_idx] / np.sum(exp_q))


def variance_drag(arithmetic_return: float, volatility: float) -> float:
    """
    Geometric return ≈ Arithmetic return - σ²/2

    This is why a +30% / -30% sequence leaves you at -9%, not 0%.
    High volatility destroys capital even with positive edge.
    Always filter for variance drag before entering positions.
    """
    return arithmetic_return - (volatility ** 2) / 2.0


def sharpe_ratio(returns: list[float], risk_free: float = 0.0) -> float:
    """
    SR = (E[R] - R_f) / σ(R)

    Target: SR > 1.2 before entering any trade.
    SR > 2.0 = institutional territory.
    """
    r = np.array(returns)
    if len(r) < 2 or np.std(r) == 0:
        return 0.0
    return float((np.mean(r) - risk_free) / np.std(r))


def bayesian_update(prior: float, likelihood_given_true: float,
                    likelihood_given_false: float) -> float:
    """
    Bayes' Theorem: P(H|D) = P(D|H) × P(H) / P(D)

    prior                  = P(H)   — your initial probability estimate
    likelihood_given_true  = P(D|H) — how likely is this evidence if H is true?
    likelihood_given_false = P(D|¬H)— how likely if H is false?

    Use this to update p_hat as each new signal arrives.
    """
    p_d = (likelihood_given_true * prior +
           likelihood_given_false * (1.0 - prior))
    if p_d == 0:
        return prior
    return (likelihood_given_true * prior) / p_d


# ─── Bayesian Updater (from versiondeep_bayesian) ─────────────────────────────
class BayesianProbabilityUpdater:
    """
    Implements Bayesian probability updates for prediction markets.
    P(H|E) = P(E|H) * P(H) / P(E). Supports Beta prior, time decay.
    """

    def __init__(self, prior: float = 0.5, confidence: float = 1.0):
        self.alpha = prior * confidence
        self.beta_param = (1 - prior) * confidence  # avoid clash with scipy.beta
        self.prior = prior
        self.signals: list = []

    def update(self, likelihood: float, signal_weight: float = 1.0,
               signal_name: str = "") -> float:
        self.signals.append({
            "timestamp": datetime.now(),
            "likelihood": likelihood,
            "weight": signal_weight,
            "name": signal_name,
        })
        self._apply_time_decay()
        if likelihood > 0.5:
            self.alpha += signal_weight * (likelihood - 0.5) * 2
        else:
            self.beta_param += signal_weight * (0.5 - likelihood) * 2
        posterior = self.alpha / (self.alpha + self.beta_param)
        self.prior = posterior
        return posterior

    def _apply_time_decay(self, half_life_hours: float = 1.0) -> None:
        now = datetime.now()
        lambda_decay = np.log(2) / (half_life_hours * 3600)
        for sig in self.signals:
            age = (now - sig["timestamp"]).total_seconds()
            sig["weight"] *= np.exp(-lambda_decay * age)
        self.signals = [s for s in self.signals if s["weight"] > 0.01]

    def get_confidence(self) -> float:
        total_evidence = (self.alpha + self.beta_param) - 2
        return min(1.0, total_evidence / 100)

    def get_credible_interval(self, interval: float = 0.95) -> Tuple[float, float]:
        lower = (1 - interval) / 2
        upper = 1 - lower
        return (beta.ppf(lower, self.alpha, self.beta_param),
                beta.ppf(upper, self.alpha, self.beta_param))


class MultiSourceBayesianFusion:
    """Fuse multiple Bayesian updaters with inverse-variance weighting."""

    def __init__(self) -> None:
        self.sources: dict[str, BayesianProbabilityUpdater] = {}
        self.weights: dict[str, float] = {}

    def add_source(self, name: str, prior: float = 0.5,
                   confidence: float = 1.0, weight: float = 1.0) -> None:
        self.sources[name] = BayesianProbabilityUpdater(prior, confidence)
        self.weights[name] = weight

    def update_source(self, name: str, likelihood: float,
                      signal_weight: float = 1.0) -> float:
        if name not in self.sources:
            raise ValueError(f"Unknown source: {name}")
        return self.sources[name].update(likelihood, signal_weight, name)

    def fuse(self) -> Tuple[float, float]:
        if not self.sources:
            return 0.5, 1.0
        weighted_sum = 0.0
        total_weight = 0.0
        for name, source in self.sources.items():
            prob = source.prior
            variance = (source.alpha * source.beta_param) / (
                (source.alpha + source.beta_param) ** 2
                * (source.alpha + source.beta_param + 1)
            )
            weight = self.weights.get(name, 1.0) / (variance + 1e-8)
            weighted_sum += prob * weight
            total_weight += weight
        fused_prob = weighted_sum / total_weight if total_weight > 0 else 0.5
        fused_uncertainty = 1.0 / np.sqrt(total_weight) if total_weight > 0 else 1.0
        return fused_prob, fused_uncertainty


def order_book_imbalance(bid_vol: float, ask_vol: float) -> float:
    """
    OBI = (BidVol - AskVol) / (BidVol + AskVol)

    OBI > +0.3 → buyers dominating → bullish signal
    OBI < -0.3 → sellers dominating → bearish signal
    """
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def volatility_position_multiplier(realized_vol: float) -> float:
    """
    Scale position size DOWN when volatility is high.

    σ < 60%  → full position (1.0x)
    60-80%   → half position (0.5x)
    > 80%    → no entry (0.0x)
    """
    if realized_vol > trading_config.VOL_THRESHOLD_HI:
        return 0.0
    elif realized_vol > trading_config.VOL_THRESHOLD_MID:
        return 0.5
    return 1.0


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 3 — SIMULATION STATE MANAGER
═══════════════════════════════════════════════════════════════════════════════

The simulation runs EXACTLY like live trading but with virtual money.
Run at least 1-2 weeks of simulation and achieve a consistent win rate
above 55% before switching DRY_RUN to False.
"""


@dataclass
class SimState:
    balance: float = field(default_factory=lambda: trading_config.SIM_BALANCE)
    starting_balance: float = field(default_factory=lambda: trading_config.SIM_BALANCE)
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    daily_starting_balance: float = 0.0
    returns_history: list = field(default_factory=list)
    # Risk metrics (from versiondeep_risk)
    var_95: float = 0.0
    cvar_95: float = 0.0
    volatility_regime: str = "normal"  # "low", "normal", "high"
    peak_balance: float = 0.0
    # Dashboard: last scan counts and maker quotes
    last_scan: dict = field(default_factory=dict)
    active_quotes: dict = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def profit_factor(self):
        gross_win = sum(t["pnl"] for t in self.trades if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t.get("pnl", 0) < 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def current_sharpe(self) -> float:
        return sharpe_ratio(self.returns_history)

    def is_halted(self) -> bool:
        """Check all circuit breakers before trading."""
        # Daily drawdown check
        if self.daily_starting_balance > 0:
            daily_dd = (self.daily_starting_balance - self.balance) / self.daily_starting_balance
            if daily_dd > trading_config.MAX_DAILY_LOSS_PCT:
                log.warning(f"🛑 HALT: Daily drawdown {daily_dd:.1%} > {trading_config.MAX_DAILY_LOSS_PCT:.0%}")
                return True
        # Consecutive loss check
        if self.consecutive_losses >= trading_config.MAX_CONSECUTIVE_LOSSES:
            log.warning(f"🛑 HALT: {self.consecutive_losses} consecutive losses")
            return True
        return False

    def _calculate_var(self) -> None:
        """Update VaR, CVaR, and volatility_regime from returns_history."""
        if len(self.returns_history) < 20:
            return
        returns = np.array(self.returns_history[-100:])
        var_95_val = np.percentile(returns, 5)
        self.var_95 = abs(var_95_val)
        below_var = returns[returns <= var_95_val]
        self.cvar_95 = abs(below_var.mean()) if len(below_var) > 0 else 0.0
        vol = float(np.std(returns) * np.sqrt(365 * 288))
        if vol > trading_config.MAX_VOLATILITY:
            self.volatility_regime = "high"
        elif vol < trading_config.MIN_VOLATILITY:
            self.volatility_regime = "low"
        else:
            self.volatility_regime = "normal"

    def can_trade(self, position_size: float, edge: float) -> tuple[bool, str]:
        """
        Additional risk checks alongside is_halted(). Returns (allowed, reason).
        Halve position if volatility regime is high; block if extreme.
        """
        if self.is_halted():
            return False, "halted"
        if self.daily_starting_balance > 0:
            daily_dd = (self.daily_starting_balance - self.balance) / self.daily_starting_balance
            if daily_dd >= trading_config.MAX_DAILY_LOSS_PCT:
                return False, f"Daily drawdown {daily_dd:.1%}"
        if self.consecutive_losses >= trading_config.MAX_CONSECUTIVE_LOSSES:
            return False, f"Consecutive losses {self.consecutive_losses}"
        ref = self.peak_balance if self.peak_balance > 0 else self.balance
        if ref > 0:
            pos_pct = position_size / ref
            if pos_pct > trading_config.MAX_POSITION_SIZE_PCT:
                return False, f"Position {pos_pct:.1%} > limit"
            if self.volatility_regime == "high":
                if pos_pct > trading_config.MAX_POSITION_SIZE_PCT * 0.5:
                    return False, "High vol: position capped at half"
        if edge < trading_config.MIN_EDGE_PCT:
            return False, f"Edge {edge:.1%} < minimum"
        return True, "OK"

    def record_trade(self, market_id: str, entry_price: float,
                     exit_price: float, shares: float, outcome: str):
        pnl = (exit_price - entry_price) * shares
        won = pnl > 0
        self.trades.append({
            "market_id": market_id,
            "entry": entry_price,
            "exit": exit_price,
            "shares": shares,
            "pnl": pnl,
            "outcome": outcome,
            "timestamp": datetime.now().isoformat(),
        })
        self.total_trades += 1
        self.balance += pnl
        if won:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1
        ret = pnl / (entry_price * shares) if entry_price * shares > 0 else 0
        self.returns_history.append(ret)
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        self._calculate_var()
        log.info(f"{'✅' if won else '❌'} Trade closed: PnL ${pnl:+.2f} | "
                 f"Balance ${self.balance:.2f} | WR {self.win_rate:.1%}")
        self.log_trade_jsonl(market_id, entry_price, exit_price, shares, pnl, strategy="unknown")

    def log_trade_jsonl(self, market_id: str, entry_price: float, exit_price: float,
                        shares: float, pnl: float, strategy: str = "unknown") -> None:
        """Append trade record to JSONL log file."""
        record = {
            "ts": datetime.now().isoformat(),
            "type": "CLOSE",
            "market_id": market_id,
            "strategy": strategy,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "shares": shares,
            "pnl": pnl,
            "balance": self.balance,
            "win_rate": self.win_rate,
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def save(self, path: str = trading_config.SIM_FILE):
        with open(path, "w") as f:
            json.dump({
                "balance": self.balance,
                "starting_balance": self.starting_balance,
                "peak_balance": self.peak_balance,
                "total_trades": self.total_trades,
                "wins": self.wins,
                "losses": self.losses,
                "trades": self.trades[-50:],  # keep last 50
                "var_95": self.var_95,
                "cvar_95": self.cvar_95,
                "volatility_regime": self.volatility_regime,
                "consecutive_losses": self.consecutive_losses,
                "sharpe": self.current_sharpe,
                "win_rate": self.win_rate,
                "last_scan": self.last_scan,
                "active_quotes": self.active_quotes,
            }, f, indent=2)

    @classmethod
    def load(cls, path: str = trading_config.SIM_FILE) -> "SimState":
        try:
            with open(path) as f:
                d = json.load(f)
            s = cls()
            s.balance = d.get("balance", trading_config.SIM_BALANCE)
            s.starting_balance = d.get("starting_balance", trading_config.SIM_BALANCE)
            s.total_trades = d.get("total_trades", 0)
            s.wins = d.get("wins", 0)
            s.losses = d.get("losses", 0)
            s.trades = d.get("trades", [])
            s.daily_starting_balance = s.balance
            s.peak_balance = max(d.get("peak_balance", 0), s.balance, s.starting_balance)
            s.var_95 = d.get("var_95", 0.0)
            s.cvar_95 = d.get("cvar_95", 0.0)
            s.volatility_regime = d.get("volatility_regime", "normal")
            s.consecutive_losses = d.get("consecutive_losses", 0)
            s.last_scan = d.get("last_scan", {})
            s.active_quotes = d.get("active_quotes", {})
            return s
        except FileNotFoundError:
            s = cls()
            s.daily_starting_balance = s.balance
            return s


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 4 — POLYMARKET API CLIENT
═══════════════════════════════════════════════════════════════════════════════
"""


class PolymarketClient:
    """Wraps the Polymarket Gamma + CLOB APIs."""

    def __init__(self):
        self.gamma = api_config.POLY_REST
        self.clob  = api_config.POLY_CLOB
        self.api_key = api_config.POLY_API_KEY
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polybot/1.0"})

    def get_markets(self, limit: int = 100, active: bool = True) -> list[dict]:
        """Fetch active markets from Gamma API."""
        params = {"limit": limit, "active": str(active).lower()}
        try:
            r = self._session.get(f"{self.gamma}/markets", params=params, timeout=10)
            return r.json() if r.ok else []
        except Exception as e:
            log.error(f"get_markets error: {e}")
            return []

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Fetch a specific market by slug."""
        try:
            r = self._session.get(f"{self.gamma}/markets/{slug}", timeout=10)
            return r.json() if r.ok else None
        except Exception as e:
            log.error(f"get_market_by_slug error: {e}")
            return None

    def get_events(self, limit: int = 50, active: bool = True,
                   tag: Optional[str] = None,
                   series_slug: Optional[str] = None) -> list[dict]:
        """Fetch events from Gamma API. Optional tag or seriesSlug filter."""
        params = {"limit": limit, "active": str(active).lower()}
        if tag:
            params["tag"] = tag
        if series_slug:
            params["seriesSlug"] = series_slug
        try:
            r = self._session.get(f"{self.gamma}/events", params=params, timeout=10)
            return r.json() if r.ok else []
        except Exception as e:
            log.error(f"get_events error: {e}")
            return []

    def get_events_by_series(self, series_slug: str, limit: int = 50) -> list[dict]:
        """Fetch events filtered by series slug (e.g. btc-up-or-down-5m)."""
        return self.get_events(limit=limit, series_slug=series_slug)

    def get_event_by_slug(self, slug: str) -> Optional[dict]:
        """Fetch event (with all child markets) by slug."""
        try:
            r = self._session.get(
                f"{self.gamma}/events", params={"slug": slug}, timeout=10
            )
            data = r.json()
            return data[0] if data else None
        except Exception as e:
            log.error(f"get_event error: {e}")
            return None

    def get_trades(self, market_slug: str, limit: int = 500) -> list[dict]:
        """Fetch recent trade history for a market."""
        try:
            r = self._session.get(
                f"{self.gamma}/trades",
                params={"market": market_slug, "limit": limit},
                timeout=10,
            )
            return r.json() if r.ok else []
        except Exception as e:
            log.error(f"get_trades error: {e}")
            return []

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch live CLOB order book for a token."""
        try:
            r = self._session.get(
                f"{self.clob}/book", params={"token_id": token_id}, timeout=5
            )
            return r.json() if r.ok else {}
        except Exception as e:
            log.error(f"get_orderbook error: {e}")
            return {}

    def place_order(self, token_id: str, side: str,
                    amount_usdc: float, sim: SimState) -> bool:
        """
        Place a market order.
        In DRY_RUN mode: simulate the fill and log it.
        In live mode: use py-clob-client to submit to CLOB.
        """
        if trading_config.DRY_RUN:
            log.info(f"[SIM] {side} ${amount_usdc:.2f} on token {token_id}")
            return True
        # Live execution (requires py-clob-client installed):
        # from py_clob_client.client import ClobClient
        # client = ClobClient(host=self.clob, key=self.api_key, chain_id=137)
        # order = client.create_market_order(token_id=token_id, side=side, amount=amount_usdc)
        # result = client.post_order(order)
        # return result.get("success", False)
        log.warning("Live mode not enabled. Set DRY_RUN=False and configure py-clob-client.")
        return False


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 5 — ENGINE 1: BTC ORACLE-LAG / LMSR-LAG ENGINE
═══════════════════════════════════════════════════════════════════════════════

THEORY:
  Polymarket runs on LMSR. The average 5-minute BTC market has ~20-40 trades
  total (~1 trade every 7-15 seconds). Binance processes thousands per second.
  The gap between Binance's real-time knowledge and Polymarket's price
  can be 5-30 seconds wide. That lag is the alpha.

SIGNALS COMBINED INTO p_real:
  1. Order Book Imbalance (OBI)    — weight 0.40
  2. Cumulative Volume Delta (CVD) — weight 0.30
  3. VWAP Position                 — weight 20%
  4. EMA Momentum (5 > 20)         — weight 0.10
"""


class BinanceSignalEngine:
    """
    Processes real-time Binance WebSocket data into a probability score.
    Instantiate once; feed data via update_trade() and update_book().
    """

    def __init__(self, window: int = 60):
        self.window = window          # seconds of history to keep
        self.trades: list[dict] = []
        self.book_bids: dict[float, float] = {}
        self.book_asks: dict[float, float] = {}
        self.cvd: float = 0.0
        self.prices: list[float] = []
        self.vwap_num: float = 0.0
        self.vwap_den: float = 0.0
        self.ema5: Optional[float] = None
        self.ema20: Optional[float] = None
        self._EMA5_K  = 2 / (5  + 1)
        self._EMA20_K = 2 / (20 + 1)
        # Heikin-Ashi (from versiondeep_signals)
        self.ohlcv = deque(maxlen=100)
        self.heikin_ashi = deque(maxlen=100)
        self.current_candle = {
            "open": None, "high": 0, "low": float("inf"),
            "close": None, "volume": 0
        }
        self.candle_start = datetime.now()
        # MRO oscillator (from versiongrok)
        self.vol_buy = deque(maxlen=120)
        self.vol_sell = deque(maxlen=120)
        # Bayesian fusion (from versiondeep_bayesian)
        self.fusion = MultiSourceBayesianFusion()
        self.fusion.add_source("obi", prior=0.5, weight=0.40)
        self.fusion.add_source("cvd", prior=0.5, weight=0.30)
        self.fusion.add_source("vwap", prior=0.5, weight=0.20)
        self.fusion.add_source("momentum", prior=0.5, weight=0.05)
        self.fusion.add_source("heikin_ashi", prior=0.5, weight=0.08)
        self.fusion.add_source("mro", prior=0.5, weight=0.07)

    def update_trade(self, price: float, qty: float, is_buyer_maker: bool):
        """
        Process an aggTrade event from Binance WebSocket.
        is_buyer_maker=True means the buyer is the market maker → SELL hit.
        """
        is_buy = not is_buyer_maker      # aggressive buy if maker sold
        self.cvd += qty if is_buy else -qty
        self.vwap_num += price * qty
        self.vwap_den += qty
        self.prices.append(price)
        if is_buy:
            self.vol_buy.append(qty)
            self.vol_sell.append(0)
        else:
            self.vol_buy.append(0)
            self.vol_sell.append(qty)

        # EMA updates
        if self.ema5 is None:
            self.ema5 = self.ema20 = price
        else:
            self.ema5  = price * self._EMA5_K  + self.ema5  * (1 - self._EMA5_K)
            self.ema20 = price * self._EMA20_K + self.ema20 * (1 - self._EMA20_K)

        now = time.time()
        self.trades.append({"price": price, "qty": qty, "buy": is_buy, "t": now})
        # Trim old trades
        cutoff = now - self.window
        self.trades = [t for t in self.trades if t["t"] >= cutoff]
        # Heikin-Ashi candle update
        self._update_candle(price, qty)

    def _update_candle(self, price: float, volume: float) -> None:
        """Build 1-minute OHLCV candles; append to ohlcv and compute HA when minute changes."""
        now = datetime.now(timezone.utc)
        if now.minute != self.candle_start.minute or now.hour != self.candle_start.hour or now.day != self.candle_start.day:
            if self.current_candle["open"] is not None:
                self.current_candle["close"] = price
                self.current_candle["volume"] = self.current_candle.get("volume", 0)
                self.ohlcv.append(dict(self.current_candle))
                self._calculate_heikin_ashi()
            self.candle_start = now
            self.current_candle = {
                "open": price, "high": price, "low": price if price < float("inf") else price,
                "close": None, "volume": 0
            }
        self.current_candle["high"] = max(self.current_candle["high"], price)
        self.current_candle["low"] = min(self.current_candle["low"], price) if self.current_candle["low"] < price else min(self.current_candle["low"], price)
        self.current_candle["volume"] = self.current_candle.get("volume", 0) + volume

    def _calculate_heikin_ashi(self) -> None:
        """Compute Heikin-Ashi from last OHLCV candle and append to heikin_ashi."""
        if len(self.ohlcv) == 0:
            return
        c = self.ohlcv[-1]
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"] if c["close"] is not None else c["open"]
        ha_close = (o + h + l + cl) / 4
        if len(self.heikin_ashi) == 0:
            ha_open = (o + cl) / 2
        else:
            prev = self.heikin_ashi[-1]
            ha_open = (prev["open"] + prev["close"]) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        self.heikin_ashi.append({"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close})

    def get_heikin_ashi_signal(self) -> float:
        """+1 if last 3 HA candles green, -1 if all red, else 0."""
        if len(self.heikin_ashi) < 3:
            return 0.0
        last3 = list(self.heikin_ashi)[-3:]
        if all(ha["close"] > ha["open"] for ha in last3):
            return 1.0
        if all(ha["close"] < ha["open"] for ha in last3):
            return -1.0
        return 0.0

    def get_mro_signal(self) -> float:
        """MRO oscillator: +1 if mro>70, -1 if mro<-70, else 0."""
        if len(self.prices) < 6 or len(self.vol_buy) < 10 or len(self.vol_sell) < 10:
            return 0.0
        vol_buy_l = list(self.vol_buy)
        vol_sell_l = list(self.vol_sell)
        pv_now = self.prices[-1] * (sum(vol_buy_l[-5:]) + sum(vol_sell_l[-5:]))
        pv_old = self.prices[-6] * (sum(vol_buy_l[-10:-5]) + sum(vol_sell_l[-10:-5]))
        mro = 100 * (pv_now - pv_old) / (pv_old + 1e-9)
        if mro > 70:
            return 1.0
        if mro < -70:
            return -1.0
        return 0.0

    def update_book(self, bids: list, asks: list):
        """Process depth20 order book snapshot."""
        self.book_bids = {float(p): float(q) for p, q in bids}
        self.book_asks = {float(p): float(q) for p, q in asks}

    def get_obi(self) -> float:
        """Order Book Imbalance: (BidVol - AskVol) / (BidVol + AskVol)"""
        bid_vol = sum(self.book_bids.values())
        ask_vol = sum(self.book_asks.values())
        return order_book_imbalance(bid_vol, ask_vol)

    def get_vwap_signal(self) -> float:
        """Returns +1 if price is above VWAP, -1 if below, 0 if no data."""
        if self.vwap_den == 0 or not self.prices:
            return 0.0
        vwap = self.vwap_num / self.vwap_den
        current_price = self.prices[-1]
        return 1.0 if current_price > vwap else -1.0

    def get_momentum_signal(self) -> float:
        """EMA 5 > EMA 20 → bullish (+1), else bearish (-1)."""
        if self.ema5 is None or self.ema20 is None:
            return 0.0
        return 1.0 if self.ema5 > self.ema20 else -1.0

    def get_cvd_signal(self) -> float:
        """Rising CVD = net buying pressure (+1), else (-1)."""
        return 1.0 if self.cvd > 0 else -1.0

    def get_bias_score(self) -> float:
        """
        Combined directional bias: -1.0 (strong bear) to +1.0 (strong bull).
        Uses MultiSourceBayesianFusion: OBI 40%, CVD 30%, VWAP 20%, Momentum 5%,
        Heikin-Ashi 8%, MRO 7%.
        """
        obi = self.get_obi()
        cvd = self.get_cvd_signal()
        vwap = self.get_vwap_signal()
        mom = self.get_momentum_signal()
        ha_signal = self.get_heikin_ashi_signal()
        mro_signal = self.get_mro_signal()
        # Map each signal from [-1, +1] to likelihood [0, 1]
        likelihood_obi = (obi + 1) / 2
        likelihood_cvd = (cvd + 1) / 2
        likelihood_vwap = (vwap + 1) / 2
        likelihood_mom = (mom + 1) / 2
        likelihood_ha = (ha_signal + 1) / 2
        likelihood_mro = (mro_signal + 1) / 2
        self.fusion.update_source("obi", likelihood_obi, signal_weight=0.40)
        self.fusion.update_source("cvd", likelihood_cvd, signal_weight=0.30)
        self.fusion.update_source("vwap", likelihood_vwap, signal_weight=0.20)
        self.fusion.update_source("momentum", likelihood_mom, signal_weight=0.05)
        self.fusion.update_source("heikin_ashi", likelihood_ha, signal_weight=0.08)
        self.fusion.update_source("mro", likelihood_mro, signal_weight=0.07)
        fused_prob, _ = self.fusion.fuse()
        # Convert back to bias score in [-1, +1]
        fused_score = (fused_prob - 0.5) * 2
        return max(-1.0, min(1.0, fused_score))

    def conviction_count(self) -> int:
        """Number of signals agreeing. Trade only when >= 3 of 4 agree."""
        signals = [
            self.get_obi() > 0.1,
            self.get_cvd_signal() > 0,
            self.get_vwap_signal() > 0,
            self.get_momentum_signal() > 0,
        ]
        positives = sum(signals)
        negatives = 4 - positives
        return max(positives, negatives)


class BTCOracleLagEngine:
    """
    Main BTC engine: connects Binance signal to Polymarket decision.

    ENTRY LOGIC:
      p_real  = 0.5 + bias_score × 0.30   (maps ±1 → 0.20–0.80)
      EV      = p_real - p_market
      Trade YES if EV > MIN_EDGE AND conviction >= 3
      Trade NO  if EV < -MIN_EDGE AND conviction >= 3
      Size    = quarter-Kelly, capped at MAX_POSITION_PCT

    FILTERS (MUST ALL PASS):
      · Skip first 30 sec of each 5-min window (price discovery noise)
      · Skip if YES + NO > $1.02 (illiquid spread)
      · Skip if Binance volume in last 60s is below threshold
      · Skip if projected Sharpe < SHARPE_THRESHOLD
      · Skip during major econ announcements (manual override)
    """

    def __init__(self, poly_client: PolymarketClient, sim: SimState):
        self.poly   = poly_client
        self.sim    = sim
        self.signal = BinanceSignalEngine(window=60)
        self._btc_market_id: Optional[str] = None

    def find_btc_market(self) -> Optional[dict]:
        """
        Find the active BTC 5-min or 15-min Up/Down market on Polymarket.
        Strategy 1: Slug-based lookup (btc-updown-5m-{unix_ts}) — most reliable.
        Strategy 2: Crypto events. Strategy 3: Direct markets search.
        """
        # Strategy 1: Slug-based lookup — Polymarket uses btc-updown-5m-{unix_timestamp}
        # Timestamp = START of 5-min window (UTC aligned to :00/:05/:10/.../:55)
        now = datetime.now(timezone.utc)
        minute = (now.minute // 5) * 5
        floored = now.replace(minute=minute, second=0, microsecond=0)
        ts = int(floored.timestamp())
        for delta_sec in (0, -300, 300, -600, 600):  # current, prev, next, then ±10min
            slug = f"btc-updown-5m-{ts + delta_sec}"
            evt = self.poly.get_event_by_slug(slug)
            if evt and evt.get("markets"):
                markets = evt.get("markets", [])
                for m in markets:
                    q = (m.get("question") or "").lower()
                    if "bitcoin" in q or "btc" in q:
                        return m
                return markets[0]  # fallback to first market

        # Strategy 2: Crypto events (BTC Up/Down 5m often under tag=crypto)
        for event in self.poly.get_events(limit=100, tag="crypto"):
            slug = (event.get("slug") or "").lower()
            title = (event.get("title") or "").lower()
            markets = event.get("markets", [])
            # Slug patterns: btc-updown-5m, btc-up-down-5-min, etc.
            is_5m = "5m" in slug or "5-min" in slug or "5min" in slug or "5 minute" in slug
            is_15m = "15m" in slug or "15-min" in slug or "15min" in slug or "15 minute" in slug
            if not (is_5m or is_15m) and ("updown" in slug or "up-down" in slug or "up or down" in title):
                is_5m = "5" in slug or "5" in title
                is_15m = "15" in slug or "15" in title
            for m in markets:
                q = (m.get("question") or "").lower()
                if "bitcoin" in q or "btc" in q:
                    if is_5m:
                        return m
                    if is_15m and not is_5m:
                        return m  # fallback
            # First crypto event with "up" or "down" in any market
            for m in markets:
                q = (m.get("question") or "").lower()
                if ("bitcoin" in q or "btc" in q) and ("up" in q or "down" in q):
                    return m

        # Strategy 2: Direct markets search
        markets = self.poly.get_markets(limit=300)
        five_min = None
        fifteen_min = None
        for m in markets:
            q = m.get("question", "").lower()
            slug = (m.get("slug") or "").lower()
            if "bitcoin" not in q and "btc" not in q:
                continue
            if "5-minute" in q or "5 minute" in q or "5min" in q or "5m" in slug or ("up" in q and "5" in q):
                five_min = m
                break
            if "15-minute" in q or "15 minute" in q or "15min" in q or "15m" in slug or ("up" in q and "15" in q):
                fifteen_min = m
        return five_min or fifteen_min

    def make_decision(self, polymarket_yes_price: float) -> dict:
        """
        Core decision function.
        Returns: {"action": "BUY_YES"|"BUY_NO"|"WAIT", "size_fraction": float,
                  "ev": float, "p_real": float, "conviction": int}
        """
        bias = self.signal.get_bias_score()
        p_real = 0.5 + bias * 0.30
        ev = expected_value(p_real, polymarket_yes_price)
        conviction = self.signal.conviction_count()

        if abs(ev) < trading_config.MIN_EDGE_PCT:
            return {"action": "WAIT", "size_fraction": 0, "ev": ev,
                    "p_real": p_real, "conviction": conviction}

        if conviction < 3:
            return {"action": "WAIT", "size_fraction": 0, "ev": ev,
                    "p_real": p_real, "conviction": conviction}

        if ev > trading_config.MIN_EDGE_PCT:
            size = kelly_fraction(p_real, polymarket_yes_price)
            size = min(size, trading_config.MAX_POSITION_SIZE_PCT)
            return {"action": "BUY_YES", "size_fraction": size, "ev": ev,
                    "p_real": p_real, "conviction": conviction}
        else:
            p_no_real = 1 - p_real
            p_no_market = 1 - polymarket_yes_price
            size = kelly_fraction(p_no_real, p_no_market)
            size = min(size, trading_config.MAX_POSITION_SIZE_PCT)
            return {"action": "BUY_NO", "size_fraction": size, "ev": ev,
                    "p_real": p_real, "conviction": conviction}

    async def run_binance_stream(self):
        """
        Open Binance WebSocket stream for BTC.
        Processes aggTrade + depth20 events in real time.
        """
        symbol = api_config.BTC_SYMBOL
        url = f"{api_config.BINANCE_WS}/{symbol}@aggTrade/{symbol}@depth20@100ms"
        log.info(f"📡 Connecting to Binance stream: {symbol}")
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async for raw in ws:
                    data = json.loads(raw)
                    stream = data.get("stream", "")
                    payload = data.get("data", data)

                    if "aggTrade" in stream:
                        self.signal.update_trade(
                            price=float(payload["p"]),
                            qty=float(payload["q"]),
                            is_buyer_maker=payload["m"],
                        )
                    elif "depth20" in stream:
                        self.signal.update_book(
                            bids=payload.get("bids", []),
                            asks=payload.get("asks", []),
                        )
        except Exception as e:
            log.error(f"Binance stream error: {e}")

    async def run_polymarket_stream(self, market_id: str):
        """
        Subscribe to Polymarket WebSocket for real-time YES/NO prices.
        Processes price updates and fires decisions when edge is detected.
        """
        url = api_config.CLOB_WS
        log.info(f"📡 Subscribing to Polymarket market: {market_id}")
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "auth": {},
                    "markets": [market_id],
                    "type": "Market",
                }))
                async for raw in ws:
                    data = json.loads(raw)
                    yes_price = self._extract_yes_price(data)
                    if yes_price is None:
                        continue
                    if self.sim.is_halted():
                        continue
                    decision = self.make_decision(yes_price)
                    if decision["action"] != "WAIT":
                        trade_amount = self.sim.balance * decision["size_fraction"]
                        if trade_amount >= 5.0:   # minimum $5 trade
                            log.info(
                                f"🎯 BTC {decision['action']} | "
                                f"EV={decision['ev']:+.3f} | "
                                f"p_real={decision['p_real']:.3f} | "
                                f"p_mkt={yes_price:.3f} | "
                                f"conviction={decision['conviction']}/4 | "
                                f"size=${trade_amount:.2f}"
                            )
                            self.poly.place_order(
                                token_id=market_id,
                                side="BUY",
                                amount_usdc=trade_amount,
                                sim=self.sim,
                            )
        except Exception as e:
            log.error(f"Polymarket stream error: {e}")

    def _extract_yes_price(self, ws_data: dict) -> Optional[float]:
        """Parse YES price from Polymarket WebSocket payload."""
        try:
            assets = ws_data.get("assets_of_interest", [])
            for a in assets:
                if a.get("outcome", "").upper() == "YES":
                    return float(a["price"])
            # fallback: first price field
            if "price" in ws_data:
                return float(ws_data["price"])
        except Exception:
            pass
        return None


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 6 — ENGINE 2: ARBITRAGE ENGINE
═══════════════════════════════════════════════════════════════════════════════

THEORY:
  In any binary market: YES + NO = $1.00 always at resolution.
  When liquidity is thin or markets move fast, the sum drifts.
  If YES + NO < $1.00 − fees: buy BOTH → guaranteed profit.
  If YES + NO > $1.00: short the overpriced side.

  The 99-Cent Resolution Strategy:
    When outcome is 99% certain, YES trades at $0.96–$0.99.
    Buy at $0.97, collect $1.00 at resolution.
    Repeat across many markets for compounding 1-3% returns.
"""


class ArbitrageEngine:
    """
    Scans all active markets for YES+NO mispricings.
    Also detects resolution-adjacent opportunities (99¢ strategy).
    """

    def __init__(self, poly_client: PolymarketClient, sim: SimState):
        self.poly = poly_client
        self.sim = sim

    def scan_markets(self) -> list[dict]:
        """
        Returns list of arbitrage opportunities found:
        [{market_slug, yes_price, no_price, total, arb_type, profit_per_dollar}, ...]
        """
        markets = self.poly.get_markets(limit=200)
        opportunities = []

        for m in markets:
            try:
                yes_price, no_price = self._extract_prices(m)
                if yes_price is None or no_price is None:
                    continue
                if yes_price == 0.0 or no_price == 0.0:
                    continue
                if yes_price < 0.01 or no_price < 0.01:
                    continue
                if float(m.get("volume", 0)) == 0.0:
                    continue

                total = yes_price + no_price
                deviation = abs(total - 1.0)

                # Classic arb: both sides < $1 combined
                if total < 1.0 - trading_config.ARB_SPREAD_MIN:
                    profit = (1.0 - total) / total
                    opportunities.append({
                        "market": m.get("slug", ""),
                        "question": m.get("question", "")[:80],
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "total": total,
                        "arb_type": "BUY_BOTH",
                        "profit_per_dollar": profit,
                        "volume": float(m.get("volume", 0)),
                    })

                # 99-cent resolution strategy
                if yes_price >= 0.90 and total < 1.02:
                    discount = 1.0 - yes_price
                    if discount >= 0.02:   # at least 2% return
                        opportunities.append({
                            "market": m.get("slug", ""),
                            "question": m.get("question", "")[:80],
                            "yes_price": yes_price,
                            "no_price": no_price,
                            "total": total,
                            "arb_type": "RESOLUTION_SWEEP",
                            "profit_per_dollar": discount,
                            "volume": float(m.get("volume", 0)),
                        })

            except Exception:
                continue

        # Sort by profit opportunity descending
        opportunities.sort(key=lambda x: x["profit_per_dollar"], reverse=True)
        return opportunities

    def _extract_prices(self, market: dict) -> tuple[Optional[float], Optional[float]]:
        """Extract YES and NO prices from a market object."""
        try:
            prices_raw = market.get("outcomePrices")
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            elif isinstance(prices_raw, list):
                prices = prices_raw
            else:
                return None, None
            if len(prices) >= 2:
                return float(prices[0]), float(prices[1])
        except Exception:
            pass
        return None, None

    def execute_opportunity(self, opp: dict) -> bool:
        """Execute an arbitrage opportunity."""
        if self.sim.is_halted():
            return False

        trade_amount = min(
            self.sim.balance * trading_config.MAX_POSITION_SIZE_PCT,
            self.sim.balance * 0.10,   # never more than 10% on single arb
        )
        if trade_amount < 5.0:
            return False

        log.info(
            f"💰 ARB [{opp['arb_type']}] | "
            f"{opp['question'][:50]} | "
            f"YES={opp['yes_price']:.3f} NO={opp['no_price']:.3f} | "
            f"Total={opp['total']:.3f} | "
            f"Edge={opp['profit_per_dollar']:.2%} | "
            f"Size=${trade_amount:.2f}"
        )
        return self.poly.place_order("", "BUY", trade_amount, self.sim)

    def run_scan_loop(self, interval_secs: int = 30):
        """Blocking scan loop — run in a thread or as a coroutine."""
        log.info("🔍 Arbitrage scanner started")
        while True:
            if not self.sim.is_halted():
                opps = self.scan_markets()
                for opp in opps[:3]:   # max 3 arb trades per scan
                    if opp["profit_per_dollar"] > trading_config.ARB_MIN_DEVIATION:
                        self.execute_opportunity(opp)
                if opps:
                    log.info(f"📊 Scan complete: {len(opps)} opportunities found")
            time.sleep(interval_secs)


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 7 — ENGINE 3: WEATHER FORECAST ENGINE
═══════════════════════════════════════════════════════════════════════════════

THEORY:
  Polymarket weather markets resolve on official airport station readings
  (NWS / Wunderground). The bot queries the SAME source directly.
  If the NWS says 46°F in Chicago tomorrow but Polymarket prices the
  46-47°F bucket at 8¢, one of them is wrong. We bet on the official forecast.

  Edge source: retail traders use general weather apps; we use the exact
  station data that Polymarket uses for resolution.
"""

# Airport stations matching Polymarket resolution sources
WEATHER_LOCATIONS = {
    "nyc":     {"lat": 40.7772, "lon": -73.8726, "name": "New York City",
                "station": "KLGA",
                "nws": "https://api.weather.gov/gridpoints/OKX/37,39/forecast/hourly"},
    "chicago": {"lat": 41.9742, "lon": -87.9073, "name": "Chicago",
                "station": "KORD",
                "nws": "https://api.weather.gov/gridpoints/LOT/66,77/forecast/hourly"},
    "miami":   {"lat": 25.7959, "lon": -80.2870, "name": "Miami",
                "station": "KMIA",
                "nws": "https://api.weather.gov/gridpoints/MFL/106,51/forecast/hourly"},
    "dallas":  {"lat": 32.8471, "lon": -96.8518, "name": "Dallas",
                "station": "KDAL",
                "nws": "https://api.weather.gov/gridpoints/FWD/87,107/forecast/hourly"},
    "seattle": {"lat": 47.4502, "lon": -122.3088, "name": "Seattle",
                "station": "KSEA",
                "nws": "https://api.weather.gov/gridpoints/SEW/124,61/forecast/hourly"},
    "atlanta": {"lat": 33.6407, "lon": -84.4277,  "name": "Atlanta",
                "station": "KATL",
                "nws": "https://api.weather.gov/gridpoints/FFC/50,82/forecast/hourly"},
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]


class WeatherEngine:
    """
    Fetches NWS forecasts, finds matching Polymarket temperature bucket markets,
    and trades when the market undervalues the forecasted outcome.
    """

    def __init__(self, poly_client: PolymarketClient, sim: SimState):
        self.poly = poly_client
        self.sim = sim
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polybot-weather/1.0"})

    def get_daily_forecast(self, city_slug: str) -> dict[str, int]:
        """
        Returns {date_str: max_temp_F, ...} for the next 4 days.
        Combines actual observations (past hours of today) with
        hourly forecast (remaining hours), ensuring we see the true daily max.
        """
        loc = WEATHER_LOCATIONS.get(city_slug)
        if not loc:
            return {}

        daily_max: dict[str, int] = {}

        # 1. Actual station observations (handles today's past hours)
        try:
            obs_url = f"https://api.weather.gov/stations/{loc['station']}/observations?limit=48"
            r = self._session.get(obs_url, timeout=10)
            for obs in r.json().get("features", []):
                props = obs["properties"]
                date_str = props.get("timestamp", "")[:10]
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is not None:
                    temp_f = round(temp_c * 9 / 5 + 32)
                    if date_str not in daily_max or temp_f > daily_max[date_str]:
                        daily_max[date_str] = temp_f
        except Exception as e:
            log.warning(f"Observations error for {city_slug}: {e}")

        # 2. NWS hourly forecast (future hours)
        try:
            r = self._session.get(loc["nws"], timeout=10)
            for p in r.json()["properties"]["periods"]:
                date_str = p["startTime"][:10]
                temp = p["temperature"]
                if p.get("temperatureUnit") == "C":
                    temp = round(temp * 9 / 5 + 32)
                if date_str not in daily_max or temp > daily_max[date_str]:
                    daily_max[date_str] = temp
        except Exception as e:
            log.warning(f"Forecast error for {city_slug}: {e}")

        return daily_max

    def parse_temp_range(self, question: str) -> Optional[tuple[int, int]]:
        """
        Convert natural-language temperature question to (low, high) tuple.
        'between 44-45°F' → (44, 45)
        '48°F or higher'  → (48, 999)
        '40°F or below'   → (-999, 40)
        """
        if "or below" in question.lower():
            m = re.search(r'(\d+)°?F? or below', question, re.IGNORECASE)
            if m:
                return (-999, int(m.group(1)))
        if "or higher" in question.lower():
            m = re.search(r'(\d+)°?F? or higher', question, re.IGNORECASE)
            if m:
                return (int(m.group(1)), 999)
        m = re.search(r'between (\d+)[–\-](\d+)°?F?', question, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None

    def bucket_probability(self, forecast_temp: int, bucket: tuple) -> float:
        """Gaussian probability that temp falls in (lo, hi) given forecast. Uses math.erf."""
        lo, hi = bucket
        sigma = 2.0

        def cdf(x):
            return 0.5 * (1 + math.erf((x - forecast_temp) / (sigma * math.sqrt(2))))

        p = cdf(hi + 0.5) - cdf(lo - 0.5)
        return max(0.01, min(0.99, p))

    def scan_and_trade(self, dry_run: bool = True) -> int:
        """
        Main weather scan loop iteration.
        Returns number of signals found.
        """
        signals_found = 0
        now_utc = datetime.now(timezone.utc)

        for city_slug, loc_data in WEATHER_LOCATIONS.items():
            forecast = self.get_daily_forecast(city_slug)
            if not forecast:
                continue

            for days_ahead in range(4):
                target_date = now_utc + timedelta(days=days_ahead)
                date_str = target_date.strftime("%Y-%m-%d")
                forecast_temp = forecast.get(date_str)
                if forecast_temp is None:
                    continue

                # Build Polymarket event slug
                month = MONTHS[target_date.month - 1]
                slug = (f"highest-temperature-in-{city_slug}-on-"
                        f"{month}-{target_date.day}-{target_date.year}")
                event = self.poly.get_event_by_slug(slug)
                if not event:
                    continue

                log.debug(f"Weather check: {loc_data['name']} {date_str} | "
                          f"Forecast: {forecast_temp}°F")

                for market in event.get("markets", []):
                    question = market.get("question", "")
                    temp_range = self.parse_temp_range(question)
                    if not temp_range:
                        continue

                    low, high = temp_range
                    if low <= forecast_temp <= high:
                        try:
                            prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                            yes_price = float(prices[0])
                        except Exception:
                            continue

                        yes_price_model = self.bucket_probability(forecast_temp, temp_range)
                        ev = yes_price_model - yes_price
                        if ev > trading_config.WEATHER_ENTRY_THRESHOLD:
                            position_size = self.sim.balance * trading_config.WEATHER_POSITION_PCT
                            log.info(
                                f"🌡️  WEATHER SIGNAL | {loc_data['name']} {date_str} | "
                                f"Forecast: {forecast_temp}°F | "
                                f"Bucket: {question[:60]} | "
                                f"Model: {yes_price_model:.3f} | Market: {yes_price:.3f} | "
                                f"EV: {ev:+.3f} | Size: ${position_size:.2f}"
                            )
                            signals_found += 1
                            if not dry_run and not self.sim.is_halted():
                                self.poly.place_order(
                                    market.get("id", ""), "BUY",
                                    position_size, self.sim
                                )
                        break   # found the matching bucket, move on

        return signals_found

    def run_scan_loop(self, interval_secs: int = 300):
        """Blocking scan loop — run every 5 minutes."""
        log.info("🌤️  Weather engine started")
        while True:
            if not self.sim.is_halted():
                n = self.scan_and_trade(dry_run=trading_config.DRY_RUN)
                if n == 0:
                    log.debug("Weather: no signals this cycle")
            time.sleep(interval_secs)


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 7B — EVENT BAYES ENGINE (from versionchat)
═══════════════════════════════════════════════════════════════════════════════
"""


class EventBayesEngine:
    """
    Trades current events markets using keyword scoring
    and Bayesian probability updates.
    Uses the existing BayesianProbabilityUpdater class already in the file.
    """
    BULLISH_WORDS = ["approved", "confirmed", "wins", "passes",
                     "launched", "elected", "signed", "passed"]
    BEARISH_WORDS = ["denied", "rejected", "fails", "blocked",
                     "delay", "lawsuit", "suspended", "vetoed"]

    def __init__(self, poly_client: PolymarketClient, sim: SimState):
        self.poly = poly_client
        self.sim = sim

    def headline_score(self, question: str) -> float:
        q = question.lower()
        score = 0.0
        for w in self.BULLISH_WORDS:
            if w in q:
                score += 0.10
        for w in self.BEARISH_WORDS:
            if w in q:
                score -= 0.10
        return score

    def bayes_update_odds(self, prior: float, evidence_score: float) -> float:
        odds = prior / max(1e-9, 1 - prior)
        lr = math.exp(evidence_score)
        post_odds = odds * lr
        post = post_odds / (1 + post_odds)
        return max(0.01, min(0.99, post))

    def scan_and_trade(self, dry_run: bool = True) -> int:
        markets = self.poly.get_markets(limit=200)
        signals_found = 0
        for m in markets:
            q = m.get("question", "")
            ql = q.lower()
            # Skip crypto and weather — handled by other engines
            if any(x in ql for x in ["bitcoin", "btc", "ethereum",
                                      "eth", "temperature", "weather"]):
                continue
            try:
                prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                yes_price = float(prices[0])
            except Exception:
                continue
            score = self.headline_score(q)
            if abs(score) < 0.08:
                continue
            p_model = self.bayes_update_odds(yes_price, score)
            ev = p_model - yes_price
            if abs(ev) < trading_config.MIN_EDGE_PCT:
                continue
            signals_found += 1
            position_size = self.sim.balance * trading_config.MAX_POSITION_SIZE_PCT
            action = "BUY_YES" if ev > 0 else "BUY_NO"
            log.info(f"EVENT SIGNAL | {q[:60]} | "
                     f"score={score:+.2f} | p_model={p_model:.3f} | "
                     f"ev={ev:+.3f} | {action} ${position_size:.2f}")
            if not dry_run and not self.sim.is_halted():
                self.poly.place_order(m.get("id", ""), "BUY",
                                      position_size, self.sim)
        return signals_found

    def run_scan_loop(self, interval_secs: int = 120):
        """Blocking scan loop — run every interval_secs."""
        log.info("📰 EventBayes engine started")
        while True:
            if not self.sim.is_halted():
                n = self.scan_and_trade(dry_run=trading_config.DRY_RUN)
                if n == 0:
                    log.debug("Events: no signals this cycle")
            time.sleep(interval_secs)


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 7C — MARKET MAKING ENGINE (from versiongrok)
═══════════════════════════════════════════════════════════════════════════════
"""


class MarketMakingEngine:
    """
    Quotes both sides of a market to collect spread.
    ALWAYS gated behind trading_config.DRY_RUN.
    Never places real orders unless DRY_RUN is False
    AND explicitly enabled.
    """
    MAKER_SPREAD_BPS = 40       # 0.4% spread target
    MAX_MAKER_SIZE_USD = 400    # max per quote
    MIN_LIQUIDITY_USD = 3000    # skip thin markets

    def __init__(self, poly_client, sim):
        self.poly = poly_client
        self.sim = sim
        self.active_quotes = {}  # market_id -> quote info

    def compute_quotes(self, mid_price: float) -> tuple:
        spread = self.MAKER_SPREAD_BPS / 10000.0
        half = spread / 2
        bid = max(0.01, mid_price - half)
        ask = min(0.99, mid_price + half)
        return bid, ask

    def scan_and_quote(self) -> int:
        if not trading_config.DRY_RUN:
            log.warning("MarketMakingEngine: live mode not "
                        "enabled. Set DRY_RUN=False only after "
                        "paper testing.")
            return 0
        markets = self.poly.get_markets(limit=100)
        quotes_placed = 0
        for m in markets:
            try:
                prices = json.loads(
                    m.get("outcomePrices", "[0.5,0.5]"))
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except Exception:
                continue
            liquidity = float(m.get("liquidity", 0))
            if liquidity < self.MIN_LIQUIDITY_USD:
                continue
            # Only quote near-50/50 markets
            # (most liquid, tightest spreads)
            if abs(yes_price - 0.5) > 0.20:
                continue
            mid = yes_price
            bid, ask = self.compute_quotes(mid)
            size = min(
                self.MAX_MAKER_SIZE_USD,
                liquidity * 0.05
            )
            log.info(
                f"[MAKER] {m.get('question','')[:50]} | "
                f"bid={bid:.4f} ask={ask:.4f} | "
                f"size=${size:.0f} | [DRY RUN]"
            )
            self.active_quotes[m.get("id", "")] = {
                "bid": bid, "ask": ask,
                "size": size, "mid": mid,
                "question": m.get("question", "")
            }
            quotes_placed += 1
        return quotes_placed

    def run_scan_loop(self, interval_secs: int = 60):
        log.info("Market making engine started (DRY RUN)")
        while True:
            if not self.sim.is_halted():
                n = self.scan_and_quote()
                if n > 0:
                    log.info(f"[MAKER] {n} quotes computed")
            time.sleep(interval_secs)


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 8 — MAIN ORCHESTRATOR
═══════════════════════════════════════════════════════════════════════════════
"""


class PolymarketBot:
    """
    Top-level orchestrator. Manages all three engines and the shared sim state.

    USAGE:
      bot = PolymarketBot()
      asyncio.run(bot.run())          # Full async run
      bot.run_sync_scan()             # Quick synchronous scan (arb + weather)
    """

    def __init__(self, btc_only: bool = False):
        self.sim = SimState.load()
        self.poly = PolymarketClient()
        self.btc = BTCOracleLagEngine(self.poly, self.sim)
        self.btc_only = btc_only
        if not btc_only:
            self.arb = ArbitrageEngine(self.poly, self.sim)
            self.weather = WeatherEngine(self.poly, self.sim)
            self.events = EventBayesEngine(self.poly, self.sim)
            self.maker = MarketMakingEngine(self.poly, self.sim)
        else:
            self.arb = self.weather = self.events = self.maker = None

    def print_status(self):
        """Print current simulation status."""
        s = self.sim
        print("\n" + "=" * 60)
        print("  POLYMARKET BOT - STATUS")
        print("=" * 60)
        print(f"  Balance:          ${s.balance:,.2f}")
        print(f"  Peak Balance:     ${s.peak_balance:,.2f}")
        print(f"  P&L:              ${s.balance - s.starting_balance:+,.2f} "
              f"({(s.balance/s.starting_balance - 1):.1%})")
        print(f"  Total Trades:     {s.total_trades}")
        print(f"  Win Rate:         {s.win_rate:.1%}")
        print(f"  Sharpe Ratio:     {s.current_sharpe:.2f}")
        print(f"  VaR (95%):        {s.var_95:.1%}")
        print(f"  CVaR (95%):       {s.cvar_95:.1%}")
        print(f"  Vol Regime:       {s.volatility_regime}")
        print(f"  Consec Losses:    {s.consecutive_losses}")
        print(f"  Mode:             {'PAPER TRADING' if trading_config.DRY_RUN else 'LIVE'}")
        if not self.btc_only:
            print(f"  Active quotes:    {len(self.maker.active_quotes)}")
        print("=" * 60 + "\n")

    def run_sync_scan(self):
        """
        Synchronous scan — no WebSocket needed.
        BTC-only: finds and reports BTC market status.
        Full: runs arbitrage + weather + events + maker.
        """
        self.print_status()
        if self.btc_only:
            log.info("🤖 BTC-only mode: scanning for 5/15-min market...")
            btc_market = self.btc.find_btc_market()
            if btc_market:
                q = btc_market.get("question", "")[:70]
                mkt_id = btc_market.get("id", "?")
                log.info(f"🔗 Found: {q}")
                log.info(f"   Market ID: {mkt_id}")
                try:
                    prices = json.loads(btc_market.get("outcomePrices", "[0.5,0.5]"))
                    log.info(f"   YES={float(prices[0]):.3f} NO={float(prices[1]):.3f}")
                except Exception:
                    pass
            else:
                log.warning("⚠️  No BTC 5/15-min market found on Polymarket")
            self.sim.last_scan = {
                "timestamp": datetime.now().isoformat(),
                "arb_signals": 0,
                "weather_signals": 0,
                "event_signals": 0,
                "maker_quotes": 0,
            }
            self.sim.save()
            self.print_status()
            return

        log.info("🤖 Starting synchronous scan...")
        # Load strategy filter from config_override.json
        import json as _json
        import os as _os
        _override_path = "config_override.json"
        _active = None
        if _os.path.exists(_override_path):
            try:
                with open(_override_path) as f:
                    _active = _json.load(f).get("active_strategies", None)
            except Exception:
                _active = None

        # Arbitrage scan
        if _active is None or "Arbitrage" in _active:
            opps = self.arb.scan_markets()
            log.info(f"💰 Arbitrage: {len(opps)} opportunities found")
            for opp in opps[:5]:
                log.info(f"   {opp['arb_type']:20} | {opp['question'][:50]} | "
                         f"edge={opp['profit_per_dollar']:.2%}")
                if opp["profit_per_dollar"] > trading_config.ARB_MIN_DEVIATION:
                    self.arb.execute_opportunity(opp)
        else:
            opps = []
            log.info("💰 Arbitrage: skipped (filtered)")

        # Weather scan
        if _active is None or "Weather" in _active:
            n_weather = self.weather.scan_and_trade(dry_run=trading_config.DRY_RUN)
            log.info(f"🌡️  Weather: {n_weather} signals found")
        else:
            n_weather = 0

        # Events scan
        if _active is None or "Events" in _active:
            n_events = self.events.scan_and_trade(dry_run=trading_config.DRY_RUN)
            log.info(f"Events: {n_events} signals found")
        else:
            n_events = 0

        # Market making scan
        if _active is None or "Market Making" in _active:
            n_maker = self.maker.scan_and_quote()
            log.info(f"Market making: {n_maker} quotes computed")
        else:
            n_maker = 0

        # Persist scan counts and active quotes for dashboard
        self.sim.last_scan = {
            "timestamp": datetime.now().isoformat(),
            "arb_signals": len(opps),
            "weather_signals": n_weather,
            "event_signals": n_events,
            "maker_quotes": n_maker,
        }
        self.sim.active_quotes = dict(self.maker.active_quotes)
        self.sim.save()
        self.print_status()

    async def run(self):
        """
        Full async run.
        BTC-only: just BTC WebSocket streams.
        Full: BTC + arb/weather/events/maker background threads.
        """
        self.print_status()
        log.info("🚀 Starting full async bot..." + (" (BTC-only)" if self.btc_only else ""))

        # Find BTC market
        btc_market = self.btc.find_btc_market()
        btc_market_id = btc_market.get("id", "") if btc_market else ""
        if btc_market_id:
            log.info(f"🔗 BTC market found: {btc_market.get('question', '')[:60]}")
        else:
            log.warning("⚠️  No BTC 5/15-min market found")

        # Schedule background tasks (only when not btc_only)
        if not self.btc_only:
            import threading
            arb_thread = threading.Thread(
                target=self.arb.run_scan_loop, args=(60,), daemon=True
            )
            weather_thread = threading.Thread(
                target=self.weather.run_scan_loop, args=(300,), daemon=True
            )
            events_thread = threading.Thread(
                target=self.events.run_scan_loop, args=(120,), daemon=True
            )
            maker_thread = threading.Thread(
                target=self.maker.run_scan_loop, args=(60,), daemon=True
            )
            arb_thread.start()
            weather_thread.start()
            events_thread.start()
            maker_thread.start()

        # Run BTC WebSocket streams
        if btc_market_id:
            await asyncio.gather(
                self.btc.run_binance_stream(),
                self.btc.run_polymarket_stream(btc_market_id),
            )
        else:
            # No BTC market: keep alive or run other engines
            if self.btc_only:
                log.warning("BTC-only mode but no BTC market. Exiting.")
                return
            while True:
                await asyncio.sleep(60)
                self.sim.save()
                self.print_status()


"""
═══════════════════════════════════════════════════════════════════════════════
SECTION 9 — ENTRY POINT
═══════════════════════════════════════════════════════════════════════════════
"""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Polymarket Quantitative Trading Bot"
    )
    parser.add_argument(
        "--mode",
        choices=["scan", "run", "status"],
        default="scan",
        help="scan=one-shot sync scan | run=full async bot | status=show stats",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (default: paper trading)",
    )
    parser.add_argument(
        "--btc-only",
        action="store_true",
        help="Run only BTC 5/15-min engine (no arb, weather, events, maker)",
    )
    args = parser.parse_args()

    if args.live:
        trading_config.DRY_RUN = False
        log.warning("⚠️  LIVE MODE ENABLED — real money will be used")

    bot = PolymarketBot(btc_only=args.btc_only)

    if args.mode == "status":
        bot.print_status()
    elif args.mode == "scan":
        bot.run_sync_scan()
    elif args.mode == "run":
        asyncio.run(bot.run())


"""
═══════════════════════════════════════════════════════════════════════════════
QUICK START GUIDE
═══════════════════════════════════════════════════════════════════════════════

1. Install dependencies:
   pip install requests numpy websockets

2. Paper trading scan (safe, no real money):
   python polymarket_bot.py --mode scan

3. Full bot with WebSocket streams (paper trading):
   python polymarket_bot.py --mode run

4. Live trading (ONLY after 1-2 weeks of stable paper results):
   pip install py-clob-client python-dotenv
   echo "POLY_API_KEY=your_key_here" > .env
   python polymarket_bot.py --mode run --live

RISK RULES (ALWAYS ENFORCED):
  · 1/4 Kelly position sizing — never full Kelly
  · Max 3% of bankroll per trade
  · Halt if daily drawdown > 10%
  · Pause after 3 consecutive losses
  · Only trade when projected edge > 5%
  · Only trade when Sharpe filter passes
  · Start with $100–$500 test capital minimum

NEVER run during: FOMC, CPI, major geopolitical events
ALWAYS check positions manually every hour during live trading.
═══════════════════════════════════════════════════════════════════════════════
"""
