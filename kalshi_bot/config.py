"""
config.py — All configuration for the Kalshi 15-min multi-asset bot.
Single source of truth. All other modules import from here.
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()


# ─── Asset manifest ──────────────────────────────────────────────────────────
# Kalshi series tickers for 15-min crypto markets.
# Verify these against Kalshi's API before live trading:
#   GET /trade-api/v2/series  →  look for series_ticker containing KXBTC, KXETH, etc.
# XRP 15-min availability is uncertain — set enabled=False to skip at startup.

@dataclass
class AssetSpec:
    symbol: str               # e.g. "BTC"
    series_ticker: str        # Kalshi series, e.g. "KXBTC15M"
    binance_symbol: str      # e.g. "btcusdt"
    okx_inst_id: str         # e.g. "BTC-USDT"
    enabled: bool = True


ASSETS: List[AssetSpec] = [
    AssetSpec("BTC", "KXBTC15M", "btcusdt",  "BTC-USDT",  enabled=True),
    AssetSpec("ETH", "KXETH15M", "ethusdt",  "ETH-USDT",  enabled=True),
    AssetSpec("SOL", "KXSOL15M", "solusdt",  "SOL-USDT",  enabled=True),
    # Disabled after R2/R3 (0–17% WR, net drag). Pass --enable-xrp to trade it again.
    AssetSpec("XRP", "KXXRP15M", "xrpusdt",  "XRP-USDT",  enabled=False),
]


# ─── Trading parameters ───────────────────────────────────────────────────────

@dataclass
class TradingConfig:
    # PAPER-ONLY aggressive profile: maximize fire rate + size. Do NOT use live.
    # Classic max_risk_paper (same spirit as R11/R12/R19).
    CONFIG_PROFILE: str         = "max_risk_paper"

    # Kelly / position sizing — large bets
    KELLY_FRACTION: float       = 0.50
    MIN_EDGE_PCT: float         = 0.010
    MAX_POS_PCT: float          = 0.08      # 8% of bankroll per trade
    PORTFOLIO_GROSS_CAP: float  = 0.30      # allow up to ~3 concurrent max-size positions
    MIN_TRADE_USD: float        = 5.0

    # Circuit breaker — rarely pause
    MAX_CONSEC_LOSSES: int      = 8
    COOLDOWN_MINUTES: float     = 10.0
    PER_ASSET_CIRCUIT_BREAKER: bool = True
    MAX_DAILY_LOSS_PCT: float   = 0.40      # allow deep drawdown in paper
    # Soft DD halt kept (equity-based) so vault skims don't fake-freeze; high threshold
    MAX_DRAWDOWN_PCT: float     = 0.50
    DRAWDOWN_HALT_ENABLED: bool = True
    DRAWDOWN_USE_EQUITY: bool   = True
    DAILY_RESET_HOUR_UTC: int   = 0

    # Activity mandate OFF — classic max_risk does not force probe trades
    ACTIVITY_MANDATE_ENABLED: bool = False
    ACTIVITY_IDLE_SECS: float      = 3600.0
    ACTIVITY_PROBE_SIZE_PCT: float = 0.025
    ACTIVITY_EDGE_SCALE: float     = 0.70
    ACTIVITY_SPOT_CONF_FLOOR: float = 0.30
    ACTIVITY_LAG_SCALE: float      = 0.70

    # Entry gates — loose (lottery tickets allowed, as in R11/R12)
    MIN_ENTRY_PRICE: float      = 0.02
    MAX_ENTRY_PRICE: float      = 0.98
    LAG_CONFIDENCE_MIN: float   = 0.12
    LAG_ABSENT_MIN: float       = 0.08
    CWM_MIN: float              = 0.015
    ALPHA_EDGE_ENABLED: bool    = True
    ALPHA_EDGE_MIN: float       = 0.04
    ALPHA_EDGE_BAND_LOW: float  = 0.02
    ALPHA_EDGE_LAG_MIN: float   = 0.15
    MIN_CONVICTION: int         = 2
    P_BASE_CENTER_MIN: float    = 0.02
    SHARPE_MIN: float           = -99.0
    SHARPE_MIN_TRADES: int      = 10_000

    # Volatility — almost never hard-block
    VOL_HI: float               = 8.00
    VOL_MID: float              = 4.00

    # Spot feed — allow thinner venue coverage
    SPOT_CONFIDENCE_MIN: float  = 0.30

    # Early exit OFF — ride binary 0/1
    EARLY_EXIT_ENABLED: bool            = False
    EARLY_EXIT_MIN_HOLD_SECS: float     = 240.0
    EARLY_EXIT_LOSS_FRACTION: float     = 0.85
    EARLY_EXIT_SPOT_ADVERSE_BPS: float  = 15.0
    EARLY_EXIT_MIN_TIME_LEFT_SECS: float = 120.0
    EARLY_EXIT_MODERATE_LOSS_FRAC: float = 0.70

    MIN_STRUCTURAL_VOL: float   = 0.15

    # Wider structural acceptance
    P_BASE_MIN: float           = 0.02
    P_BASE_MAX: float           = 0.98

    HAWKES_DECAY: float         = 0.046
    HAWKES_ALPHA: float         = 0.8
    OFI_WINDOW_SECS: int        = 120
    LOGIT_EWM_ALPHA: float      = 0.15

    # Trade almost the whole window
    WINDOW_SECS: int            = 900
    SKIP_OPEN_SECS: int         = 10
    SKIP_CLOSE_SECS: int        = 15
    PTB_CAPTURE_SECS: float     = 120.0

    PRICE_MAX_AGE_SECS: float   = 60.0

    # Variance sizing — do NOT shrink lottery entries in this profile
    ENTRY_VAR_MILD_DIST: float  = 0.49
    ENTRY_VAR_HARD_DIST: float  = 0.50
    ENTRY_VAR_MILD_SCALE: float = 1.0
    ENTRY_VAR_HARD_SCALE: float = 1.0
    BELIEF_VOL_MILD: float      = 1.0
    BELIEF_VOL_HARD: float      = 1.0
    BELIEF_VOL_MILD_SCALE: float = 1.0
    BELIEF_VOL_HARD_SCALE: float = 1.0

    # Simulation
    SIM_BALANCE: float          = 2000.0
    SIM_FILE: str               = "logs/kalshi_sim.json"
    DRY_RUN: bool               = True


@dataclass
class APIConfig:
    # Kalshi credentials (from .env)
    KALSHI_API_KEY:     str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    KALSHI_PRIVATE_KEY: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY", ""))

    # Coinbase Advanced Trade (primary — US-available)
    COINBASE_WS: str = "wss://advanced-trade-ws.coinbase.com"

    # Binance (fallback — blocked for US IPs, returns HTTP 451)
    BINANCE_WS: str = "wss://stream.binance.com:9443/stream"

    # OKX public WebSocket (secondary)
    OKX_WS: str = "wss://ws.okx.com:8443/ws/v5/public"

    # Kalshi endpoints
    @property
    def KALSHI_REST(self) -> str:
        override = os.getenv("KALSHI_BASE_URL")
        if override:
            return override.rstrip("/")
        if os.getenv("KALSHI_DEMO", "false").lower() == "true":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def KALSHI_WS(self) -> str:
        if os.getenv("KALSHI_DEMO", "false").lower() == "true":
            return "wss://demo-api.kalshi.co/trade-api/ws/v2"
        return "wss://api.elections.kalshi.com/trade-api/ws/v2"


MICRO_ALPHA_OVERRIDES: dict = {}

BLACKOUT_WINDOWS = [
    (2, 18,  0),   # FOMC Wednesday 18:00 UTC
    (1, 12, 30),   # CPI Tuesday 12:30 UTC
    (4, 12, 30),   # NFP Friday 12:30 UTC
]
BLACKOUT_HALF_WIDTH_SECS = 300


cfg     = TradingConfig()
api_cfg = APIConfig()
