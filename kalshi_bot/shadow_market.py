"""GET-only Kalshi market data for Shadow.

Read semantics follow KalshiClient. This module does not import that client,
api_config, or any order/transfer method. The signer hardcodes GET.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger("kalshi_bot.shadow_market")

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


class BookReadError(RuntimeError):
    """The execution-book GET failed or returned an unusable body."""


class BookIdentityError(RuntimeError):
    """The response named a different market than the frozen ticker."""


def _normalize_pem(pem: str) -> str:
    if not pem:
        return pem
    s = pem.strip().strip('"').strip("'")
    if "\\n" in s and s.count("\n") < 2:
        s = s.replace("\\n", "\n")
    return s


def sign_get(api_key: str, private_key_pem: str, path: str) -> dict:
    """RSA-PSS headers for one GET. There is no method argument."""
    if not private_key_pem or not _HAS_CRYPTO:
        return {"KALSHI-ACCESS-KEY": api_key}
    pem = _normalize_pem(private_key_pem)
    pem_bytes = pem.encode() if isinstance(pem, str) else pem
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    ts = str(int(time.time() * 1000))
    msg = (ts + "GET" + path).encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


class ReadOnlyKalshi:
    """Market discovery, quotes, and order books. Writes are absent, not disabled."""

    __slots__ = ("_base", "_api_key", "_private_key", "_transport", "_last_get_ok")

    def __init__(self, base_url: str, api_key: str, private_key_pem: str,
                 transport: Callable[..., Any]):
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._private_key = private_key_pem
        self._transport = transport
        self._last_get_ok = False

    def _get(self, path: str, params: Optional[dict] = None, timeout: int = 8) -> dict | list:
        url = self._base + path
        sign_path = urlparse(url).path
        headers = sign_get(self._api_key, self._private_key, sign_path)
        self._last_get_ok = False
        log.info("Shadow Kalshi read: GET %s", path)
        try:
            status, body = self._transport(url, headers=headers, params=params, timeout=timeout)
        except Exception as exc:
            log.error("GET %s error: %s", path, exc)
            return {}
        if isinstance(status, int) and 200 <= status < 300 and isinstance(body, (dict, list)):
            self._last_get_ok = True
            return body
        log.warning("GET %s → %s", path, status)
        return {}

    @staticmethod
    def _parse_close_ts(raw: Any) -> Optional[float]:
        if not raw:
            return None
        s = str(raw).strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            return None

    @classmethod
    def _market_is_live(cls, market: dict, now: Optional[float] = None) -> bool:
        status = str(market.get("status") or "").lower()
        if status in {"closed", "determined", "settled", "finalized", "inactive"}:
            return False
        close_ts = cls._parse_close_ts(market.get("close_time"))
        if close_ts is not None and close_ts <= (now if now is not None else time.time()) - 1.0:
            return False
        return True

    def find_active_market(self, series_ticker: str) -> Optional[dict]:
        markets: list[dict] = []
        for status in ("open", "active"):
            data = self._get("/markets", params={
                "series_ticker": series_ticker,
                "status": status,
                "limit": 20,
            })
            batch = data.get("markets", []) if isinstance(data, dict) else []
            if batch:
                markets = list(batch)
                break
        if not markets:
            return None
        now = time.time()
        live = [m for m in markets if isinstance(m, dict) and self._market_is_live(m, now)]
        pool = live or [m for m in markets if isinstance(m, dict)]
        if not pool:
            return None

        def _close_key(m: dict) -> float:
            ts = self._parse_close_ts(m.get("close_time"))
            return ts if ts is not None else 0.0

        if live:
            pool.sort(key=_close_key)
            return pool[0]
        pool.sort(key=_close_key, reverse=True)
        return pool[0]

    def get_market(self, ticker: str) -> Optional[dict]:
        data = self._get(f"/markets/{ticker}")
        return data.get("market") if isinstance(data, dict) else None

    def verify_series(self, series_ticker: str) -> bool:
        data = self._get(f"/series/{series_ticker}")
        return isinstance(data, dict) and "series" in data

    @staticmethod
    def _parse_orderbook(data: dict) -> dict:
        ob_fp = data.get("orderbook_fp", {})
        if isinstance(ob_fp, dict):
            def parse_side(entries):
                out = []
                for e in entries or []:
                    if not e or len(e) < 2:
                        continue
                    try:
                        price, size = float(e[0]), float(e[1])
                        if price > 0 and size > 0:
                            out.append([price, size])
                    except (TypeError, ValueError):
                        pass
                return out
            return {
                "yes": parse_side(ob_fp.get("yes_dollars", [])),
                "no": parse_side(ob_fp.get("no_dollars", [])),
            }
        book = data.get("orderbook", data)
        return book if isinstance(book, dict) else {}

    def get_orderbook(self, ticker: str, depth: int = 100) -> dict:
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        if not isinstance(data, dict):
            return {}
        reported = data.get("ticker") or data.get("market_ticker")
        if reported and reported != ticker:
            raise BookIdentityError(f"book ticker {reported} != {ticker}")
        return self._parse_orderbook(data)

    def fetch_execution_book(self, ticker: str, depth: int = 100) -> dict:
        """One execution observation. Failure raises; it does not retry."""
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        if not self._last_get_ok or not isinstance(data, dict) or not data:
            raise BookReadError(f"execution book unavailable for {ticker}")
        reported = data.get("ticker") or data.get("market_ticker")
        if reported and reported != ticker:
            raise BookIdentityError(f"book ticker {reported} != {ticker}")
        book = self._parse_orderbook(data)
        if not isinstance(book, dict) or ("yes" not in book and "no" not in book):
            raise BookReadError(f"execution book malformed for {ticker}")
        return book

    @staticmethod
    def _dollar_quote(raw: Any) -> float:
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_yes_mid(self, ticker: str) -> Optional[float]:
        m = self.get_market(ticker) or {}
        bid = self._dollar_quote(m.get("yes_bid_dollars"))
        ask = self._dollar_quote(m.get("yes_ask_dollars"))
        last = self._dollar_quote(m.get("last_price_dollars"))
        if bid > 0 and 0.0 < ask < 1.0:
            return max(0.01, min(0.99, (bid + ask) / 2.0))
        if bid > 0:
            return max(0.01, min(0.99, bid))
        if 0.0 < ask < 1.0:
            return max(0.01, min(0.99, ask))
        if 0.0 < last < 1.0:
            return max(0.01, min(0.99, last))

        book = self.get_orderbook(ticker)
        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        def safe_float(row, index):
            try:
                return float(row[index]) if len(row) > index else 0.0
            except (TypeError, ValueError):
                return 0.0

        best_yes_bid = max((safe_float(row, 0) for row in yes_bids if len(row) >= 2), default=0.0)
        best_no_bid = max((safe_float(row, 0) for row in no_bids if len(row) >= 2), default=0.0)
        best_yes_ask = 1.0 - best_no_bid if best_no_bid > 0 else 1.0
        if best_yes_bid > 0 and best_yes_ask < 1.0:
            mid = (best_yes_bid + best_yes_ask) / 2
            return max(0.01, min(0.99, mid))
        if best_yes_bid > 0:
            return max(0.01, min(0.99, best_yes_bid))
        if best_yes_ask < 1.0:
            return max(0.01, min(0.99, best_yes_ask))
        return None
