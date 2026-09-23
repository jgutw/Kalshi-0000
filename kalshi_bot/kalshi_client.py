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
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Optional
from urllib.parse import urlparse

import requests

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

from .api_config import api_cfg
from .config import cfg

log = logging.getLogger("kalshi_client")


def _normalize_pem(pem: str) -> str:
    """Restore newlines when .env stores the RSA key as one escaped line."""
    if not pem:
        return pem
    s = pem.strip().strip('"').strip("'")
    if "\\n" in s and s.count("\n") < 2:
        s = s.replace("\\n", "\n")
    return s


def snap_price_to_ranges(
    price: float,
    price_ranges: list[dict],
    *,
    aggressive_up: bool,
) -> float:
    """Snap an order price to the market's valid fixed-point grid."""
    p = Decimal(str(max(0.0001, min(0.9999, float(price)))))
    for band in price_ranges or []:
        if not isinstance(band, dict):
            continue
        try:
            start = Decimal(str(band["start"]))
            end = Decimal(str(band["end"]))
            step = Decimal(str(band["step"]))
        except (KeyError, TypeError, ValueError):
            continue
        if step <= 0 or p < start or p > end:
            continue
        offset = p - start
        n_steps = offset / step
        rounding = ROUND_UP if aggressive_up else ROUND_DOWN
        snapped = start + step * n_steps.to_integral_value(rounding=rounding)
        snapped = min(end, max(start, snapped))
        return float(snapped)
    # Fallback: whole-cent grid
    cents = Decimal(str(price))
    step = Decimal("0.01")
    offset = cents - Decimal("0.00")
    rounding = ROUND_UP if aggressive_up else ROUND_DOWN
    snapped = Decimal("0.00") + step * (offset / step).to_integral_value(rounding=rounding)
    return float(min(Decimal("0.99"), max(Decimal("0.01"), snapped)))


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
                pem = _normalize_pem(private_key_pem) if isinstance(private_key_pem, str) else private_key_pem
                pem_bytes = pem.encode() if isinstance(pem, str) else pem
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
        """True when the contract is still the current tradable window."""
        status = str(market.get("status") or "").lower()
        if status in {"closed", "determined", "settled", "finalized", "inactive"}:
            return False
        close_ts = cls._parse_close_ts(market.get("close_time"))
        if close_ts is not None and close_ts <= (now if now is not None else time.time()) - 1.0:
            return False
        return True

    def find_active_market(self, series_ticker: str) -> Optional[dict]:
        """
        Return the currently tradable 15-min market for a series (e.g. KXBTC15M).

        Kalshi's status=open/active filters still return windows that have already
        closed (settling). Pick the live contract whose close_time is soonest in
        the future — that is the current window, not the next one listed early.
        """
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

        # Current window = soonest future close. If every row is already past
        # close (fallback pool), take the latest close so we are least stale.
        if live:
            pool.sort(key=_close_key)
            return pool[0]
        pool.sort(key=_close_key, reverse=True)
        return pool[0]

    def get_market(self, ticker: str) -> Optional[dict]:
        """Fetch a single market by ticker."""
        data = self._get(f"/markets/{ticker}")
        return data.get("market") if isinstance(data, dict) else None

    def get_orderbook(self, ticker: str, depth: int = 100) -> dict:
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

    @staticmethod
    def _dollar_quote(raw: Any) -> float:
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_yes_mid(self, ticker: str) -> Optional[float]:
        """
        YES probability 0–1 matching the Kalshi UI quote.

        Prefer market yes_bid_dollars / yes_ask_dollars (what the app shows).
        A shallow orderbook_fp snapshot often omits the best bid, so a reconstructed
        mid can sit 10–20¢ away from the displayed price.
        """
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

        market = self.get_market(ticker) or {}
        ranges = list(market.get("price_ranges") or [])
        px = snap_price_to_ranges(
            px,
            ranges,
            aggressive_up=(book_side == "bid"),
        )

        body = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{n:.2f}",
            "price": f"{px:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "exchange_index": int(getattr(cfg, "CRYPTO_EXCHANGE_INDEX", 2)),
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

    def get_balance_detail(self, exchange_index: Optional[int] = None) -> dict[str, Any]:
        """
        available: withdrawable cash (dollars)
        portfolio_value: marked open exposure (dollars)

        When exchange_index is set, reads that shard's balance (required for
        crypto live trading after Kalshi exchange sharding).
        """
        params = {"exchange_index": int(exchange_index)} if exchange_index is not None else None
        data = self._get("/portfolio/balance", params=params)
        if not isinstance(data, dict) or not data:
            return {"available": 0.0, "portfolio_value": 0.0, "raw": {}}
        if data.get("balance_dollars") is not None:
            try:
                available = float(data["balance_dollars"])
            except (TypeError, ValueError):
                available = float(data.get("balance") or 0) / 100.0
        else:
            available = float(data.get("balance") or 0) / 100.0
        # portfolio_value is the same unit as `balance` (integer cents).
        # There is usually no portfolio_value_dollars field. A previous
        # heuristic (only divide when pv > 5x cash) treated 870 cents of a
        # $8.70 position as $870 and tripped live_open_divergence / fake DD.
        if data.get("portfolio_value_dollars") is not None:
            try:
                pv = float(data["portfolio_value_dollars"])
            except (TypeError, ValueError):
                pv = float(data.get("portfolio_value") or 0) / 100.0
        else:
            try:
                pv = float(data.get("portfolio_value") or 0) / 100.0
            except (TypeError, ValueError):
                pv = 0.0
        return {"available": available, "portfolio_value": pv, "raw": data}

    @staticmethod
    def _dollars_to_centicents(amount_usd: float) -> int:
        """Kalshi intra-transfer amounts are in centicents (1/10000 USD)."""
        return max(1, int(round(float(amount_usd) * 10_000)))

    def get_shard_balances(self) -> dict[int, float]:
        """Return available USD per exchange shard from balance_breakdown."""
        data = self._get("/portfolio/balance")
        out: dict[int, float] = {}
        if not isinstance(data, dict):
            return out
        for row in data.get("balance_breakdown") or []:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("exchange_index", 0))
                bal = float(row.get("balance") or 0.0)
            except (TypeError, ValueError):
                continue
            out[idx] = bal
        return out

    def intra_transfer_shards(
        self,
        amount_usd: float,
        *,
        from_shard: int = 0,
        to_shard: int = 2,
    ) -> tuple[bool, str]:
        """Move cash between Kalshi exchange shards (async on Kalshi side)."""
        amt = float(amount_usd)
        if amt <= 0:
            return False, "amount must be > 0"
        body = {
            "source": "event_contract",
            "destination": "event_contract",
            "amount": self._dollars_to_centicents(amt),
            "source_exchange_shard": int(from_shard),
            "destination_exchange_shard": int(to_shard),
        }
        result = self._post("/portfolio/intra_exchange_instance_transfer", body)
        if not result or not result.get("transfer_id"):
            return False, f"transfer failed: {result or 'no response'}"
        return True, str(result["transfer_id"])

    def ensure_crypto_shard_funded(self, reserve_usd: Optional[float] = None) -> tuple[float, str]:
        """
        Preallocate cash on the crypto exchange shard for live order placement.
        Returns (amount_transferred, status_message).
        """
        shard = int(getattr(cfg, "CRYPTO_EXCHANGE_INDEX", 2))
        reserve = float(
            reserve_usd if reserve_usd is not None else getattr(cfg, "CRYPTO_SHARD_RESERVE_USD", 0.50)
        )
        shards = self.get_shard_balances()
        on_crypto = float(shards.get(shard, 0.0))
        on_default = float(shards.get(0, 0.0))
        # Consolidate shard-0 cash onto the crypto shard whenever live trading starts.
        move = max(0.0, on_default - reserve)
        if move < 0.01:
            if on_crypto >= float(getattr(cfg, "LIVE_MIN_AVAILABLE_USD", 2.0)):
                return 0.0, f"crypto shard funded (${on_crypto:.2f})"
            return 0.0, (
                f"insufficient on shard 0 (${on_default:.2f}) to fund crypto shard "
                f"(need >${reserve + cfg.LIVE_MIN_AVAILABLE_USD:.2f})"
            )
        ok, tid = self.intra_transfer_shards(move, from_shard=0, to_shard=shard)
        if not ok:
            return 0.0, tid
        # Transfer is async — brief pause then re-check
        time.sleep(2.0)
        funded = float(self.get_shard_balances().get(shard, 0.0))
        log.warning(
            "Funded crypto shard %d: moved $%.2f (transfer_id=%s) → shard balance $%.2f",
            shard, move, tid, funded,
        )
        return move, f"transferred ${move:.2f} to shard {shard} (id={tid})"

    def get_market_positions(self, exchange_index: Optional[int] = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 200}
        if exchange_index is not None:
            params["exchange_index"] = int(exchange_index)
        data = self._get("/portfolio/positions", params=params)
        if not isinstance(data, dict):
            return []
        return list(data.get("market_positions") or [])

    def get_open_exposure_dollars(self, exchange_index: Optional[int] = None) -> float:
        """Sum of Kalshi market_exposure_dollars on non-zero positions (premium, not MTM)."""
        total = 0.0
        for m in self.get_market_positions(exchange_index=exchange_index):
            try:
                pos = float(m.get("position_fp") or 0.0)
            except (TypeError, ValueError):
                pos = 0.0
            if abs(pos) < 1e-9:
                continue
            try:
                total += abs(float(m.get("market_exposure_dollars") or 0.0))
            except (TypeError, ValueError):
                continue
        return total

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
