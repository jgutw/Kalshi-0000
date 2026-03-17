"""
Polymarket Starter Bot — Month 3–5 Institutional-Inspired Version
Date: March 14, 2026
Focus: Market making + micro-arbitrage + statistical edges on BTC short-term, weather buckets, high-volume events
"""

import os
import json
import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import requests
import websockets
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.constants import BUY, SELL
from py_clob_client.order_builder.constants import BUY as CLOB_BUY, SELL as CLOB_SELL
from py_clob_client.order_builder.constants import OrderType

# ─── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler("pm_bot.log")]
)
logger = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DRY_RUN              = os.getenv("DRY_RUN", "true").lower() == "true"
PRIVATE_KEY          = os.getenv("POLYGON_PRIVATE_KEY")
CHAIN_ID             = int(os.getenv("POLYGON_CHAIN_ID", 137))
CLOB_HOST            = os.getenv("CLOB_API_HOST", "https://clob.polymarket.com")

MAX_RISK_PCT         = float(os.getenv("MAX_RISK_PCT", 0.015))          # per trade
DAILY_DD_STOP_PCT    = float(os.getenv("DAILY_DD_STOP_PCT", 0.08))
MIN_EDGE             = 0.045            # lowered slightly for maker edge
KELLY_FRACTION       = 0.30             # still conservative
MAKER_SPREAD_BPS     = 40               # 0.4% spread target
MAX_INVENTORY_PCT    = 0.10             # max directional exposure per contract
MIN_LIQUIDITY_USD    = 3000             # skip very thin markets
POLL_INTERVAL_SEC    = 12               # market scan frequency

# Simulation / paper trading
SIM_INITIAL_BALANCE  = 10000.0          # USDC
sim_balance          = SIM_INITIAL_BALANCE
sim_pnl_today        = 0.0
sim_start_day        = datetime.now(timezone.utc).date()

# ─── Global State ──────────────────────────────────────────────────────────────
active_markets: Dict[str, dict] = {}          # token_id → market metadata
positions: Dict[str, dict] = {}               # token_id → {'side': BUY/SELL, 'size': float, 'entry_price': float}
inventory_exposure: Dict[str, float] = {}     # token_id → net directional notional
last_pnl_update      = time.time()

# ─── Clob Client ───────────────────────────────────────────────────────────────
if not DRY_RUN:
    client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
        logger.info("Connected to Polymarket CLOB (LIVE mode)")
    except Exception as e:
        logger.error(f"CLOB connection failed: {e}")
        raise
else:
    logger.info("DRY RUN MODE — no real orders will be placed")

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_usdc_balance() -> float:
    if DRY_RUN:
        return sim_balance
    # TODO: real balance query via web3 / Polygon RPC
    return 999999.0  # placeholder

def update_sim_pnl(pnl: float):
    global sim_balance, sim_pnl_today
    sim_balance += pnl
    sim_pnl_today += pnl
    logger.info(f"Sim PnL update: ${pnl:+.2f} | Total: ${sim_balance:.2f} | Today: ${sim_pnl_today:.2f}")

def check_daily_drawdown_stop() -> bool:
    global sim_pnl_today, sim_start_day
    today = datetime.now(timezone.utc).date()
    if today != sim_start_day:
        sim_pnl_today = 0.0
        sim_start_day = today
    dd = sim_pnl_today / SIM_INITIAL_BALANCE
    if dd <= -DAILY_DD_STOP_PCT:
        logger.warning("!!! DAILY DRAWDOWN LIMIT HIT — PAUSING FOR 4 HOURS !!!")
        return True
    return False

def kelly_fraction(p_model: float, p_market: float, b: float = 0.97) -> float:
    """Binary Kelly with fee adjustment"""
    edge = p_model - p_market
    if edge <= 0:
        return 0.0
    q = 1 - p_model
    f_full = (p_model * b - q) / b if b > 0 else 0
    f = max(0.0, min(KELLY_FRACTION * f_full, 0.15))  # hard cap ~15%
    return f

