"""Production API configuration; credential loading is explicit at this boundary."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


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

    # Gemini public WebSocket (4th mid venue; NEAR not listed)
    GEMINI_WS: str = "wss://ws.gemini.com"

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


api_cfg = APIConfig()
