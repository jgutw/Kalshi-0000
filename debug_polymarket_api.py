"""Debug Polymarket API: Gamma events + CLOB orderbook fetch."""
import json
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

print("1. Fetching crypto events from Gamma...")
r = requests.get(f"{GAMMA}/events", params={"limit": 100, "active": "true", "tag": "crypto"}, timeout=10)
events = r.json() if r.ok else []
print(f"   Found {len(events)} events\n")

yes_token = None
for e in events:
    slug = (e.get("slug") or "").lower()
    if "btc" not in slug and "bitcoin" not in slug:
        continue
    if "5m" not in slug and "5-min" not in slug:
        continue
    for m in e.get("markets", []):
        q = (m.get("question") or "").lower()
        if "bitcoin" in q and ("up" in q or "down" in q):
            tokens = m.get("clobTokenIds", [])
            if tokens:
                yes_token = tokens[0]
                print(f"2. Found BTC 5m market: {m.get('question','')[:60]}")
                print(f"   conditionId: {m.get('conditionId','')[:50]}...")
                print(f"   YES token:   {yes_token[:50]}...")
                op = m.get("outcomePrices") or m.get("outcome_prices")
                print(f"   outcomePrices: {op!r}")
                print(f"   Market keys: {list(m.keys())}")
                break
    if yes_token:
        break

market_from_slug = None
if not yes_token:
    print("   No BTC 5m market found in events.")
    print("   Trying slug btc-updown-5m-* ...")
    import time
    now = int(time.time())
    ts = now - (now % 300)
    for delta in [0, -300, 300]:
        slug = f"btc-updown-5m-{ts + delta}"
        r2 = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10)
        data = r2.json()
        if data and data[0].get("markets"):
            m = data[0]["markets"][0]
            market_from_slug = m
            yes_token = (m.get("clobTokenIds") or [None])[0]
            print(f"   Found via slug {slug}")
            op = m.get("outcomePrices") or m.get("outcome_prices")
            print(f"   outcomePrices: {op!r}")
            print(f"   Market keys: {list(m.keys())}")
            if op:
                try:
                    prices = json.loads(op) if isinstance(op, str) else op
                    yes_p = float(prices[0]) if prices else None
                    print(f"   -> YES price from Gamma: {yes_p}")
                except Exception as ex:
                    print(f"   -> Parse error: {ex}")
            if yes_token:
                break

if yes_token:
    print(f"\n3. Fetching CLOB orderbook for YES token...")
    r3 = requests.get(f"{CLOB}/book", params={"token_id": yes_token}, timeout=10)
    print(f"   Status: {r3.status_code}")
    if r3.ok:
        book = r3.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        print(f"   Keys: {list(book.keys())}")
        print(f"   Bids: {len(bids)} levels")
        print(f"   Asks: {len(asks)} levels")
        if bids:
            print(f"   Best bid sample: {bids[0]}")
        if asks:
            print(f"   Best ask sample: {asks[0]}")
        if bids or asks:
            def _p(arr, idx):
                if not arr: return 0.0 if idx == 0 else 1.0
                r = arr[0]
                if isinstance(r, (list, tuple)): return float(r[0])
                return float(r.get("price", 0))
            bb = _p(bids, 0)
            ba = _p(asks, 1)
            mid = (bb + ba) / 2 if bb > 0 and ba < 1 else 0.5
            print(f"   YES mid price: {mid:.4f}")
        else:
            print("   Empty book — market may be resolved or illiquid")
    else:
        print(f"   Error: {r3.text[:200]}")
else:
    print("\nCould not find a BTC 5m market to test.")