# ─── Market Discovery & Polling ────────────────────────────────────────────────

async def refresh_active_markets():
    """Poll Gamma API for active high-volume markets every ~5 minutes"""
    global active_markets
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&limit=200&order_by=volume&order_dir=desc"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        markets = r.json()

        new_markets = {}
        for m in markets:
            token_id = m.get("id") or m.get("clobTokenIds", [None])[0]
            if not token_id:
                continue
            vol = float(m.get("volume24hrs", 0))
            if vol < 5000:  # skip tiny markets
                continue
            new_markets[token_id] = {
                "question": m["question"],
                "volume24h": vol,
                "liquidity": float(m.get("liquidity", 0)),
                "outcomes": m.get("outcomes", []),
                "category": m.get("category", "Unknown"),
                "last_refresh": time.time()
            }
        active_markets = new_markets
        logger.info(f"Refreshed {len(active_markets)} active markets")
    except Exception as e:
        logger.error(f"Market refresh failed: {e}")

# ─── BTC Short-Term Signal (enhanced MRO + order-flow) ─────────────────────────

BTC_WS_URI = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade/btcusdt@depth20@100ms"

class BTCShortTermEngine:
    def __init__(self, window=120):
        self.window = window
        self.prices = []
        self.vol_buy = []
        self.vol_sell = []
        self.cvd = 0.0
        self.last_ts = 0

    async def on_ws_message(self, msg: str):
        try:
            data = json.loads(msg)
            if data.get("e") == "aggTrade":
                p = float(data["p"])
                q = float(data["q"])
                is_buyer = not data["m"]
                self.prices.append(p)
                if is_buyer:
                    self.vol_buy.append(q)
                    self.vol_sell.append(0)
                else:
                    self.vol_buy.append(0)
                    self.vol_sell.append(q)
                self.cvd += q if is_buyer else -q
                self.last_ts = time.time()

                if len(self.prices) > self.window * 2:
                    self.prices = self.prices[-self.window:]
                    self.vol_buy = self.vol_buy[-self.window:]
                    self.vol_sell = self.vol_sell[-self.window:]
        except:
            pass

    def compute_bias_and_strength(self) -> Tuple[float, float]:
        if len(self.prices) < 30 or time.time() - self.last_ts > 45:
            return 0.50, 0.0

        # log return over window
        logret = np.log(self.prices[-1] / self.prices[0]) if self.prices[0] > 0 else 0

        # order book imbalance
        total_vol = sum(self.vol_buy) + sum(self.vol_sell) + 1e-9
        obi = (sum(self.vol_buy) - sum(self.vol_sell)) / total_vol

        # simple MRO-like oscillator (price-volume momentum)
        if len(self.prices) >= 6:
            pv_now = self.prices[-1] * (sum(self.vol_buy[-5:]) + sum(self.vol_sell[-5:]))
            pv_old = self.prices[-6] * (sum(self.vol_buy[-10:-5]) + sum(self.vol_sell[-10:-5]))
            mro = 100 * (pv_now - pv_old) / (pv_old + 1e-9)
        else:
            mro = 0.0

        # combined bias (sigmoid squash)
        raw = 0.45 * np.tanh(12 * logret) + 0.35 * np.tanh(5.5 * obi) + 0.20 * np.tanh(mro / 80)
        bias = np.clip(0.5 + raw, 0.04, 0.96)

        # confidence proxy (how aligned the signals are)
        strength = min(1.0, abs(logret)*15 + abs(obi)*2.5 + abs(mro)/120)

        return bias, strength

btc_engine = BTCShortTermEngine()

# ─── Weather Forecast vs Market Engine ─────────────────────────────────────────

NWS_GRIDPOINTS = {
    "nyc":     {"point": "OKX/37,39", "station": "KLGA"},
    "chicago": {"point": "LOT/66,77", "station": "KORD"},
    "miami":   {"point": "MFL/106,51", "station": "KMIA"},
    # add more
}

