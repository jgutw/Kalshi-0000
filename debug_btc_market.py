"""Debug: See what crypto events/markets the Gamma API returns."""
import requests

url = "https://gamma-api.polymarket.com/events"
params = {"limit": 200, "active": "true"}
r = requests.get(url, params=params, timeout=10)
events = r.json() if r.ok else []
print(f"Found {len(events)} crypto events\n")

for i, e in enumerate(events[:15]):
    slug = e.get("slug", "")
    title = (e.get("title") or "")[:60]
    markets = e.get("markets", [])
    print(f"{i+1}. slug={slug[:70]}")
    print(f"   title={title}")
    print(f"   markets={len(markets)}")
    for j, m in enumerate(markets[:2]):
        q = (m.get("question") or "")[:50]
        mid = m.get("id", m.get("conditionId", "?"))
        print(f"   mkt{j+1}: id={mid[:40] if isinstance(mid,str) else mid}... | {q}")
    print()
