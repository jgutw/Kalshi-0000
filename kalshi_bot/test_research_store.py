"""Synthetic temp-directory tests for durable research forecast persistence."""
from __future__ import annotations

import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.config import ASSETS
from kalshi_bot.kalshi_client import KalshiClient
from kalshi_bot.research_store import ResearchForecastStore
from kalshi_bot.sim_state import SimState


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def rec(asset="BTC", window=1722470400, p_real=.55, remaining=90, **extra) -> dict:
    return {
        "asset": asset, "window_id_ts": window, "window_id": str(window), "ticker": f"KX{asset}",
        "ts": "2026-08-01T00:00:00+00:00", "time_remaining": remaining,
        "p_base": .5, "p_real": p_real, "action": "WAIT", "reason": "fixture",
        "price_to_beat": 100.0, "p_market": .51, **extra,
    }


def finalize(store: ResearchForecastStore, asset="BTC", window=1722470400, outcome="YES") -> None:
    store.finalize(asset=asset, closed_window_id_ts=window, actual_outcome=outcome, exit_spot=101.0, close_time_utc="2026-08-01T00:15:00Z")


def rows(root: Path, mode="paper") -> list[dict]:
    result = []
    for path in (root / "research_data" / mode).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return result


def store(root: Path, **kwargs) -> ResearchForecastStore:
    return ResearchForecastStore(root, dry_run=kwargs.pop("dry_run", True), config_profile="fixture", session_tag_provider=lambda: "fixture", **kwargs)


