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
    # R14: equity-based DD, vault-aware peaks, hourly activity mandate.
    # Do NOT use live without further fill/settlement hardening.
    CONFIG_PROFILE: str         = "disciplined_paper_v2"

    # Kelly / position sizing — moderate (R12 half-size cut DD ~half)
    KELLY_FRACTION: float       = 0.22
    MIN_EDGE_PCT: float         = 0.015
    MAX_POS_PCT: float          = 0.05      # 5% bankroll per trade
    PORTFOLIO_GROSS_CAP: float  = 0.15      # ~3 concurrent max-size names
    MIN_TRADE_USD: float        = 5.0

    # Circuit breaker
    MAX_CONSEC_LOSSES: int      = 5
    COOLDOWN_MINUTES: float     = 15.0
    PER_ASSET_CIRCUIT_BREAKER: bool = True
    MAX_DAILY_LOSS_PCT: float   = 0.20
    # Peak-to-trough on EQUITY (trading + vault); halt when breached
    MAX_DRAWDOWN_PCT: float     = 0.25
    DRAWDOWN_HALT_ENABLED: bool = True
    DRAWDOWN_USE_EQUITY: bool   = True      # R13 fix: don't freeze on vault skims
    DAILY_RESET_HOUR_UTC: int   = 0

    # If no closed trade for this long, ease soft gates + smaller probe size
    ACTIVITY_MANDATE_ENABLED: bool = True
    ACTIVITY_IDLE_SECS: float      = 3600.0   # ~1 hour
    ACTIVITY_PROBE_SIZE_PCT: float = 0.025    # 2.5% book when probing
    ACTIVITY_EDGE_SCALE: float     = 0.70     # min edge × this when idle
    ACTIVITY_SPOT_CONF_FLOOR: float = 0.30    # allow thinner venue coverage when idle
    ACTIVITY_LAG_SCALE: float      = 0.70

    # Entry gates — R12: 4/4 losses were entry < 0.15; skipping them = +$40 only
    MIN_ENTRY_PRICE: float      = 0.15      # hard reject lottery YES/NO
    MAX_ENTRY_PRICE: float      = 0.85      # hard reject expensive favorites
    LAG_CONFIDENCE_MIN: float   = 0.18
    LAG_ABSENT_MIN: float       = 0.12
    CWM_MIN: float              = 0.020
    ALPHA_EDGE_ENABLED: bool    = True
    ALPHA_EDGE_MIN: float       = 0.05
    ALPHA_EDGE_BAND_LOW: float  = 0.03
    ALPHA_EDGE_LAG_MIN: float   = 0.18
    MIN_CONVICTION: int         = 2
    P_BASE_CENTER_MIN: float    = 0.03
    SHARPE_MIN: float           = -99.0
    SHARPE_MIN_TRADES: int      = 10_000

    # Volatility
    VOL_HI: float               = 5.00
    VOL_MID: float              = 2.50

    # Spot feed
    SPOT_CONFIDENCE_MIN: float  = 0.45

    # Early exit OFF — ride binary 0/1
    EARLY_EXIT_ENABLED: bool            = False
    EARLY_EXIT_MIN_HOLD_SECS: float     = 240.0
    EARLY_EXIT_LOSS_FRACTION: float     = 0.85
    EARLY_EXIT_SPOT_ADVERSE_BPS: float  = 15.0
    EARLY_EXIT_MIN_TIME_LEFT_SECS: float = 120.0
    EARLY_EXIT_MODERATE_LOSS_FRAC: float = 0.70

    MIN_STRUCTURAL_VOL: float   = 0.15

    # Structural band (still allow wide markets, but size/entry floors gate risk)
    P_BASE_MIN: float           = 0.05
    P_BASE_MAX: float           = 0.95

    HAWKES_DECAY: float         = 0.046
    HAWKES_ALPHA: float         = 0.8
    OFI_WINDOW_SECS: int        = 120
    LOGIT_EWM_ALPHA: float      = 0.15

    WINDOW_SECS: int            = 900
    SKIP_OPEN_SECS: int         = 20
    SKIP_CLOSE_SECS: int        = 30
    PTB_CAPTURE_SECS: float     = 120.0

    PRICE_MAX_AGE_SECS: float   = 60.0

    # Variance sizing — shrink when price is far from 0.50
    ENTRY_VAR_MILD_DIST: float  = 0.20      # |p-0.5| > 0.20 → mild shrink
    ENTRY_VAR_HARD_DIST: float  = 0.30      # |p-0.5| > 0.30 → hard shrink
    ENTRY_VAR_MILD_SCALE: float = 0.65
    ENTRY_VAR_HARD_SCALE: float = 0.40
    BELIEF_VOL_MILD: float      = 0.06
    BELIEF_VOL_HARD: float      = 0.12
    BELIEF_VOL_MILD_SCALE: float = 0.70
    BELIEF_VOL_HARD_SCALE: float = 0.40

    # Simulation
    SIM_BALANCE: float          = 1000.0
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
