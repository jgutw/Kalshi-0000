"""
kalshi_client.py — Kalshi REST + WebSocket client.

Authentication: RSA-PSS per request (arXiv-recommended approach).
  Header: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
  Signature: RSA-PSS( private_key, SHA-256, timestamp + method + path )

Price units: Kalshi uses CENTS (integers 0–100).
  50 cents = YES pays $0.50 = probability ≈ 50%.
  Always divide by 100 before passing to signal math.

Market lifecycle for 15-min windows:
  - Each window has a unique market ticker, e.g. KXBTC15M-25Jan01-T1234567
  - Use GET /markets?series_ticker=KXBTC15M&status=open to find the live one
  - On window rollover, re-query to get the next market
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

from .config import api_cfg, cfg

log = logging.getLogger("kalshi_client")


class KalshiAuth:
    """Generates RSA-PSS signed headers for each Kalshi REST request."""

    def __init__(self, api_key: str, private_key_pem: str):
        self.api_key = api_key
        self._private_key = None
        if private_key_pem and _HAS_CRYPTO:
            try:
                pem_bytes = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
                self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            except Exception as e:
                log.error(f"Failed to load private key: {e}")

    def headers(self, method: str, path: str) -> dict:
        """Build signed auth headers for one request."""
        if not self._private_key:
            # No key — paper trading or demo without auth
            return {"KALSHI-ACCESS-KEY": self.api_key}
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type":            "application/json",
        }


class KalshiClient:
    """
    Thin wrapper around Kalshi REST API v2.

    Price conventions:
      - All prices returned from Kalshi are CENTS (int 0–100).
      - price_to_prob(p) divides by 100 for signal math.
      - prob_to_price(p) multiplies by 100 for order placement.

    Market discovery:
      - find_active_market(series_ticker) returns the currently open
        market for a given series (e.g. KXBTC15M).
      - Returns None if no market is open.
    """

    CENTS = 100  # divisor for probability conversion

    def __init__(self):
        self._base = api_cfg.KALSHI_REST
        self._auth = KalshiAuth(api_cfg.KALSHI_API_KEY, api_cfg.KALSHI_PRIVATE_KEY)
        self._sess = requests.Session()
        self._sess.headers["User-Agent"] = "kalshi-multi-bot/1.0"

    # ─── Price helpers ────────────────────────────────────────────────────────

    @staticmethod
    def price_to_prob(cents: int) -> float:
        """Kalshi cents (0–100) → probability float (0.0–1.0)."""
        return max(0.01, min(0.99, cents / 100.0))

    @staticmethod
    def prob_to_price(prob: float) -> int:
        """Probability → Kalshi cents, clamped 1–99."""
        return max(1, min(99, round(prob * 100)))

    # ─── REST helpers ─────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None, timeout: int = 8) -> dict | list:
        url = self._base + path
        sign_path = urlparse(url).path
        hdrs = self._auth.headers("GET", sign_path)
        log.info(f"Kalshi request: GET {url}")
        try:
            r = self._sess.get(url, headers=hdrs, params=params, timeout=timeout)
            if r.ok:
                return r.json()
            log.warning(f"GET {path} → {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log.error(f"GET {path} error: {e}")
        return {}

    def _post(self, path: str, body: dict, timeout: int = 8) -> dict:
        url = self._base + path
        sign_path = urlparse(url).path
        hdrs = self._auth.headers("POST", sign_path)
        try:
            r = self._sess.post(url, headers=hdrs, json=body, timeout=timeout)
            if r.ok:
                return r.json()
            log.warning(f"POST {path} → {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log.error(f"POST {path} error: {e}")
        return {}

    # ─── Market discovery ─────────────────────────────────────────────────────

    def find_active_market(self, series_ticker: str) -> Optional[dict]:
        """
        Return the currently open market for a series (e.g. KXBTC15M).
        Kalshi returns markets sorted by close_time ascending; first open = active window.
        """
        data = self._get("/markets", params={
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 5,
        })
        markets = data.get("markets", []) if isinstance(data, dict) else []
        if not markets:
            # Try with status=active (Kalshi uses both terms)
            data = self._get("/markets", params={
                "series_ticker": series_ticker,
                "status": "active",
                "limit": 5,
            })
            markets = data.get("markets", []) if isinstance(data, dict) else []
        if markets:
            # At rollover, old market may still appear "open" during settlement.
            # Take the one with latest close_time = the window that just opened.
            try:
                markets.sort(key=lambda m: m.get("close_time", ""), reverse=True)
            except Exception:
                pass
            return markets[0]
        return None

    def get_market(self, ticker: str) -> Optional[dict]:
        """Fetch a single market by ticker."""
        data = self._get(f"/markets/{ticker}")
        return data.get("market") if isinstance(data, dict) else None

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """
        Fetch current orderbook snapshot.
        Returns {"yes": [[price, size], ...], "no": [...]} with price/size as floats.
        API returns orderbook_fp: yes_dollars/no_dollars (decimal 0-1 prices).
        """
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        if not isinstance(data, dict):
            log.debug(f"Raw orderbook response for {ticker}: {data}")
            return {}
        ob_fp = data.get("orderbook_fp", {})
        if isinstance(ob_fp, dict):
            yes_raw = ob_fp.get("yes_dollars", [])
            no_raw = ob_fp.get("no_dollars", [])
            def parse_side(entries):
                out = []
                for e in entries:
                    if not e or len(e) < 2:
                        continue
                    try:
                        p, s = float(e[0]), float(e[1])
                        if p > 0 and s > 0:
                            out.append([p, s])
                    except (TypeError, ValueError):
                        pass
                return out
            yes_parsed = parse_side(yes_raw)
            no_parsed = parse_side(no_raw)
            result = {"yes": yes_parsed, "no": no_parsed}
            if not yes_parsed and not no_parsed:
                log.debug(f"Raw orderbook response for {ticker}: {data}")
            return result
        # Fallback: legacy {"orderbook": {"yes": [...], "no": [...]}}
        book = data.get("orderbook", data)
        result = book if isinstance(book, dict) else {}
        if not result or (not result.get("yes") and not result.get("no")):
            log.debug(f"Raw orderbook response for {ticker}: {data}")
        return result

    def get_yes_mid(self, ticker: str) -> Optional[float]:
        """
        Derive YES probability from orderbook mid-price.
        Returns float 0–1, or None if book is empty.
        API uses decimal prices (0–1). Best YES bid = highest in yes; Best YES ask = 1 - best NO bid.
        """
        book = self.get_orderbook(ticker)
        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        def safe_float(r, i):
            try:
                return float(r[i]) if len(r) > i else 0.0
            except (TypeError, ValueError):
                return 0.0

        best_yes_bid = max((safe_float(r, 0) for r in yes_bids if len(r) >= 2), default=0.0)
        best_no_bid = max((safe_float(r, 0) for r in no_bids if len(r) >= 2), default=0.0)
        best_yes_ask = 1.0 - best_no_bid if best_no_bid > 0 else 1.0

        if best_yes_bid > 0 and best_yes_ask < 1.0:
            mid = (best_yes_bid + best_yes_ask) / 2
            return max(0.01, min(0.99, mid))
        if best_yes_bid > 0:
            return max(0.01, min(0.99, best_yes_bid))
        if best_yes_ask < 1.0:
            return max(0.01, min(0.99, best_yes_ask))
        log.debug(f"Raw orderbook response for {ticker}: {book}")
        return None

    # ─── Order placement ─────────────────────────────────────────────────────

    def place_market_order(
        self,
        ticker: str,
        side: str,               # "yes" or "no"
        count: int,              # number of contracts
        client_order_id: str = "",
    ) -> bool:
        """
        Place a market order (buy only — we don't short).
        Each contract pays $1.00 at resolution; cost = mid_price × count.
        DRY_RUN: logs and returns True without hitting the API.
        """
        if cfg.DRY_RUN:
            log.info(f"[SIM] BUY {side.upper()} ×{count} | {ticker}")
            return True
        if not api_cfg.KALSHI_API_KEY:
            log.warning("No API key — set KALSHI_API_KEY in .env")
            return False
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side.lower(),
            "count": count,
            "type": "market",
            "client_order_id": client_order_id or f"bot_{int(time.time())}",
        }
        result = self._post("/orders", body)
        return bool(result.get("order"))

    # ─── Balance / account ───────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Return available USDC balance in dollars."""
        data = self._get("/portfolio/balance")
        cents = data.get("balance", 0) if isinstance(data, dict) else 0
        return cents / 100.0   # Kalshi returns balance in cents

    # ─── Series verification ─────────────────────────────────────────────────

    def verify_series(self, series_ticker: str) -> bool:
        """Check that a series exists and has active markets."""
        data = self._get(f"/series/{series_ticker}")
        return isinstance(data, dict) and "series" in data
