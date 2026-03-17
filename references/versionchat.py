"""
starter_polymarket_bot.py

Research-first Polymarket bot:
- BTC 5m / 15m directional paper trading
- Weather bucket paper trading
- Event/news Bayesian paper trading
- Shared deterministic risk engine
- Paper portfolio + logs

IMPORTANT:
- Starts in paper mode only
- Replace stubs with your preferred data providers
- Add live Polymarket execution only after calibration

Python 3.11+
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import random
import statistics
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

# =========================
# CONFIG
# =========================

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

PAPER_STARTING_CAPITAL = 10_000.0
MAX_RISK_PER_TRADE = 0.01          # 1% of equity max
MAX_OPEN_RISK = 0.05               # 5% total open risk
MAX_DRAWDOWN = 0.08                # stop new trades at 8%
MIN_EDGE = 0.04                    # 4% minimum edge
FRACTIONAL_KELLY = 0.25            # quarter Kelly
MAX_NOTIONAL_PCT_OF_EVENT_VOL = 0.02
MIN_ORDERBOOK_SUM_LIQ = 500.0      # crude liquidity filter
VOL_HALT_SIGMA = 0.80              # example realized vol halt
LOG_FILE = "paper_trades.jsonl"

# Optional weather locations
NWS_ENDPOINTS = {
    "nyc": "https://api.weather.gov/gridpoints/OKX/37,39/forecast/hourly",
    "chicago": "https://api.weather.gov/gridpoints/LOT/66,77/forecast/hourly",
    "miami": "https://api.weather.gov/gridpoints/MFL/106,51/forecast/hourly",
}

STATION_IDS = {
    "nyc": "KLGA",
    "chicago": "KORD",
    "miami": "KMIA",
}

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]


# =========================
# DATA MODELS
# =========================

@dataclass
class Market:
    market_id: str
    event_id: str
    question: str
    slug: str
    active: bool
    closed: bool
    enable_order_book: bool
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    volume: float = 0.0
    category: str = ""
    token_ids: list[str] = field(default_factory=list)
    fees_enabled: bool = False


@dataclass
class Signal:
    strategy: str
    market_id: str
    question: str
    side: str                   # "BUY_YES" or "BUY_NO"
    p_model: float
    p_mkt_yes: float
    edge: float
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    market_id: str
    strategy: str
    question: str
    side: str
    entry_price: float
    size_dollars: float
    shares: float
    p_model_at_entry: float
    opened_at: float
    stop_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Portfolio:
    cash: float = PAPER_STARTING_CAPITAL
    equity_peak: float = PAPER_STARTING_CAPITAL
    positions: dict[str, Position] = field(default_factory=dict)
    closed_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0

    def equity(self, mark_prices: dict[str, float]) -> float:
        total = self.cash
        for pos in self.positions.values():
            mark = mark_prices.get(pos.market_id, pos.entry_price)
            if pos.side == "BUY_YES":
                total += pos.shares * mark
            else:
                no_mark = 1.0 - mark
                total += pos.shares * no_mark
        return total

    def drawdown(self, mark_prices: dict[str, float]) -> float:
        eq = self.equity(mark_prices)
        self.equity_peak = max(self.equity_peak, eq)
        return 0.0 if self.equity_peak <= 0 else (self.equity_peak - eq) / self.equity_peak


# =========================
# UTILITIES
# =========================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def log_jsonl(payload: dict[str, Any]) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def safe_get(url: str, params: Optional[dict[str, Any]] = None, timeout: int = 10) -> Any:
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "starter-pm-bot/1.0"})
    r.raise_for_status()
    return r.json()


# =========================
# POLYMARKET DISCOVERY
# =========================

class PolymarketDiscovery:
    def fetch_active_markets(self, limit: int = 200) -> list[Market]:
        """
        Uses Gamma /markets. In production, you may prefer /events for broader scans.
        """
        raw = safe_get(
            f"{GAMMA_BASE}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
        )

        markets: list[Market] = []
        for m in raw:
            outcome_prices = []
            try:
                # Gamma often returns JSON-encoded list for outcome prices
                op = m.get("outcomePrices", [])
                outcome_prices = json.loads(op) if isinstance(op, str) else op
                outcome_prices = [float(x) for x in outcome_prices]
            except Exception:
                outcome_prices = []

            token_ids = []
            try:
                clob_ids = m.get("clobTokenIds", [])
                token_ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
            except Exception:
                token_ids = []

            markets.append(
                Market(
                    market_id=str(m.get("id", "")),
                    event_id=str(m.get("eventId", "")),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    active=bool(m.get("active", False)),
                    closed=bool(m.get("closed", False)),
                    enable_order_book=bool(m.get("enableOrderBook", False)),
                    outcomes=m.get("outcomes", []) or [],
                    outcome_prices=outcome_prices,
                    volume=float(m.get("volume", 0.0) or 0.0),
                    category=str(m.get("category", "") or ""),
                    token_ids=token_ids,
                    fees_enabled=bool(m.get("feesEnabled", False)),
                )
            )
        return markets


# =========================
# SIMPLE MARKET STATE CACHE
# =========================

class MarketState:
    def __init__(self) -> None:
        self.yes_prices: dict[str, float] = {}
        self.market_liquidity: dict[str, float] = defaultdict(lambda: 0.0)
        self.event_volume: dict[str, float] = defaultdict(lambda: 0.0)
        self.returns_1m: deque[float] = deque(maxlen=240)

    def set_price(self, market_id: str, yes_price: float) -> None:
        self.yes_prices[market_id] = clamp(yes_price, 0.001, 0.999)

    def get_price(self, market_id: str) -> Optional[float]:
        return self.yes_prices.get(market_id)

    def realized_vol(self) -> float:
        if len(self.returns_1m) < 10:
            return 0.0
        return statistics.pstdev(self.returns_1m)


# =========================
# RISK ENGINE
# =========================

class RiskEngine:
    def __init__(self, portfolio: Portfolio, market_state: MarketState) -> None:
        self.portfolio = portfolio
        self.market_state = market_state

    def kelly_fraction(self, p: float, price: float) -> float:
        """
        Kelly for binary contract bought at price.
        EV per $1 share = p - price.
        Approx full Kelly = (p - price) / (1 - price)
        """
        denom = max(1e-9, 1.0 - price)
        full = (p - price) / denom
        return max(0.0, full)

    def max_trade_dollars(self, signal: Signal, market_volume: float) -> float:
        mark_prices = self.market_state.yes_prices
        equity = self.portfolio.equity(mark_prices)

        # Hard drawdown stop
        if self.portfolio.drawdown(mark_prices) >= MAX_DRAWDOWN:
            return 0.0

        # Volatility stop
        if self.market_state.realized_vol() > VOL_HALT_SIGMA:
            return 0.0

        # Edge threshold
        if abs(signal.edge) < MIN_EDGE:
            return 0.0

        # Kelly
        price = signal.p_mkt_yes if signal.side == "BUY_YES" else (1.0 - signal.p_mkt_yes)
        kelly = self.kelly_fraction(signal.p_model if signal.side == "BUY_YES" else (1.0 - signal.p_model), price)
        frac = FRACTIONAL_KELLY * kelly

        # Risk cap
        frac = min(frac, MAX_RISK_PER_TRADE)

        # Event volume cap
        vol_cap = market_volume * MAX_NOTIONAL_PCT_OF_EVENT_VOL

        # Open-risk cap
        open_alloc = sum(p.size_dollars for p in self.portfolio.positions.values())
        remaining_open_capacity = max(0.0, equity * MAX_OPEN_RISK - open_alloc)

        return max(0.0, min(equity * frac, vol_cap, remaining_open_capacity))

    def approve(self, signal: Signal, market: Market) -> tuple[bool, str, float]:
        if not market.enable_order_book:
            return False, "market_not_orderbook_enabled", 0.0

        if market.volume < MIN_ORDERBOOK_SUM_LIQ:
            return False, "market_too_illiquid", 0.0

        dollars = self.max_trade_dollars(signal, market.volume)
        if dollars <= 0:
            return False, "risk_engine_rejected", 0.0

        return True, "approved", dollars


# =========================
# PAPER EXECUTION
# =========================

class PaperExecutor:
    def __init__(self, portfolio: Portfolio, market_state: MarketState) -> None:
        self.portfolio = portfolio
        self.market_state = market_state

    def open_position(self, signal: Signal, dollars: float) -> None:
        yes_price = signal.p_mkt_yes
        entry_price = yes_price if signal.side == "BUY_YES" else (1.0 - yes_price)
        if entry_price <= 0 or dollars > self.portfolio.cash:
            return

        shares = dollars / entry_price
        self.portfolio.cash -= dollars
        self.portfolio.trade_count += 1

        self.portfolio.positions[signal.market_id] = Position(
            market_id=signal.market_id,
            strategy=signal.strategy,
            question=signal.question,
            side=signal.side,
            entry_price=entry_price,
            size_dollars=dollars,
            shares=shares,
            p_model_at_entry=signal.p_model,
            opened_at=time.time(),
            metadata=signal.metadata,
        )

        log_jsonl({
            "ts": utc_now().isoformat(),
            "type": "OPEN",
            "market_id": signal.market_id,
            "strategy": signal.strategy,
            "side": signal.side,
            "entry_price": entry_price,
            "dollars": dollars,
            "shares": shares,
            "p_model": signal.p_model,
            "p_mkt_yes": signal.p_mkt_yes,
            "edge": signal.edge,
            "metadata": signal.metadata,
        })

    def maybe_close_positions(self) -> None:
        """
        Example exit rules:
        - close if edge compresses
        - close if time stop hits
        - close if mark moved favorably enough
        """
        to_close: list[str] = []
        for market_id, pos in self.portfolio.positions.items():
            yes_mark = self.market_state.get_price(market_id)
            if yes_mark is None:
                continue

            current_leg_price = yes_mark if pos.side == "BUY_YES" else (1.0 - yes_mark)
            age_sec = time.time() - pos.opened_at

            # Simple exits
            pnl_pct = (current_leg_price - pos.entry_price) / pos.entry_price
            if pnl_pct >= 0.15:
                pos.stop_reason = "take_profit"
                to_close.append(market_id)
            elif pnl_pct <= -0.10:
                pos.stop_reason = "stop_loss"
                to_close.append(market_id)
            elif age_sec >= 60 * 15:
                pos.stop_reason = "time_stop"
                to_close.append(market_id)

        for market_id in to_close:
            self.close_position(market_id)

    def close_position(self, market_id: str) -> None:
        pos = self.portfolio.positions.pop(market_id, None)
        if not pos:
            return

        yes_mark = self.market_state.get_price(market_id)
        if yes_mark is None:
            yes_mark = pos.entry_price if pos.side == "BUY_YES" else (1.0 - pos.entry_price)

        exit_price = yes_mark if pos.side == "BUY_YES" else (1.0 - yes_mark)
        proceeds = pos.shares * exit_price
        pnl = proceeds - pos.size_dollars
        self.portfolio.cash += proceeds
        self.portfolio.closed_pnl += pnl

        if pnl >= 0:
            self.portfolio.win_count += 1
        else:
            self.portfolio.loss_count += 1

        log_jsonl({
            "ts": utc_now().isoformat(),
            "type": "CLOSE",
            "market_id": pos.market_id,
            "strategy": pos.strategy,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "dollars_in": pos.size_dollars,
            "proceeds": proceeds,
            "pnl": pnl,
            "reason": pos.stop_reason,
        })


# =========================
# BTC SIGNAL ENGINE
# =========================

class BTCSignalEngine:
    """
    Starter version:
    - fetch BTC price from a liquid exchange REST endpoint placeholder
    - build a fast microstructure-style probability estimate
    - compare to Polymarket BTC up/down markets
    """

    def __init__(self) -> None:
        self.price_window = deque(maxlen=120)
        self.volume_window = deque(maxlen=120)

    def fetch_btc_spot(self) -> tuple[float, float]:
        """
        Placeholder: replace with Binance/Coinbase websocket in production.
        Returns (price, synthetic volume signal).
        """
        # Example using Coinbase spot endpoint as a simple public source
        raw = safe_get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        price = float(raw["data"]["amount"])
        volume_signal = random.uniform(0.8, 1.2)  # placeholder
        return price, volume_signal

    def update(self) -> None:
        price, volume_signal = self.fetch_btc_spot()
        self.price_window.append(price)
        self.volume_window.append(volume_signal)

    def estimate_probability_up(self, horizon: str = "5m") -> float:
        if len(self.price_window) < 20:
            return 0.50

        recent = list(self.price_window)
        rets = []
        for i in range(1, len(recent)):
            rets.append((recent[i] - recent[i - 1]) / recent[i - 1])

        momentum = sum(rets[-5:])
        short_vol = statistics.pstdev(rets[-20:]) if len(rets) >= 20 else 0.0
        vol_bias = statistics.mean(self.volume_window) - 1.0

        # Simple score: momentum + volume - volatility penalty
        score = (momentum * 20.0) + (vol_bias * 0.6) - (short_vol * 10.0)

        # Horizon adjustment
        if horizon == "15m":
            score *= 1.15

        # Map to probability
        p = sigmoid(score)
        return clamp(p, 0.05, 0.95)

    def market_matches(self, market: Market) -> Optional[str]:
        q = market.question.lower()
        if "bitcoin" not in q and "btc" not in q:
            return None
        if "5 min" in q or "5-minute" in q or "5m" in q:
            return "5m"
        if "15 min" in q or "15-minute" in q or "15m" in q:
            return "15m"
        return None

    def generate_signal(self, market: Market, yes_price: float) -> Optional[Signal]:
        horizon = self.market_matches(market)
        if not horizon:
            return None

        p_model = self.estimate_probability_up(horizon)
        edge = p_model - yes_price

        if abs(edge) < MIN_EDGE:
            return None

        side = "BUY_YES" if edge > 0 else "BUY_NO"
        return Signal(
            strategy=f"btc_{horizon}",
            market_id=market.market_id,
            question=market.question,
            side=side,
            p_model=p_model,
            p_mkt_yes=yes_price,
            edge=edge,
            confidence=abs(edge),
            metadata={"horizon": horizon},
        )


# =========================
# WEATHER SIGNAL ENGINE
# =========================

class WeatherSignalEngine:
    def get_daily_max(self, city_slug: str) -> dict[str, int]:
        headers = {"User-Agent": "starter-pm-weather-bot/1.0"}
        daily_max: dict[str, int] = {}

        # observations
        station_id = STATION_IDS[city_slug]
        obs_url = f"https://api.weather.gov/stations/{station_id}/observations?limit=48"
        try:
            r = requests.get(obs_url, timeout=10, headers=headers)
            r.raise_for_status()
            for obs in r.json().get("features", []):
                props = obs["properties"]
                date_str = props.get("timestamp", "")[:10]
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is not None:
                    temp_f = round(temp_c * 9 / 5 + 32)
                    daily_max[date_str] = max(daily_max.get(date_str, -999), temp_f)
        except Exception:
            pass

        # forecast
        try:
            r = requests.get(NWS_ENDPOINTS[city_slug], timeout=10, headers=headers)
            r.raise_for_status()
            periods = r.json()["properties"]["periods"]
            for p in periods:
                date_str = p["startTime"][:10]
                temp = int(p["temperature"])
                if p.get("temperatureUnit") == "C":
                    temp = round(temp * 9 / 5 + 32)
                daily_max[date_str] = max(daily_max.get(date_str, -999), temp)
        except Exception:
            pass

        return daily_max

    def parse_bucket(self, q: str) -> Optional[tuple[int, int]]:
        import re
        ql = q.lower()
        if "or below" in ql:
            m = re.search(r"(\d+)°f or below", ql)
            if m:
                return (-999, int(m.group(1)))
        if "or higher" in ql:
            m = re.search(r"(\d+)°f or higher", ql)
            if m:
                return (int(m.group(1)), 999)
        m = re.search(r"between (\d+)-(\d+)°f", ql)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None

    def infer_city(self, question: str) -> Optional[str]:
        q = question.lower()
        for city_slug in NWS_ENDPOINTS.keys():
            if city_slug in q:
                return city_slug
        if "new york city" in q:
            return "nyc"
        return None

    def bucket_probability(self, forecast_temp: int, bucket: tuple[int, int]) -> float:
        """
        Crude probabilistic model:
        centered around forecast with fixed sigma.
        You should replace this with calibrated forecast distribution.
        """
        lo, hi = bucket
        sigma = 2.0

        def cdf(x: float) -> float:
            return 0.5 * (1 + math.erf((x - forecast_temp) / (sigma * math.sqrt(2))))

        return clamp(cdf(hi + 0.5) - cdf(lo - 0.5), 0.01, 0.99)

    def generate_signal(self, market: Market, yes_price: float) -> Optional[Signal]:
        q = market.question.lower()
        if "highest temperature" not in q:
            return None

        city_slug = self.infer_city(q)
        if not city_slug:
            return None

        bucket = self.parse_bucket(market.question)
        if not bucket:
            return None

        daily = self.get_daily_max(city_slug)
        today = utc_now().strftime("%Y-%m-%d")
        forecast_temp = daily.get(today)
        if forecast_temp is None:
            return None

        p_model = self.bucket_probability(forecast_temp, bucket)
        edge = p_model - yes_price
        if abs(edge) < MIN_EDGE:
            return None

        side = "BUY_YES" if edge > 0 else "BUY_NO"
        return Signal(
            strategy="weather",
            market_id=market.market_id,
            question=market.question,
            side=side,
            p_model=p_model,
            p_mkt_yes=yes_price,
            edge=edge,
            confidence=abs(edge),
            metadata={"city": city_slug, "forecast_temp": forecast_temp, "bucket": bucket},
        )


# =========================
# EVENT / NEWS BAYES ENGINE
# =========================

class EventBayesEngine:
    """
    Placeholder event engine:
    prior + simple evidence updates.
    Replace headline_score() with a real news/NLP pipeline later.
    """

    def headline_score(self, question: str) -> float:
        q = question.lower()
        bullish_words = ["approved", "confirmed", "wins", "passes", "launched", "yes"]
        bearish_words = ["denied", "rejected", "fails", "no", "delay", "lawsuit"]
        s = 0.0
        for w in bullish_words:
            if w in q:
                s += 0.1
        for w in bearish_words:
            if w in q:
                s -= 0.1
        return s

    def bayes_update(self, prior: float, evidence_score: float) -> float:
        """
        Converts score into likelihood ratio.
        """
        odds = prior / max(1e-9, 1 - prior)
        lr = math.exp(evidence_score)
        post_odds = odds * lr
        post = post_odds / (1 + post_odds)
        return clamp(post, 0.01, 0.99)

    def generate_signal(self, market: Market, yes_price: float) -> Optional[Signal]:
        q = market.question.lower()
        if any(x in q for x in ["temperature", "bitcoin", "btc", "ethereum", "eth"]):
            return None

        prior = yes_price
        score = self.headline_score(market.question)
        if abs(score) < 0.08:
            return None

        p_model = self.bayes_update(prior, score)
        edge = p_model - yes_price
        if abs(edge) < MIN_EDGE:
            return None

        side = "BUY_YES" if edge > 0 else "BUY_NO"
        return Signal(
            strategy="event_bayes",
            market_id=market.market_id,
            question=market.question,
            side=side,
            p_model=p_model,
            p_mkt_yes=yes_price,
            edge=edge,
            confidence=abs(edge),
            metadata={"headline_score": score},
        )


# =========================
# BOT ORCHESTRATOR
# =========================

class StarterPolymarketBot:
    def __init__(self) -> None:
        self.discovery = PolymarketDiscovery()
        self.market_state = MarketState()
        self.portfolio = Portfolio()
        self.risk = RiskEngine(self.portfolio, self.market_state)
        self.executor = PaperExecutor(self.portfolio, self.market_state)

        self.btc_engine = BTCSignalEngine()
        self.weather_engine = WeatherSignalEngine()
        self.event_engine = EventBayesEngine()

    def refresh_markets(self) -> list[Market]:
        markets = self.discovery.fetch_active_markets(limit=200)
        for m in markets:
            if m.outcome_prices:
                # assume yes outcome is first
                self.market_state.set_price(m.market_id, float(m.outcome_prices[0]))
        return markets

    def generate_signals(self, markets: list[Market]) -> list[tuple[Market, Signal]]:
        out: list[tuple[Market, Signal]] = []

        self.btc_engine.update()

        for m in markets:
            yes_price = self.market_state.get_price(m.market_id)
            if yes_price is None:
                continue

            for engine in (self.btc_engine, self.weather_engine, self.event_engine):
                sig = engine.generate_signal(m, yes_price)
                if sig:
                    out.append((m, sig))

        return out

    def run_once(self) -> None:
        markets = self.refresh_markets()
        signals = self.generate_signals(markets)

        # Rank by absolute edge
        signals.sort(key=lambda x: abs(x[1].edge), reverse=True)

        for market, signal in signals[:10]:
            ok, reason, dollars = self.risk.approve(signal, market)
            if ok:
                self.executor.open_position(signal, dollars)
            else:
                log_jsonl({
                    "ts": utc_now().isoformat(),
                    "type": "REJECT",
                    "market_id": signal.market_id,
                    "strategy": signal.strategy,
                    "reason": reason,
                    "edge": signal.edge,
                })

        self.executor.maybe_close_positions()

        equity = self.portfolio.equity(self.market_state.yes_prices)
        dd = self.portfolio.drawdown(self.market_state.yes_prices)

        print(
            f"[{utc_now().isoformat()}] "
            f"cash={self.portfolio.cash:.2f} "
            f"equity={equity:.2f} "
            f"closed_pnl={self.portfolio.closed_pnl:.2f} "
            f"open_positions={len(self.portfolio.positions)} "
            f"drawdown={dd:.2%} "
            f"wins={self.portfolio.win_count} "
            f"losses={self.portfolio.loss_count}"
        )

    async def run_forever(self, sleep_seconds: int = 30) -> None:
        while True:
            try:
                self.run_once()
            except Exception as e:
                log_jsonl({
                    "ts": utc_now().isoformat(),
                    "type": "ERROR",
                    "error": str(e),
                })
                print("ERROR:", e)
            await asyncio.sleep(sleep_seconds)


if __name__ == "__main__":
    bot = StarterPolymarketBot()
    asyncio.run(bot.run_forever(30))


    