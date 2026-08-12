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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
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


@dataclass
class FillResult:
    """Result of a live (or paper) order attempt."""
    ok: bool
    fill_count: int = 0
    entry_price: float = 0.0   # price of purchased side (yes or no), 0–1
    fees: float = 0.0
    cost: float = 0.0          # premium paid (+ fees when known)
    order_id: str = ""
    raw: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.ok)


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
            log.warning(f"POST {path} → {r.status_code}: {r.text[:300]}")
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
        limit_price: Optional[float] = None,
        max_slippage: float = 0.10,
    ) -> FillResult:
        """
        Place an aggressive IOC buy (market-style) via Create Order V2.

        Kalshi V2 quotes the YES book only:
          bid = buy YES, ask = sell YES (= buy NO at 1 - price).
        DRY_RUN: returns a synthetic FillResult at limit_price (truthy).
        """
        side_l = side.lower().strip()
        n = max(1, int(count))
        if cfg.DRY_RUN:
            entry = float(limit_price) if limit_price is not None else 0.50
            entry = max(0.01, min(0.99, entry))
            log.info(f"[SIM] BUY {side_l.upper()} ×{n} | {ticker}")
            return FillResult(
                ok=True,
                fill_count=n,
                entry_price=entry,
                fees=0.0,
                cost=round(entry * n, 4),
                order_id="sim",
            )
        if not api_cfg.KALSHI_API_KEY:
            log.warning("No API key — set KALSHI_API_KEY in .env")
            return FillResult(ok=False)

        slip = max(0.0, min(0.50, float(max_slippage)))

        # Aggressive IOC in YES-book terms so we take liquidity.
        if side_l == "yes":
            book_side = "bid"
            if limit_price is None:
                px = 0.99
            else:
                px = min(0.99, max(0.01, float(limit_price) + slip))
        elif side_l == "no":
            # Buy NO ≈ sell YES at (1 - no_price); lower YES ask = more aggressive.
            book_side = "ask"
            if limit_price is None:
                px = 0.01
            else:
                yes_equiv = 1.0 - float(limit_price)
                px = max(0.01, min(0.99, yes_equiv - slip))
        else:
            log.warning("place_market_order: side must be yes/no, got %r", side)
            return FillResult(ok=False)

        body = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{n:.2f}",
            "price": f"{px:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        result = self._post("/portfolio/events/orders", body)
        if not result:
            return FillResult(ok=False)

        # Legacy nested order
        if result.get("order") and not result.get("order_id"):
            od = result["order"] if isinstance(result.get("order"), dict) else {}
            return FillResult(ok=True, fill_count=n, entry_price=float(limit_price or 0.5),
                              cost=float(limit_price or 0.5) * n, order_id=str(od.get("order_id") or ""),
                              raw=result)

        order_id = str(result.get("order_id") or "")
        try:
            fill = float(result.get("fill_count") or 0.0)
        except (TypeError, ValueError):
            fill = 0.0
        if not order_id or fill <= 0:
            log.warning(
                "Order accepted but unfilled (IOC): %s %s ×%d @%s | %s",
                book_side, side_l, n, body["price"], ticker,
            )
            return FillResult(ok=False, order_id=order_id, raw=result)

        fill_n = max(1, int(round(fill)))
        avg_yes = None
        try:
            if result.get("average_fill_price") is not None:
                avg_yes = float(result["average_fill_price"])
        except (TypeError, ValueError):
            avg_yes = None

        if side_l == "yes":
            entry = avg_yes if avg_yes is not None else float(limit_price or px)
        else:
            # Sold YES at avg_yes ⇒ bought NO at 1 - avg_yes
            if avg_yes is not None:
                entry = 1.0 - avg_yes
            else:
                entry = float(limit_price or (1.0 - px))
        entry = max(0.01, min(0.99, float(entry)))

        fee = 0.0
        try:
            if result.get("average_fee_paid") is not None:
                fee = float(result["average_fee_paid"]) * fill
        except (TypeError, ValueError):
            fee = 0.0
        cost = round(entry * fill_n + fee, 4)

        log.info(
            "LIVE FILL %s %s ×%d entry=%.4f fees=%.4f cost=%.4f avg_yes=%s | %s",
            book_side, side_l.upper(), fill_n, entry, fee, cost,
            result.get("average_fill_price"), ticker,
        )
        return FillResult(
            ok=True,
            fill_count=fill_n,
            entry_price=entry,
            fees=round(fee, 4),
            cost=cost,
            order_id=order_id,
            raw=result,
        )

    # ─── Balance / account ───────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Return available USDC balance in dollars."""
        return float(self.get_balance_detail().get("available") or 0.0)

    def get_balance_detail(self) -> dict[str, Any]:
        """
        available: withdrawable cash (dollars)
        portfolio_value: marked open exposure (dollars)
        """
        data = self._get("/portfolio/balance")
        if not isinstance(data, dict) or not data:
            return {"available": 0.0, "portfolio_value": 0.0, "raw": {}}
        if data.get("balance_dollars") is not None:
            try:
                available = float(data["balance_dollars"])
            except (TypeError, ValueError):
                available = float(data.get("balance") or 0) / 100.0
        else:
            available = float(data.get("balance") or 0) / 100.0
        # portfolio_value from API has been observed in cents
        pv_raw = data.get("portfolio_value")
        try:
            pv = float(pv_raw or 0.0)
        except (TypeError, ValueError):
            pv = 0.0
        # Heuristic: values like 2650 with available ~$60 ⇒ cents
        if pv >= 50 and available > 0 and pv > available * 5:
            pv = pv / 100.0
        elif pv >= 1000 and available < 500:
            pv = pv / 100.0
        return {"available": available, "portfolio_value": pv, "raw": data}

    def get_market_positions(self) -> list[dict]:
        data = self._get("/portfolio/positions", params={"limit": 200})
        if not isinstance(data, dict):
            return []
        return list(data.get("market_positions") or [])

    def get_market_result(self, ticker: str) -> Optional[str]:
        """
        Official market result: 'yes', 'no', or None if not settled yet.
        """
        m = self.get_market(ticker)
        if not isinstance(m, dict):
            return None
        result = m.get("result") or m.get("settlement_result")
        if isinstance(result, str):
            r = result.strip().lower()
            if r in ("yes", "no"):
                return r
        # Some payloads use settlement_value 1/0 on yes
        status = str(m.get("status") or "").lower()
        if status in ("determined", "finalized", "settled"):
            sv = m.get("settlement_value")
            try:
                if sv is not None:
                    return "yes" if float(sv) >= 0.5 else "no"
            except (TypeError, ValueError):
                pass
        return None

    # ─── Series verification ─────────────────────────────────────────────────

    def verify_series(self, series_ticker: str) -> bool:
        """Check that a series exists and has active markets."""
        data = self._get(f"/series/{series_ticker}")
        return isinstance(data, dict) and "series" in data