def parse_temp_bucket(question: str) -> Optional[Tuple[float, float]]:
    """Very basic regex-based bucket parser — improve heavily in production"""
    import re
    q = question.lower()
    if "or below" in q:
        m = re.search(r'(\d+)°?f?\s*or below', q)
        if m: return (-200, float(m.group(1)))
    if "or higher" in q:
        m = re.search(r'(\d+)°?f?\s*or (above|higher)', q)
        if m: return (float(m.group(1)), 200)
    m = re.search(r'(\d+)\s*-\s*(\d+)°?f?', q)
    if m: return (float(m.group(1)), float(m.group(2)))
    return None

async def get_forecast_and_observation(city: str = "nyc") -> Optional[float]:
    """Try to get today's observed high + near-term forecast high"""
    try:
        grid = NWS_GRIDPOINTS[city]["point"]
        station = NWS_GRIDPOINTS[city]["station"]

        # observations (past 24h)
        obs_url = f"https://api.weather.gov/stations/{station}/observations?limit=24"
        obs_r = requests.get(obs_url, headers={"User-Agent": "pm-quant-bot"}, timeout=6)
        obs_data = obs_r.json().get("features", [])
        temps = [f["properties"]["temperature"]["value"] for f in obs_data if f["properties"].get("temperature", {}).get("value") is not None]
        obs_high_c = max(temps) if temps else None

        # forecast
        fc_url = f"https://api.weather.gov/gridpoints/{grid}/forecast/hourly"
        fc_r = requests.get(fc_url, headers={"User-Agent": "pm-quant-bot"}, timeout=6)
        periods = fc_r.json()["properties"]["periods"]
        fc_temps = [p["temperature"] for p in periods if p.get("temperature") is not None and p.get("temperatureUnit") == "F"]
        fc_high = max(fc_temps) if fc_temps else None

        if obs_high_c is not None and fc_high is not None:
            return max(obs_high_c, fc_high)
        return obs_high_c or fc_high
    except Exception as e:
        logger.debug(f"Weather fetch failed for {city}: {e}")
        return None

def compute_weather_edge(forecast_f: float, outcomes: List[dict]) -> Tuple[float, float]:
    """Compare forecast to each bucket's implied probability"""
    if not outcomes or forecast_f is None:
        return 0.5, 0.0

    edges = []
    for outcome in outcomes:
        bucket = parse_temp_bucket(outcome.get("question", ""))
        if not bucket:
            continue
        low, high = bucket
        if low <= forecast_f <= high:
            p_market = float(outcome.get("price", 0.5))
            # simplistic logistic confidence
            dist_to_center = abs(forecast_f - (low + high)/2)
            width = high - low
            confidence = np.exp(-2 * dist_to_center / max(width, 1))
            p_model = 0.5 + 0.5 * confidence * (1 if p_market < 0.5 else -1)  # crude
            edge = p_model - p_market
            edges.append((edge, p_market, outcome))

    if not edges:
        return 0.5, 0.0

    best = max(edges, key=lambda x: abs(x[0]))
    return best[1] + best[0], best[0]   # p_model, edge

# ─── Basic Market-Making Quote Logic ───────────────────────────────────────────

async def place_maker_quotes(token_id: str, mid_price: float, liquidity_usd: float):
    """Simple symmetric maker — quote around mid with target spread"""
    if liquidity_usd < MIN_LIQUIDITY_USD:
        return

    spread = MAKER_SPREAD_BPS / 10000.0
    half_spread = spread / 2

    bid_price = mid_price - half_spread
    ask_price = mid_price + half_spread

    size_usd = min(liquidity_usd * 0.08, 400)  # small maker size

    if not DRY_RUN:
        try:
            # Example — real code would use client.create_limit_order(...)
            logger.info(f"Would place maker quote on {token_id}: "
                        f"Bid ${bid_price:.4f} / Ask ${ask_price:.4f} | Size ${size_usd:.0f}")
        except Exception as e:
            logger.error(f"Maker order failed: {e}")
    else:
        logger.info(f"[DRY] Maker quote {token_id}: {bid_price:.4f} – {ask_price:.4f} (${size_usd:.0f})")

