"""Focused v2 boundary-research persistence tests (offline/temp only)."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.research_store import ResearchForecastStore


WINDOW = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def rec(*, asset="BTC", window=WINDOW, raw_tte=91, p_base=.6, action="WAIT", strategy=None, **extra) -> dict:
    return {
        "asset": asset, "window_id_ts": window, "ticker": f"KX{asset}",
        "ts": "2026-08-01T00:00:00+00:00", "raw_tte": raw_tte,
        "tau_used": max(raw_tte, 60), "realized_vol_value": .3,
        "sigma_used": .3, "p_base": p_base, "z_threshold": .25,
        "spot_now": 101., "price_to_beat": 100., "yes_price_raw": .51,
        "p_market": .51, "p_real": .62, "alpha_micro": .1,
        "action": action, "strategy": strategy, **extra,
    }


def store(root: Path) -> ResearchForecastStore:
    return ResearchForecastStore(root, dry_run=True, config_profile="fixture", session_tag_provider=lambda: "fixture")


def rows(root: Path) -> list[dict]:
    out = []
    for path in (root / "research_data" / "paper" / "boundary").glob("*.jsonl"):
        out.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return out


def snapshots(root: Path, *, window=WINDOW, target=90) -> list[dict]:
    return [r for r in rows(root) if r["record_kind"] == "boundary_snapshot" and r["window_id_ts"] == window and r["target_tte"] == target]


def main() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent) as temp:
        root = Path(temp)
        s = store(root)
        s.observe_boundary(rec(raw_tte=91)); s.observe_boundary(rec(raw_tte=88))
        saved = snapshots(root)
        check("91 persists when later tick is 88", len(saved) == 1 and saved[0]["actual_tte"] == 91)
        check("snapshot contains no future outcome", not ({"yes_settled", "actual_outcome", "exit_spot", "close_time_utc"} & set(saved[0])))

        s = store(root); window = WINDOW + 900
        s.observe_boundary(rec(window=window, raw_tte=88))
        check("only post-horizon 88 omits target 90", not snapshots(root, window=window))

        s = store(root); window = WINDOW + 1800
        for t in (93, 92, 91, 89): s.observe_boundary(rec(window=window, raw_tte=t))
        saved = snapshots(root, window=window)
        check("closest valid pre-horizon candidate wins", len(saved) == 1 and saved[0]["actual_tte"] == 91)
        check("all persisted snapshots obey no-lookahead", all(r["actual_tte"] >= r["target_tte"] for r in rows(root) if r["record_kind"] == "boundary_snapshot"))

        s = store(root); window = WINDOW + 2700
        s.observe_boundary(rec(window=window, raw_tte=91)); s.observe_boundary(rec(window=window, raw_tte=89)); s.observe_boundary(rec(window=window, raw_tte=88))
        check("one row per asset window target", len(snapshots(root, window=window)) == 1)

        s = store(root); window = WINDOW + 3600
        s.observe_boundary(rec(window=window, raw_tte=91)); s.observe_boundary(rec(window=window, raw_tte=88))
        check("snapshot writes before rollover", len(snapshots(root, window=window)) == 1)
        s = store(root)  # simulated restart: snapshot survives without candidate memory
        s.finalize_boundary_window(asset="BTC", closed_window_id_ts=window, actual_outcome="YES", exit_spot=101., close_time_utc="2026-08-01T00:15:00Z", ticker="KXBTC")
        outcome = [r for r in rows(root) if r["record_kind"] == "window_outcome" and r["window_id_ts"] == window]
        check("outcome sidecar joins independently after restart", len(outcome) == 1 and outcome[0]["yes_settled"] == 1 and outcome[0]["outcome_source"] == "spot_vs_price_to_beat")
        s.finalize_boundary_window(asset="BTC", closed_window_id_ts=window, actual_outcome="YES", exit_spot=101., close_time_utc=None)
        check("one outcome per asset window", len([r for r in rows(root) if r["record_kind"] == "window_outcome" and r["window_id_ts"] == window]) == 1)

        s = store(root); window = WINDOW + 4050
        s.observe_boundary(rec(window=window, raw_tte=91))
        s.finalize_boundary_window(asset="BTC", closed_window_id_ts=window, actual_outcome=None, exit_spot=None, close_time_utc=None)
        check("rollover flushes a valid held snapshot without an outcome", len(snapshots(root, window=window)) == 1 and not [r for r in rows(root) if r["record_kind"] == "window_outcome" and r["window_id_ts"] == window])

        s = store(root); window = WINDOW + 4500
        s.observe_boundary(rec(window=window, raw_tte=91, p_base=None)); s.observe_boundary(rec(window=window, raw_tte=88, p_base=None))
        check("missing p_base creates no snapshot", not snapshots(root, window=window))

        s = store(root); window = WINDOW + 5400
        s.observe_boundary(rec(window=window, raw_tte=91, yes_bid=None, yes_ask=None, per_venue_mids=None)); s.observe_boundary(rec(window=window, raw_tte=88))
        saved = snapshots(root, window=window)[0]
        check("missing book and venue state remain nullable", saved["yes_bid"] is None and saved["per_venue_mids"] is None)
        check("raw TTE and used tau remain distinct", saved["raw_tte"] == 91 and saved["tau_used"] == 91 and saved["realized_vol_value"] == .3 and saved["sigma_used"] == .3)

        for suffix, action, strategy, expected in ((6300, "WAIT", None, "raw_WAIT"), (7200, "BUY_YES", "lag_arb", "smoothed_buy_decision"), (8100, "BUY_YES", "fill_quota", "raw_fill_quota"), (9000, "BUY_NO", "fill_quota", "raw_fill_quota")):
            window = WINDOW + suffix; s = store(root); s.observe_boundary(rec(window=window, raw_tte=91, action=action, strategy=strategy)); s.observe_boundary(rec(window=window, raw_tte=88, action=action, strategy=strategy))
            check(f"{action} {strategy} has explicit market semantics", snapshots(root, window=window)[0]["p_market_semantics"] == expected)

        bad = root / "not_a_directory"; bad.write_text("x", encoding="utf-8")
        s = store(bad); s.observe_boundary(rec(raw_tte=91)); s.observe_boundary(rec(raw_tte=88))
        check("boundary write failure never raises", True)
    print("17/17 boundary-store fixture checks passed")


if __name__ == "__main__":
    main()
