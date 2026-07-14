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
    # Active profile label (written to logs/session_meta.json)
    CONFIG_PROFILE: str         = "lower_size_more_entries"

    # Kelly / position sizing — smaller, more frequent bets
    KELLY_FRACTION: float       = 0.20
    MIN_EDGE_PCT: float         = 0.025     # keep; market orders need edge cushion
    MAX_POS_PCT: float          = 0.02      # 2% of bankroll per trade
    MIN_TRADE_USD: float        = 5.0

    # Circuit breaker — per-asset cooldown so one bad streak doesn't freeze all assets
    MAX_CONSEC_LOSSES: int      = 4
    COOLDOWN_MINUTES: float     = 25.0
    PER_ASSET_CIRCUIT_BREAKER: bool = True
    MAX_DAILY_LOSS_PCT: float   = 0.12
    DAILY_RESET_HOUR_UTC: int   = 0

    # Entry gates
    LAG_CONFIDENCE_MIN: float   = 0.22      # lag_arb strategy only (see lag_arb.py)
    LAG_ABSENT_MIN: float       = 0.15      # global floor — only enforced for lag_arb
    CWM_MIN: float              = 0.027
    ALPHA_EDGE_MIN: float       = 0.06      # was 0.10; too high for 15m binaries
    ALPHA_EDGE_BAND_LOW: float  = 0.04
    ALPHA_EDGE_LAG_MIN: float   = 0.30      # was hardcoded 0.50 in asset_engine
    MIN_CONVICTION: int         = 3
    SHARPE_MIN: float           = 1.0
    SHARPE_MIN_TRADES: int      = 25

    # Volatility filter
    VOL_HI: float               = 1.85
    VOL_MID: float               = 0.60

    # Early exit — looser stops; R6 showed MTM stops crystallized losses early
    EARLY_EXIT_ENABLED: bool            = True
    EARLY_EXIT_MIN_HOLD_SECS: float     = 180.0   # was 90; let position develop
    EARLY_EXIT_LOSS_FRACTION: float     = 0.70    # was 0.50
    EARLY_EXIT_SPOT_ADVERSE_BPS: float  = 10.0
    EARLY_EXIT_MIN_TIME_LEFT_SECS: float = 180.0
    EARLY_EXIT_MODERATE_LOSS_FRAC: float = 0.45   # was 0.30

    # Structural model: floor vol to prevent exploding z when realized vol is tiny (early window)
    MIN_STRUCTURAL_VOL: float   = 0.15     # 15% annualized; crypto typically 20–80%

    # p_base boundary: reject if outside [P_BASE_MIN, P_BASE_MAX]; loosened to 0.02/0.98 when structural_model_invalid dominates
    P_BASE_MIN: float           = 0.05
    P_BASE_MAX: float           = 0.95

    # Hawkes process (arXiv:2408.03594)
    # Half-life ≈ ln(2)/HAWKES_DECAY seconds
    # At 15-min windows we want ~15s half-life → HAWKES_DECAY = ln(2)/15 ≈ 0.046
    # OLD value was 3.0 (0.23s half-life) which decayed before each poll
    HAWKES_DECAY: float         = 0.046
    HAWKES_ALPHA: float         = 0.8

    # OFI rolling window
    OFI_WINDOW_SECS: int       = 120       # 2-min window (wider for 15-min markets)

    # Logit tracker
    LOGIT_EWM_ALPHA: float      = 0.15

    # Window timing
    WINDOW_SECS: int            = 900       # 15 minutes
    SKIP_OPEN_SECS: int         = 20        # was 45; capture early dislocations
    SKIP_CLOSE_SECS: int       = 45        # was 30; more caution near expiry

    # Staleness guard — reject Kalshi prices older than this
    PRICE_MAX_AGE_SECS: float   = 30.0

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
    # Set KALSHI_BASE_URL in .env to override (e.g. if API has moved)
    # Set KALSHI_DEMO=true for demo environment
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


# ─── Micro alpha model overrides ─────────────────────────────────────────────
# Per-asset coefficient overrides. Empty = all assets use defaults.
# Example: {"BTC": {"lag_signal": 0.50}, "ETH": {"lag_signal": 0.35}}
MICRO_ALPHA_OVERRIDES: dict = {}

# ─── Macro blackout windows (UTC weekday, hour, minute) ───────────────────────
# arXiv:2508.06788 — avoid trading around macro announcements
BLACKOUT_WINDOWS = [
    (2, 18,  0),   # FOMC Wednesday 18:00 UTC
    (1, 12, 30),   # CPI Tuesday 12:30 UTC
    (4, 12, 30),   # NFP Friday 12:30 UTC
]
BLACKOUT_HALF_WIDTH_SECS = 300


# ─── Shared singletons ───────────────────────────────────────────────────────
cfg     = TradingConfig()
api_cfg = APIConfig()