# ─── Main Trading & Scanning Loop ──────────────────────────────────────────────

async def main_loop():
    global sim_balance, sim_pnl_today

    # Launch background tasks
    async def binance_data_feed():
        uri = BTC_WS_URI
        async with websockets.connect(uri) as ws:
            logger.info("Binance WS connected")
            while True:
                try:
                    msg = await ws.recv()
                    await btc_engine.on_ws_message(msg)
                except Exception as e:
                    logger.warning(f"Binance WS error: {e}")
                    await asyncio.sleep(5)

    asyncio.create_task(binance_data_feed())
    asyncio.create_task(refresh_active_markets())  # initial call

    logger.info("Polymarket quant bot (Month 3–5 stage) running...")

    while True:
        try:
            if check_daily_drawdown_stop():
                await asyncio.sleep(14400)  # 4 hours
                continue

            balance = get_usdc_balance()
            if balance < 200:
                logger.critical("Balance critically low — stopping")
                break

            # Refresh market list every ~5 min
            if time.time() % 300 < 15:
                await refresh_active_markets()

            # ─── BTC short-term directional + maker logic ──────────────────────
            btc_bias, btc_strength = btc_engine.compute_bias_and_strength()
            for token_id, mkt in active_markets.items():
                if "bitcoin" not in mkt["question"].lower() or "5 min" not in mkt["question"].lower():
                    continue
                # pretend we got real mid-price
                mid = 0.51 + 0.03 * (btc_bias - 0.5)  # correlated dummy
                edge = btc_bias - mid
                if abs(edge) > MIN_EDGE and btc_strength > 0.4:
                    f = kelly_fraction(btc_bias, mid)
                    size = f * balance
                    logger.info(f"BTC edge {edge:+.4f} | strength {btc_strength:.2f} | size ${size:.1f}")
                    # place directional order here (omitted)

                # maker quote attempt
                if mkt["liquidity"] > MIN_LIQUIDITY_USD * 3:
                    await place_maker_quotes(token_id, mid, mkt["liquidity"])

            # ─── Weather arbitrage example ─────────────────────────────────────
            for city in NWS_GRIDPOINTS:
                forecast = await get_forecast_and_observation(city)
                if forecast is None:
                    continue
                for token_id, mkt in active_markets.items():
                    if city.lower() not in mkt["question"].lower() or "temperature" not in mkt["question"].lower():
                        continue
                    p_model, edge = compute_weather_edge(forecast, mkt["outcomes"])
                    if edge > MIN_EDGE:
                        logger.info(f"Weather {city} edge {edge:.4f} | forecast {forecast}°F")
                        f = kelly_fraction(p_model, p_model - edge)
                        size = f * balance
                        # place order logic here

            # ─── Simple intra-market arb scanner ───────────────────────────────
            for token_id, mkt in active_markets.items():
                outcomes = mkt.get("outcomes", [])
                if len(outcomes) != 2:
                    continue
                p_yes = float(outcomes[0].get("price", 0.5))
                p_no  = float(outcomes[1].get("price", 0.5))
                total = p_yes + p_no
                if total < 0.985:  # after fees still profitable
                    arb_edge = 1.0 - total
                    logger.info(f"INTRA-ARB {mkt['question']} | total {total:.4f} | edge {arb_edge:.4f}")
                    # buy both sides proportionally (logic omitted)

            await asyncio.sleep(POLL_INTERVAL_SEC)

        except Exception as e:
            logger.error(f"Main loop exception: {e}", exc_info=True)
            await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        