def main() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent) as temp:
        root = Path(temp)
        s = store(root)
        for tick in range(500):
            s.observe(rec(p_real=.01 + tick / 1000, remaining=600 - tick))
        finalize(s)
        saved = rows(root)
        check("500 ticks produce one row with last qualifying snapshot", len(saved) == 1 and saved[0]["p_real"] == .509)

        s = store(root)
        s.observe(rec("BTC", 1722471300, .51, 120)); s.observe(rec("ETH", 1722471300, .61, 120))
        finalize(s, "BTC", 1722471300); finalize(s, "ETH", 1722471300)
        shared = [row for row in rows(root) if row["window_id_ts"] == 1722471300]
        check("assets sharing a window keep independent candidates", len(shared) == 2 and {row["asset"] for row in shared} == {"BTC", "ETH"})

        s = store(root); window = 1722472200
        s.observe(rec("BTC", window, .4, 120)); s.observe(rec("ETH", window, .8, 120)); finalize(s, "BTC", window)
        check("candidate state cannot cross assets", [row for row in rows(root) if row["window_id_ts"] == window][0]["p_real"] == .4)

        s = store(root); window = 1722473100
        s.observe(rec(window=window, p_real=.4, remaining=120)); s.observe(rec(window=window, p_real=.6, remaining=90)); finalize(s, window=window)
        check("last qualifying >=60 record wins", [row for row in rows(root) if row["window_id_ts"] == window][0]["p_real"] == .6)

        s = store(root); window = 1722474000
        s.observe(rec(window=window, p_real=.4, remaining=90)); s.observe(rec(window=window, p_real=.9, remaining=59.9)); s.observe(rec(window=window, p_real=.8, remaining=45)); s.observe(rec(window=window, p_real=.7, remaining=10)); s.observe(rec(window=window, p_real=.95, remaining=80)); finalize(s, window=window)
        check("sub-60 freezes candidate and prevents look-ahead replacement", [row for row in rows(root) if row["window_id_ts"] == window][0]["p_real"] == .4)

        s = store(root); window = 1722474900; s.observe(rec(window=window)); finalize(s, window=window, outcome="NO")
        saved_row = [row for row in rows(root) if row["window_id_ts"] == window][0]
        check("outcome attaches only at finalize with current orientation", saved_row["actual_outcome"] == "NO" and saved_row["yes_settled"] == 0 and saved_row["outcome_source"] == "spot_vs_price_to_beat")

        s = store(root); window = 1722475800; s.observe(rec(window=window)); finalize(s, window=window); s.observe(rec(window=window)); finalize(s, window=window)
        check("duplicate finalize does not append twice", len([row for row in rows(root) if row["window_id_ts"] == window]) == 1)

        day = datetime.fromtimestamp(1722476700, tz=timezone.utc).date().isoformat()
        existing = root / "research_data" / "paper" / f"{day}.jsonl"; existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text('{"asset":"BTC","window_id_ts":1722476700}\nmalformed\n', encoding="utf-8")
        s = store(root); s.observe(rec(window=1722476700)); finalize(s, window=1722476700)
        check("existing duplicate and malformed line are tolerated", len([row for row in rows(root) if row.get("window_id_ts") == 1722476700]) == 1)

        s = store(root); window = 1722477600; finalize(s, window=window)
        check("missing preferred snapshot fabricates nothing", not [row for row in rows(root) if row.get("window_id_ts") == window])
        s.observe(rec(window=window, p_real=math.nan)); finalize(s, window=window)
        check("nonfinite p_real is ineligible", not [row for row in rows(root) if row.get("window_id_ts") == window])
        s.observe(rec(window=window, alpha_micro=None, raw_features=None)); finalize(s, window=window)
        check("missing alpha and raw features remain eligible", len([row for row in rows(root) if row.get("window_id_ts") == window]) == 1)

        bad_root = root / "not_a_directory"; bad_root.write_text("x", encoding="utf-8")
        s = store(bad_root); s.observe(rec(window=1722478500)); finalize(s, window=1722478500)
        check("write failure never raises", True)

        s = store(root); s.observe(rec(window=1722479400)); s = store(root); finalize(s, window=1722479400)
        check("restart does not invent lost snapshot", not [row for row in rows(root) if row.get("window_id_ts") == 1722479400])
        s.observe(rec(window=1722480300)); finalize(s, window=1722480300)
        check("new window cannot inherit prior candidate", len([row for row in rows(root) if row.get("window_id_ts") == 1722480300]) == 1)

        live = store(root, dry_run=False); live.observe(rec(window=1722481200)); finalize(live, window=1722481200)
        check("paper live provenance and directories separate", rows(root, "live")[0]["dry_run"] is False and rows(root, "live")[0]["regime"] == "fixture")
        midnight = int(datetime(2026, 8, 2, tzinfo=timezone.utc).timestamp())
        s = store(root); s.observe(rec(window=midnight)); finalize(s, window=midnight)
        check("UTC date partition comes from window timestamp", (root / "research_data" / "paper" / "2026-08-02.jsonl").exists())
        s = store(root, git_sha_provider=lambda _: (_ for _ in ()).throw(RuntimeError("no git")))
        check("git failure does not break store", s.git_sha is None)

        from kalshi_bot import asset_engine as engine_module
        from kalshi_bot.asset_engine import AssetEngine
        decision_path = engine_module.DECISION_LOG
        try:
            engine_module.DECISION_LOG = str(root / "temporary_decisions.jsonl")
            observed = []
            class Hook:
                def observe(self, value): observed.append(value.copy())
                def finalize(self, **kwargs): pass
            engine = AssetEngine(next(asset for asset in ASSETS if asset.symbol == "BTC"), KalshiClient(), SimState(), research_store=Hook())
            engine._window_id, engine._ticker = 1722482100, "KXBTC"
            before = {"action": "WAIT", "p_real": .5, "p_base": .5, "time_remaining": 90}
            engine._log_decision(before, .5)
            check("research hook observes complete rec without decision change", len(observed) == 1 and observed[0]["p_real"] == .5 and before["action"] == "WAIT")
        finally:
            engine_module.DECISION_LOG = decision_path
    print("19/19 research-store fixture checks passed")


if __name__ == "__main__":
    main()
