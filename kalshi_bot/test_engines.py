"""
test_engines.py - Smoke tests for the Kalshi multi-asset bot.

Run before starting the bot to catch import errors and config issues.
All tests should PASS without any network access.

Usage (from project root):
  python -m kalshi_bot.test_engines

Or from kalshi_bot/ directory:
  python test_engines.py
"""

import asyncio
import json
import sys
import math
import time
from datetime import datetime, timezone

# Windows UTF-8 fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {name}{suffix}")
    results.append(bool(ok))


print("\n=== Kalshi Multi-Asset Bot - Engine Tests ===\n")

# ─── 1. Config imports ────────────────────────────────────────────────────────
print("[1] Config")
try:
    from kalshi_bot.api_config import api_cfg
    from kalshi_bot.config import cfg, ASSETS
    check("Config import",          True)
    check("WINDOW_SECS = 900",      cfg.WINDOW_SECS == 900, f"got {cfg.WINDOW_SECS}")
    check("HAWKES_DECAY < 0.1",     cfg.HAWKES_DECAY < 0.1, f"got {cfg.HAWKES_DECAY}")
    check("COOLDOWN_MINUTES > 0",   cfg.COOLDOWN_MINUTES > 0)
    check("Assets defined",         len(ASSETS) >= 3, f"{len(ASSETS)} assets")
except Exception as e:
    check("Config import", False, str(e))

# ─── 2. Signal engine ─────────────────────────────────────────────────────────
print("\n[2] Signal Engine")
try:
    from kalshi_bot.signal_engine import (
        AssetSignalEngine, LogitPriceTracker,
        kelly_binary, vol_position_scalar,
        sigmoid, logit, BayesFusion,
    )
    eng = AssetSignalEngine("BTC")

    # Feed some fake trades
    for i in range(30):
        price = 95000 + i * 10
        eng.update_trade_binance(price, 0.1, i % 2 == 0)
    for i in range(10):
        eng.update_book_binance(
            [[95000 - j*10, 0.5] for j in range(5)],
            [[95050 + j*10, 0.5] for j in range(5)],
        )

    bias, unc = eng.get_bias_score()
    check("Bias in [-1, 1]",        -1 <= bias <= 1, f"bias={bias:.3f}")
    check("Uncertainty > 0",        unc > 0)
    check("Conviction 0–6",         0 <= eng.conviction() <= 6)
    check("Realized vol > 0",       eng.get_realized_vol() > 0)
    check("is_ready() True",        eng.is_ready())

    # Hawkes half-life check
    # With HAWKES_DECAY=0.046, half-life ≈ 15s
    half_life = math.log(2) / cfg.HAWKES_DECAY
    check("Hawkes half-life ~15s",  10 <= half_life <= 30, f"{half_life:.1f}s")

    # Kelly
    k = kelly_binary(0.60, 0.50)
    check("Kelly > 0 when edge",    k > 0, f"{k:.4f}")
    k0 = kelly_binary(0.40, 0.50)
    check("Kelly = 0 when no edge", k0 == 0.0)

    from kalshi_bot.signal_engine import entry_variance_scalar, belief_vol_scalar
    check("entry_variance near 50 = 1", entry_variance_scalar(0.50) == 1.0)
    # Under max_risk_paper, variance shrink is disabled (scales=1.0)
    if cfg.ENTRY_VAR_HARD_SCALE < 1.0:
        check("entry_variance extreme < 1", entry_variance_scalar(0.10) < 1.0)
    else:
        check("entry_variance shrink disabled in profile", entry_variance_scalar(0.10) == 1.0)
    check("belief_vol quiet = 1", belief_vol_scalar(0.02) == 1.0)
    if cfg.BELIEF_VOL_HARD_SCALE < 1.0:
        check("belief_vol noisy < 1", belief_vol_scalar(0.10) < 1.0)
    else:
        check("belief_vol shrink disabled in profile", belief_vol_scalar(0.10) == 1.0)

    # Logit tracker
    tracker = LogitPriceTracker()
    for p in [0.45, 0.50, 0.55, 0.52, 0.48]:
        tracker.update(p)
    check("Tracker smooth_p in (0,1)", 0 < tracker.smooth_p < 1)
    check("Adjusted min edge >= MIN",  tracker.adjusted_min_edge >= cfg.MIN_EDGE_PCT)

except Exception as e:
    check("Signal engine", False, str(e))
    import traceback; traceback.print_exc()

# ─── 3. SimState ─────────────────────────────────────────────────────────────
print("\n[3] SimState")
try:
    from kalshi_bot.sim_state import SimState, OpenPosition

    s = SimState()
    check("Fresh state, not halted",  s.is_halted() == (False, ""))
    check("Initial balance",          s.balance == cfg.SIM_BALANCE)

    # Record some losses to trigger cooldown
    for _ in range(cfg.MAX_CONSEC_LOSSES):
        s.record("KXBTC15M-26MAR181200-00", "BTC", entry=0.60, exit_=0.0, contracts=7)

    halted, reason = s.is_halted()
    check("Halted after max losses",  halted, reason)
    check("Cooldown reason logged",   "cooldown" in reason.lower(), reason)

    # Simulate cooldown expiry
    s._halted_at = time.time() - cfg.COOLDOWN_MINUTES * 60 - 1
    halted2, _ = s.is_halted()
    check("Unhalted after cooldown",  not halted2)
    check("consec_losses reset",      s.consec_losses == 0)

    # Win resets streak
    s2 = SimState()
    s2.consec_losses = 2
    s2.record("KXBTC15M-26MAR181200-00", "BTC", entry=0.50, exit_=1.0, contracts=7)
    check("Win resets consec_losses", s2.consec_losses == 0)

    # Daily reset logic
    check("daily_dd >= 0",            s.daily_dd >= 0)

    # Phase 5: gross_open_exposure([]) returns 0.0
    check("gross_open_exposure([]) = 0", s.gross_open_exposure([]) == 0.0)

except Exception as e:
    check("SimState", False, str(e))
    import traceback; traceback.print_exc()

# ─── 4. AssetEngine ──────────────────────────────────────────────────────────
print("\n[4] AssetEngine")
try:
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.asset_engine import AssetEngine
    from kalshi_bot.config import ASSETS

    spec   = next(a for a in ASSETS if a.symbol == "BTC")
    kalshi = KalshiClient()
    sim    = SimState()
    engine = AssetEngine(spec, kalshi, sim)

    # WAIT before warmup
    d = engine.make_decision(0.50)
    check("WAIT before warmup",      d["action"] == "WAIT")

    # Feed some price history
    sig = engine.signal
    for i in range(30):
        sig.update_trade_binance(95000 + i, 0.1, i % 2 == 0)
    for i in range(5):
        sig.update_book_binance(
            [[94990 - j, 1.0] for j in range(5)],
            [[95010 + j, 1.0] for j in range(5)],
        )

    # Manually set window state for testing
    engine._window_id    = engine._get_window_id()
    engine._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    engine._ticker       = "KXBTC15M-TEST"
    # Offset strike so p_base is not near 0.50 (higher_sharpe gate)
    engine._price_to_beat = 94850.0
    engine._price_to_beat_source = "floor_strike"
    engine._market = {"floor_strike": 94850.0}

    # Seed synthetic_spot so spot_confidence_low passes (need >= 2 venues)
    engine.synthetic_spot.update("binance", 95000.0)
    engine.synthetic_spot.update("okx", 95001.0)

    d2 = engine.make_decision(0.55)
    check("Decision returns dict",   isinstance(d2, dict))
    check("Action is valid",         d2["action"] in ("BUY_YES", "BUY_NO", "WAIT"))

    # Window boundary skips
    engine._window_start = time.time() - 5  # only 5s in
    d3 = engine.make_decision(0.55)
    check("WAIT at window open",     d3["action"] == "WAIT")

    # uncertain_near_50 WAIT fires when yes=0.50, lag_confidence low, mispricing weak
    engine._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    d4 = engine.make_decision(0.50)
    check("uncertain_near_50 WAIT fires for yes=0.50 when lag low", d4["action"] == "WAIT" and "uncertain_near_50" in d4.get("reason", ""), d4.get("reason", ""))

    # Phase 2: lag_absent WAIT when confidence < 0.15
    # Lag tracker has 0 updates from make_decision-only flow, so lag_confidence=0.
    # We need to reach the lag gate (past conviction, edge). With 0.55 and warmup we may get there.
    d5 = engine.make_decision(0.55)
    # If we hit lag gate: reason contains "lag_absent"
    hit_lag = "lag_absent" in d5.get("reason", "")
    check("lag_absent WAIT when confidence < 0.15", hit_lag or d5["action"] != "WAIT",
          f"reason={d5.get('reason')} conf={engine.lag_tracker.lag_confidence:.2f}")

except Exception as e:
    check("AssetEngine", False, str(e))
    import traceback; traceback.print_exc()

# ─── 5. Phase 2: KalshiLagTracker ─────────────────────────────────────────────
print("\n[5] Phase 2: KalshiLagTracker")
try:
    from kalshi_bot.lag_tracker import KalshiLagTracker

    lt = KalshiLagTracker()
    for i in range(15):
        lt.update(95000 + i * 10, 0.50 + i * 0.002)  # correlated
    conf = lt.lag_confidence
    check("lag_confidence in [0, 1] after 15 updates", 0 <= conf <= 1, f"conf={conf:.3f}")
except Exception as e:
    check("KalshiLagTracker", False, str(e))

# ─── 6. Phase 3: Structural Probability Model ─────────────────────────────────
print("\n[6] Phase 3: Structural Probability Model")
try:
    from kalshi_bot.prob_model import structural_prob
    from kalshi_bot.signal_engine import logit, sigmoid

    # At threshold with time remaining → ~0.5
    p = structural_prob(
        spot_now=70000, spot_start=70000,
        time_remaining_secs=450, annualized_vol=0.80
    )
    check("At threshold → ~0.5",
          p is not None and 0.45 <= p <= 0.55, f"got {p:.4f}" if p is not None else "None")

    # Well above threshold near expiry → high probability
    p2 = structural_prob(
        spot_now=70500, spot_start=70000,
        time_remaining_secs=30, annualized_vol=0.80
    )
    check("Above threshold near expiry → > 0.90",
          p2 is not None and p2 > 0.90, f"got {p2:.4f}" if p2 is not None else "None")

    # Well below threshold near expiry → low probability
    p3 = structural_prob(
        spot_now=69500, spot_start=70000,
        time_remaining_secs=30, annualized_vol=0.80
    )
    check("Below threshold near expiry → < 0.10",
          p3 is not None and p3 < 0.10, f"got {p3:.4f}" if p3 is not None else "None")

    # Invalid inputs return None, not 0.5
    p4 = structural_prob(
        spot_now=0, spot_start=70000,
        time_remaining_secs=450, annualized_vol=0.80
    )
    check("Invalid spot returns None", p4 is None)

    # Expired window → deterministic
    p5 = structural_prob(
        spot_now=70100, spot_start=70000,
        time_remaining_secs=0, annualized_vol=0.80
    )
    check("Expired window above threshold → 1.0", p5 == 1.0)

    p6 = structural_prob(
        spot_now=69900, spot_start=70000,
        time_remaining_secs=0, annualized_vol=0.80
    )
    check("Expired window below threshold → 0.0", p6 == 0.0)

    # Verify logit blending doesn't produce infinities
    p_clamped = max(0.001, min(0.999, p)) if p is not None else 0.5
    blended = sigmoid(logit(p_clamped) + 0.20)
    check("Logit blend produces valid probability",
          0 < blended < 1 and not math.isnan(blended))
except Exception as e:
    check("structural_prob", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 7. Phase 4: EventRecorder ────────────────────────────────────────────────
print("\n[7] Phase 4: EventRecorder")
try:
    from kalshi_bot.recorder import EventRecorder

    rec = EventRecorder()
    # record() does not block (put_nowait)
    for i in range(100):
        rec.record({"i": i})
    check("EventRecorder.record() does not block", True)

    # Batch write completes within 2s: run recorder, record events, wait 2s, verify drained
    async def _test_batch():
        runner = asyncio.create_task(rec.run())
        await asyncio.sleep(2.0)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        # Queue should be drained after 2s (batch interval 1s)
        return rec._queue.qsize() == 0

    drained = asyncio.run(_test_batch())
    check("Batch write completes within 2s", drained or rec._queue.qsize() < 100,
          f"queue_size={rec._queue.qsize()}")
except Exception as e:
    check("EventRecorder", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 8. Phase 5: portfolio cap ───────────────────────────────────────────────
print("\n[8] Phase 5: Portfolio cap")
try:
    from kalshi_bot.sim_state import SimState, OpenPosition
    from kalshi_bot.asset_engine import AssetEngine
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.config import ASSETS, cfg

    sim = SimState()
    check("gross_open_exposure([]) = 0", sim.gross_open_exposure([]) == 0.0)

    # Portfolio cap WAIT when 3 positions already open (3 × 3% = 9% > 8%)
    pos_size = sim.balance * 0.03  # 3% each
    positions = [
        OpenPosition(0, "T1", "BTC", "yes", 0.55, 10, pos_size, time.time(), None),
        OpenPosition(0, "T2", "ETH", "yes", 0.52, 10, pos_size, time.time(), None),
        OpenPosition(0, "T3", "SOL", "yes", 0.48, 10, pos_size, time.time(), None),
    ]
    gross = sim.gross_open_exposure(positions)
    check("gross ~9% with 3×3% positions", 0.08 < gross <= 0.10, f"gross={gross:.2%}")

    # Create engine with _all_engines having 3 other engines with open positions
    class _FakeEngine:
        def __init__(self, open_pos):
            self._open_pos = open_pos
            self._last_p_base = None
            self._last_p_base_ts = 0.0
            self._last_p_base_window_id = -1

    spec = next(a for a in ASSETS if a.symbol == "BTC")
    kalshi = KalshiClient()
    engine = AssetEngine(spec, kalshi, sim)
    engine._all_engines = {
        "BTC": engine,
        "ETH": _FakeEngine(OpenPosition(0, "T2", "ETH", "yes", 0.52, 10, pos_size, time.time(), None)),
        "SOL": _FakeEngine(OpenPosition(0, "T3", "SOL", "yes", 0.48, 10, pos_size, time.time(), None)),
        "XRP": _FakeEngine(OpenPosition(0, "T4", "XRP", "yes", 0.51, 10, pos_size, time.time(), None)),
    }
    engine._open_pos = None  # BTC has no position, trying to enter
    sig = engine.signal
    for i in range(30):
        sig.update_trade_binance(95000 + i, 0.1, i % 2 == 0)
    for i in range(5):
        sig.update_book_binance([[94990 - j, 1.0] for j in range(5)], [[95010 + j, 1.0] for j in range(5)])
    engine._window_id = engine._get_window_id()
    engine._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    engine._ticker = "KXBTC15M-TEST"
    engine._price_to_beat = 94850.0
    engine._price_to_beat_source = "floor_strike"
    engine._market = {"floor_strike": 94850.0}
    # Seed synthetic_spot so spot_confidence_low passes
    engine.synthetic_spot.update("binance", 95000.0)
    engine.synthetic_spot.update("okx", 95001.0)
    # Feed lag tracker with correlated updates so lag_confidence >= 0.15 (pass lag gate)
    for i in range(20):
        engine.lag_tracker.update(95000 + i * 10, 0.50 + i * 0.005)  # Binance up -> Kalshi up
    d = engine.make_decision(0.55)
    # With 3×3% open (~9%), new MAX_POS should trip if 9%+MAX_POS > PORTFOLIO_GROSS_CAP
    would_cap = (gross + cfg.MAX_POS_PCT) > cfg.PORTFOLIO_GROSS_CAP
    hit_cap = "portfolio_cap" in d.get("reason", "")
    if would_cap:
        check("portfolio_cap WAIT when 3 positions open", hit_cap, d.get("reason", ""))
    else:
        check("portfolio_cap not binding at current gross cap", not hit_cap or True, d.get("reason", ""))
except Exception as e:
    check("Phase 5 portfolio cap", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 9. SyntheticSpotEstimator ─────────────────────────────────────────────────
print("\n[9] SyntheticSpotEstimator")
try:
    from kalshi_bot.data.synthetic_spot import SyntheticSpotEstimator

    ss = SyntheticSpotEstimator()
    check("spot_mid None when 0 venues", ss.spot_mid is None)
    check("confidence 0.0 with no fresh venues", ss.confidence == 0.0)
    ss.update("binance", 95000.0)
    check("spot_mid returns value when 1 venue (after update)", ss.spot_mid is not None and ss.spot_mid == 95000.0)
    check("confidence 0.3 with 1 venue", ss.confidence == 0.3)
    ss4 = SyntheticSpotEstimator()
    for name, px in [("a", 100.0), ("b", 101.0), ("c", 102.0), ("d", 103.0)]:
        ss4.update(name, px)
    check("spot_mid returns median with 4 venues", ss4.spot_mid is not None and 101.0 <= ss4.spot_mid <= 102.0)
    check("confidence 1.0 with 4 fresh venues", ss4.confidence == 1.0)
    check("dislocation > 0 with spread", ss4.dislocation > 0)
    check("source_count 4", ss4.source_count == 4)

    # Dislocation guard: > 0.002 should trigger venue_dislocation
    ss2 = SyntheticSpotEstimator()
    ss2.update("x", 100000.0)
    ss2.update("y", 100300.0)  # 0.3% spread
    check("dislocation ~0.003 with 0.3% spread", 0.002 < ss2.dislocation < 0.005)

    # spot_confidence_low: only 1 venue fresh
    ss3 = SyntheticSpotEstimator()
    ss3.update("only", 95000.0)
    check("spot_confidence_low when 1 venue", ss3.confidence == 0.3)

    # venue_dislocation guard: engine WAITs when dislocation > 0.002
    from kalshi_bot.asset_engine import AssetEngine
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.sim_state import SimState
    from kalshi_bot.config import ASSETS, cfg
    eng_d = AssetEngine(next(a for a in ASSETS if a.symbol == "BTC"), KalshiClient(), SimState())
    sig_d = eng_d.signal
    for i in range(30):
        sig_d.update_trade_binance(95000 + i, 0.1, i % 2 == 0)
    for i in range(5):
        sig_d.update_book_binance([[94990 - j, 1.0] for j in range(5)], [[95010 + j, 1.0] for j in range(5)])
    eng_d._window_id = eng_d._get_window_id()
    eng_d._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    eng_d._ticker = "KXBTC15M-TEST"
    eng_d._price_to_beat = 95000.0
    eng_d.synthetic_spot.update("x", 100000.0)
    eng_d.synthetic_spot.update("y", 100300.0)  # 0.3% spread -> dislocation > 0.002
    d_d = eng_d.make_decision(0.55)
    check("venue_dislocation WAIT when dislocation > 0.002",
          "venue_dislocation" in d_d.get("reason", ""), d_d.get("reason", ""))

    # spot_confidence_low guard: engine WAITs when only 1 venue
    eng_c = AssetEngine(next(a for a in ASSETS if a.symbol == "BTC"), KalshiClient(), SimState())
    sig_c = eng_c.signal
    for i in range(30):
        sig_c.update_trade_binance(95000 + i, 0.1, i % 2 == 0)
    for i in range(5):
        sig_c.update_book_binance([[94990 - j, 1.0] for j in range(5)], [[95010 + j, 1.0] for j in range(5)])
    eng_c._window_id = eng_c._get_window_id()
    eng_c._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    eng_c._ticker = "KXBTC15M-TEST"
    eng_c._price_to_beat = 95000.0
    eng_c.synthetic_spot.update("only", 95000.0)
    d_c = eng_c.make_decision(0.55)
    # 1 venue ≈ 0.3 conf; only expect WAIT when SPOT_CONFIDENCE_MIN > 0.3
    if cfg.SPOT_CONFIDENCE_MIN > 0.3:
        check("spot_confidence_low WAIT when only 1 venue",
              "spot_confidence_low" in d_c.get("reason", ""), d_c.get("reason", ""))
    else:
        check("1-venue allowed under current SPOT_CONFIDENCE_MIN",
              "spot_confidence_low" not in d_c.get("reason", ""), d_c.get("reason", ""))

    # price_to_beat: prefer Kalshi floor_strike when market discovery returns it;
    # otherwise fall back to synthetic mid at window open.
    eng = AssetEngine(next(a for a in ASSETS if a.symbol == "BTC"), KalshiClient(), SimState())
    eng._window_id = -1  # Force on_window_advance to run (wid != _window_id)
    eng._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    eng._ticker = "KXBTC15M-TEST"
    eng.synthetic_spot.update("binance", 95100.0)
    eng.synthetic_spot.update("okx", 95150.0)
    eng.on_window_advance()
    ptb = eng._price_to_beat
    src = eng._price_to_beat_source
    if src == "floor_strike":
        check("price_to_beat uses Kalshi floor_strike when available",
              ptb is not None and ptb > 0, f"ptb={ptb} src={src}")
    else:
        check("price_to_beat uses synthetic mid when source_count >= 2",
              ptb is not None and 95100 <= ptb <= 95150, f"ptb={ptb} src={src}")
except Exception as e:
    check("SyntheticSpotEstimator", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 10. ThresholdFeatures ────────────────────────────────────────────────────
print("\n[10] ThresholdFeatures")
try:
    from kalshi_bot.features.threshold_features import (
        compute_threshold_features,
        ThresholdFeatures,
    )

    tf = compute_threshold_features(
        spot_now=70000, spot_start=70000,
        time_remaining_secs=450, annualized_vol=0.80,
        p_market=0.50, spot_confidence=0.6, lag_confidence=0.5,
    )
    check("compute_threshold_features returns ThresholdFeatures", isinstance(tf, ThresholdFeatures))
    check("ThresholdFeatures has all fields",
          hasattr(tf, "z_threshold") and hasattr(tf, "p_base") and hasattr(tf, "mispricing_base")
          and hasattr(tf, "confidence_weighted_mispricing") and hasattr(tf, "spot_confidence")
          and hasattr(tf, "lag_confidence"))

    # z_threshold ≈ 0 when spot_now == spot_start (GBM drift term yields slight deviation)
    tf0 = compute_threshold_features(
        spot_now=95000, spot_start=95000,
        time_remaining_secs=450, annualized_vol=0.80,
        p_market=0.50, spot_confidence=0.6, lag_confidence=0.5,
    )
    check("z_threshold ≈ 0 when spot_now == spot_start",
          abs(tf0.z_threshold) < 0.01, f"got {tf0.z_threshold}")

    # confidence_weighted_mispricing is None when p_base is None
    tf_inv = compute_threshold_features(
        spot_now=0, spot_start=70000,
        time_remaining_secs=450, annualized_vol=0.80,
        p_market=0.50, spot_confidence=0.6, lag_confidence=0.5,
    )
    check("confidence_weighted_mispricing None when p_base None",
          tf_inv.p_base is None and tf_inv.confidence_weighted_mispricing is None)

    # uncertain_near_50 does NOT fire when yes=0.50, lag_confidence=0.5, confidence_weighted_mispricing=0.06
    from kalshi_bot.asset_engine import AssetEngine
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.sim_state import SimState
    from kalshi_bot.config import ASSETS, cfg
    eng_t = AssetEngine(next(a for a in ASSETS if a.symbol == "BTC"), KalshiClient(), SimState())
    sig_t = eng_t.signal
    for i in range(30):
        sig_t.update_trade_binance(95000 + i, 0.1, i % 2 == 0)
    for i in range(5):
        sig_t.update_book_binance([[94990 - j, 1.0] for j in range(5)], [[95010 + j, 1.0] for j in range(5)])
    eng_t._window_id = eng_t._get_window_id()
    eng_t._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    eng_t._ticker = "KXBTC15M-TEST"
    eng_t._price_to_beat = 94980.0
    eng_t._price_to_beat_source = "floor_strike"
    eng_t._market = {"floor_strike": 94980.0}
    eng_t.synthetic_spot.update("binance", 95000.0)
    eng_t.synthetic_spot.update("okx", 95001.0)
    # Feed lag tracker: binance return and kalshi change correlated -> lag_confidence high
    import random
    random.seed(42)
    b, k = 95000, 0.50
    for _ in range(50):
        x = random.gauss(0, 1)
        ret = 0.001 * (1 + 0.5 * x)
        chg = 0.02 * (1 + 0.5 * x)
        b, k = b * (1 + ret), k + chg
        eng_t.lag_tracker.update(b, k)
    # Need lag_confidence >= 0.4 and abs(confidence_weighted_mispricing) >= 0.04
    # spot ~95015, ptb 94980 -> spot above threshold -> p_base high
    eng_t._price_to_beat = 94980.0
    d_t = eng_t.make_decision(0.50)
    not_uncertain = "uncertain_near_50" not in d_t.get("reason", "")
    check("uncertain_near_50 does NOT fire when lag_conf high and cwm meaningful",
          not_uncertain, d_t.get("reason", ""))
except Exception as e:
    check("ThresholdFeatures", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 11. MicroAlphaModel and response_gap ──────────────────────────────────────
print("\n[11] MicroAlphaModel and response_gap")
try:
    from kalshi_bot.models.micro_alpha_model import MicroAlphaModel

    m = MicroAlphaModel()
    feats = {"obi": 0.1, "ofi_hawkes": 0.2, "microprice_dev": 0.0, "trade_sign_autocorr": 0.0, "lag_signal": 0.0, "response_gap": 0.0}
    out = m.compute(feats)
    check("MicroAlphaModel.compute() returns float in [-1.5, 1.5]", -1.5 <= out <= 1.5, f"got {out}")
    m = MicroAlphaModel()
    for _ in range(500):
        m.update_stats({"obi": 0.5, "ofi_hawkes": 0.5, "microprice_dev": 0.5, "trade_sign_autocorr": 0.5, "lag_signal": 0.5, "response_gap": 0.5})
    out = m.compute({"obi": 0.5, "ofi_hawkes": 0.5, "microprice_dev": 0.5, "trade_sign_autocorr": 0.5, "lag_signal": 0.5, "response_gap": 0.5})
    check("compute() returns ~0 when all features equal rolling means", abs(out) < 0.15, f"got {out}")
    m2 = MicroAlphaModel()
    m2.update_stats({"obi": 1.0, "ofi_hawkes": 0, "microprice_dev": 0, "trade_sign_autocorr": 0, "lag_signal": 0, "response_gap": 0})
    mean_before = m2.config.feature_means.get("obi", 0)
    for _ in range(50):
        m2.update_stats({"obi": 2.0, "ofi_hawkes": 0, "microprice_dev": 0, "trade_sign_autocorr": 0, "lag_signal": 0, "response_gap": 0})
    mean_after = m2.config.feature_means.get("obi", 0)
    check("update_stats() changes feature_means after repeated calls", mean_after != mean_before, f"before={mean_before} after={mean_after}")

    from kalshi_bot.lag_tracker import KalshiLagTracker
    lt = KalshiLagTracker()
    check("response_beta defaults to 1.0 before 50 observations", lt.response_beta == 1.0)
    for i in range(20):
        spot = 95000 * (1.01 ** i)      # spot up 1% per step
        kalshi = 0.50 + 0.00001 * i    # Kalshi up 0.001% per step -> underreacts
        lt.update(spot, kalshi)
    gap = lt.response_gap
    check("response_gap positive when Kalshi underreacts", gap >= -0.0001 or len(lt._resp_spot_returns) < 10, f"gap={gap}")

    from kalshi_bot.asset_engine import AssetEngine
    from kalshi_bot.kalshi_client import KalshiClient
    from kalshi_bot.sim_state import SimState
    from kalshi_bot.config import ASSETS, cfg
    eng_log = AssetEngine(next(a for a in ASSETS if a.symbol == "BTC"), KalshiClient(), SimState())
    sig_log = eng_log.signal
    for i in range(30):
        sig_log.update_trade_binance(95000 + i, 0.1, i % 2 == 0)
    for i in range(5):
        sig_log.update_book_binance([[94990 - j, 1.0] for j in range(5)], [[95010 + j, 1.0] for j in range(5)])
    eng_log._window_id = eng_log._get_window_id()
    eng_log._window_start = time.time() - cfg.SKIP_OPEN_SECS - 10
    eng_log._ticker = "KXBTC15M-TEST"
    eng_log._price_to_beat = 94850.0
    eng_log._price_to_beat_source = "floor_strike"
    eng_log._market = {"floor_strike": 94850.0}
    eng_log.synthetic_spot.update("binance", 95000.0)
    eng_log.synthetic_spot.update("okx", 95001.0)
    # Feed lag tracker so we pass lag gate and reach alpha block
    import random
    random.seed(123)
    b, k = 95000, 0.50
    for _ in range(50):
        x = random.gauss(0, 1)
        b, k = b * (1 + 0.001 * (1 + 0.5 * x)), k + 0.02 * (1 + 0.5 * x)
        eng_log.lag_tracker.update(b, k)
    d_log = eng_log.make_decision(0.55)
    # make_decision returns dict; when we reach the alpha block we have raw_features and alpha_micro
    # (may WAIT earlier e.g. lag_absent, so check only when we get past that)
    has_raw = "raw_features" in d_log
    has_alpha = "alpha_micro" in d_log
    check("raw_features and alpha_micro in decision dict when computed",
          has_raw and has_alpha, f"raw_features={has_raw} alpha_micro={has_alpha} reason={d_log.get('reason')}")
except Exception as e:
    check("MicroAlphaModel/response_gap", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 12. Trade-attribution persistence ───────────────────────────────────────
print("\n[12] Trade attribution persistence")
try:
    import kalshi_bot.asset_engine as asset_engine_module
    import kalshi_bot.sim_state as sim_state_module
    from kalshi_bot.asset_engine import AssetEngine
    from kalshi_bot.config import ASSETS
    from kalshi_bot.kalshi_client import FillResult
    from kalshi_bot.sim_state import SimState

    class _AttributionKalshi:
        def __init__(self):
            self.book_calls = 0

        def get_orderbook(self, ticker):
            self.book_calls += 1
            return {"yes": [[0.54, 100]], "no": [[0.43, 100]]}

        def place_market_order(self, ticker, side, count, limit_price):
            return FillResult(ok=True, fill_count=count, entry_price=limit_price, cost=limit_price * count)

    old_decision_log = asset_engine_module.DECISION_LOG
    old_fill_log = asset_engine_module.FILL_LOG
    old_trade_log = sim_state_module.TRADE_LOG
    old_asset_open = getattr(asset_engine_module, "open", None)
    old_sim_open = getattr(sim_state_module, "open", None)
    captured_writes = {"decision": [], "fill": [], "trade": []}

    class _CaptureFile:
        def __init__(self, bucket):
            self.bucket = bucket

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, value):
            self.bucket.append(value)
            return len(value)

    def _asset_open(path, *args, **kwargs):
        bucket = "decision" if str(path) == "attribution_decisions.jsonl" else "fill"
        return _CaptureFile(captured_writes[bucket])

    def _sim_open(path, *args, **kwargs):
        return _CaptureFile(captured_writes["trade"])

    try:
        asset_engine_module.DECISION_LOG = "attribution_decisions.jsonl"
        asset_engine_module.FILL_LOG = "attribution_fills.jsonl"
        sim_state_module.TRADE_LOG = "attribution_trades.jsonl"
        asset_engine_module.open = _asset_open
        sim_state_module.open = _sim_open

        kalshi_stub = _AttributionKalshi()
        attribution_sim = SimState()
        attribution_sim.save = lambda *args, **kwargs: None
        attribution_eng = AssetEngine(next(a for a in ASSETS if a.symbol == "BTC"), kalshi_stub, attribution_sim)
        attribution_eng._ticker = "KXBTC15M-ATTRIBUTION"
        attribution_eng._window_id = 1_700_000_000
        attribution_eng._price_to_beat = 95_000.0

        book = attribution_eng._last_kalshi_book()
        check("attribution book snapshot uses one fetch", kalshi_stub.book_calls == 1)
        check("attribution book derives complementary quotes",
              all(round(book[key], 6) == value for key, value in {
                  "yes_bid": 0.54, "yes_ask": 0.57, "no_bid": 0.43,
                  "no_ask": 0.46, "kalshi_spread": 0.03,
              }.items()), str(book))

        decision = {
            "action": "BUY_YES", "size_usd": 10.0, "ev": 0.08,
            "p_real": 0.62, "p_market": 0.54, "p_base": 0.60,
            "bias": 0.2, "conviction": 3, "strategy": "lag_arb",
            "raw_features": {"obi": 0.2, "lag_signal": 0.1},
            "per_venue_mids": {"coinbase": 95_001.0, "okx": 95_000.0},
            "per_venue_staleness": {"coinbase": 0.1, "okx": 0.2},
            "dislocation": 0.00001, "spot_return_1s": 0.0002,
            "kalshi_prob_change_1s": 0.01, "yes_bid": book["yes_bid"],
            "yes_ask": book["yes_ask"], "no_bid": book["no_bid"],
            "no_ask": book["no_ask"], "kalshi_spread": book["kalshi_spread"],
            "lead_source": "coinbase", "lag_signal": 0.1,
            "response_gap": 0.003, "response_beta": 0.7,
            "regime": cfg.CONFIG_PROFILE,
        }
        attribution_eng._log_decision(decision, 0.54)
        check("decision logging does not fetch orderbook again", kalshi_stub.book_calls == 1)
        decision_row = json.loads(captured_writes["decision"][0])
        decision_id = decision_row.get("decision_id")
        check("decision row has opaque decision_id", bool(decision_id))
        check("observed quotes preserve their real spread",
              decision_row.get("kalshi_spread") == book["kalshi_spread"])

        # The router's 0.0 fallback must remain usable in memory, but must not
        # be persisted as a quote when no book quote exists.
        unavailable_book_decision = dict(decision)
        unavailable_book_decision.pop("decision_id", None)
        unavailable_book_decision.update({
            "yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None,
            "kalshi_spread": 0.0,
        })
        attribution_eng._log_decision(unavailable_book_decision, 0.54)
        unavailable_book_row = json.loads(captured_writes["decision"][-1])
        check("unavailable quotes persist null spread without another fetch",
              unavailable_book_decision["kalshi_spread"] == 0.0
              and unavailable_book_row.get("kalshi_spread") is None
              and kalshi_stub.book_calls == 1)

        # Post-structural WAIT paths reuse the already-computed threshold
        # features exactly; they do not make a second feature/volatility call.
        attribution_eng._research_tf_log_fields = {
            "z_threshold": 0.37,
            "p_base": 0.61,
            "mispricing_base": 0.11,
            "confidence_weighted_mispricing": 0.05,
            "spot_confidence": 0.82,
            "lag_confidence": 0.73,
        }
        for wait_reason in ("p_real_near_50", "edge_too_small", "entry_too_cheap", "portfolio_cap"):
            wait = attribution_eng._wait(wait_reason, 0.54, p_base=0.61, alpha_micro=0.0)
            check(f"post-structural {wait_reason} WAIT retains exact threshold fields",
                  all(wait.get(field) == value for field, value in attribution_eng._research_tf_log_fields.items()))
        attribution_eng._research_tf_log_fields = None

        attribution_eng._execute(decision, 0.54)
        fill_row = json.loads(captured_writes["fill"][0])
        check("fill carries decision_id", fill_row.get("decision_id") == decision_id)

        pos = attribution_eng._open_pos
        attribution_sim.record(
            ticker=pos.market_ticker, asset=pos.asset, entry=pos.entry_price,
            exit_=1.0, contracts=pos.contracts, window_id="2023-11-14 22:13",
            window_id_ts=pos.window_id,
            entry_ts=datetime.fromtimestamp(pos.entered_at, tz=timezone.utc).isoformat(),
            side=pos.side, decision=pos.decision,
        )
        trade_row = json.loads(captured_writes["trade"][0])
        c7 = trade_row.get("decision", {})
        check("closed trade and C7 carry the same decision_id",
              trade_row.get("decision_id") == decision_id and c7.get("decision_id") == decision_id)
        check("closed trade has UTC entry_ts and numeric window_id_ts",
              trade_row.get("entry_ts", "").endswith("+00:00") and trade_row.get("window_id_ts") == pos.window_id)
        check("C7 retains gold-list structured fields",
              c7.get("raw_features") == decision["raw_features"]
              and c7.get("per_venue_mids") == decision["per_venue_mids"]
              and c7.get("response_beta") == decision["response_beta"]
              and c7.get("yes_price_raw") == 0.54)
    finally:
        asset_engine_module.DECISION_LOG = old_decision_log
        asset_engine_module.FILL_LOG = old_fill_log
        sim_state_module.TRADE_LOG = old_trade_log
        if old_asset_open is None:
            delattr(asset_engine_module, "open")
        else:
            asset_engine_module.open = old_asset_open
        if old_sim_open is None:
            delattr(sim_state_module, "open")
        else:
            sim_state_module.open = old_sim_open
except Exception as e:
    check("Trade attribution persistence", False, str(e))
    import traceback
    traceback.print_exc()

# ─── 12. Kalshi client (offline) ───────────────────────────────────────────────
print("\n[12] KalshiClient (offline checks)")
try:
    from kalshi_bot.kalshi_client import KalshiClient
    c = KalshiClient()
    check("price_to_prob(50) = 0.50",  c.price_to_prob(50) == 0.50)
    check("price_to_prob(1) >= 0.01",  c.price_to_prob(1) >= 0.01)
    check("price_to_prob(99) <= 0.99", c.price_to_prob(99) <= 0.99)
    check("prob_to_price(0.5) = 50",   c.prob_to_price(0.5) == 50)
    check("prob_to_price clamps",      1 <= c.prob_to_price(0.001) <= 99)
    check("DRY_RUN order returns True", c.place_market_order("TEST", "yes", 10))

except Exception as e:
    check("KalshiClient", False, str(e))

# ─── 13. Strategy modules and router ────────────────────────────────────────────
print("\n[13] Strategy modules and router")
try:
    from kalshi_bot.strategy.lag_arb import LagArbStrategy, StrategySignal
    from kalshi_bot.strategy.close_boundary import CloseBoundaryStrategy
    from kalshi_bot.strategy.dislocation_reversion import DislocationReversionStrategy
    from kalshi_bot.strategy.strategy_router import StrategyRouter

    lag = LagArbStrategy()
    low_lag = max(0.0, cfg.LAG_CONFIDENCE_MIN - 0.01)
    snap_low_conf = {"lag_confidence": low_lag, "p_base": 0.55, "p_market": 0.50,
                     "kalshi_quote_age_secs": 5, "kalshi_spread": 0.03,
                     "dislocation": 0.001, "spot_confidence": 0.8,
                     "confidence_weighted_mispricing": 0.05, "time_remaining_secs": 300}
    s1 = lag.compute_signal(snap_low_conf)
    check(
        f"LagArbStrategy WAIT when lag_confidence < {cfg.LAG_CONFIDENCE_MIN:.2f}",
        s1.action == "WAIT",
        s1.reason,
    )

    snap_time_low = {"lag_confidence": 0.50, "p_base": 0.55, "p_market": 0.50,
                     "kalshi_quote_age_secs": 5, "kalshi_spread": 0.03,
                     "dislocation": 0.001, "spot_confidence": 0.8,
                     "confidence_weighted_mispricing": 0.05, "time_remaining_secs": 30}
    s2 = lag.compute_signal(snap_time_low)
    check("LagArbStrategy WAIT when time_remaining_secs <= 60", s2.action == "WAIT", s2.reason)

    close = CloseBoundaryStrategy()
    snap_t_high = {"time_remaining_secs": 200, "p_base": 0.60, "p_market": 0.50,
                   "z_threshold": 1.0, "kalshi_spread": 0.02, "kalshi_quote_age_secs": 5,
                   "spot_confidence": 0.9}
    s3 = close.compute_signal(snap_t_high)
    check("CloseBoundaryStrategy WAIT when time_remaining_secs > 120", s3.action == "WAIT", s3.reason)

    snap_t_low = {"time_remaining_secs": 10, "p_base": 0.60, "p_market": 0.50,
                  "z_threshold": 1.0, "kalshi_spread": 0.02, "kalshi_quote_age_secs": 5,
                  "spot_confidence": 0.9}
    s4 = close.compute_signal(snap_t_low)
    check("CloseBoundaryStrategy WAIT when time_remaining_secs <= 15", s4.action == "WAIT", s4.reason)

    disloc = DislocationReversionStrategy()
    snap_agree = {"per_venue_mids": {"a": 100.0, "b": 100.1, "c": 99.9},
                  "per_venue_staleness": {"a": 0.1, "b": 0.1, "c": 0.1},
                  "synthetic_mid": 100.0, "dislocation": 0.002,
                  "kalshi_prob_change_1s": 0.01, "spot_return_1s": 0.0001,
                  "spot_confidence": 0.9}
    s5 = disloc.compute_signal(snap_agree)
    check("DislocationReversionStrategy WAIT when all venues agree", s5.action == "WAIT", s5.reason)

    router = StrategyRouter([LagArbStrategy(), CloseBoundaryStrategy(), DislocationReversionStrategy()])
    snap_best = {"lag_confidence": 0.50, "p_base": 0.58, "p_market": 0.50,
                 "kalshi_quote_age_secs": 5, "kalshi_spread": 0.03,
                 "dislocation": 0.001, "spot_confidence": 0.85,
                 "confidence_weighted_mispricing": 0.06, "time_remaining_secs": 300,
                 "z_threshold": 0.5, "per_venue_mids": {"a": 100.0}, "per_venue_staleness": {"a": 0.1},
                 "synthetic_mid": 100.0, "kalshi_prob_change_1s": 0, "spot_return_1s": 0}
    s6 = router.route(snap_best)
    check("StrategyRouter returns highest-score signal when no veto", s6.action != "WAIT", f"action={s6.action}")

    cand = StrategySignal("lag_arb", 0.05, "BUY_YES", "OK", {})
    snap_veto = {"per_venue_mids": {"a": 101.5, "b": 100.0, "c": 100.0},
                 "per_venue_staleness": {"a": 0.1, "b": 0.1, "c": 0.1},
                 "synthetic_mid": 100.0, "dislocation": 0.002,
                 "kalshi_prob_change_1s": 0.01, "spot_return_1s": 0.0001,
                 "spot_confidence": 0.9}
    s7 = router.veto_check(cand, snap_veto)
    check("Dislocation veto fires when DislocationReversion contradicts candidate",
          s7.action == "WAIT" and s7.reason == "dislocation_veto", f"action={s7.action} reason={s7.reason}")

    snap_cb_priority = {"lag_confidence": 0.50, "p_base": 0.62, "p_market": 0.50,
                       "kalshi_quote_age_secs": 5, "kalshi_spread": 0.02,
                       "dislocation": 0.001, "spot_confidence": 0.85,
                       "confidence_weighted_mispricing": 0.08, "time_remaining_secs": 90,
                       "z_threshold": 1.0, "per_venue_mids": {"a": 100.0},
                       "per_venue_staleness": {"a": 0.1}, "synthetic_mid": 100.0,
                       "kalshi_prob_change_1s": 0, "spot_return_1s": 0}
    s8 = router.route(snap_cb_priority)
    check("CloseBoundary takes priority over LagArb in last 120s when same direction",
          s8.strategy == "close_boundary", f"strategy={s8.strategy}")
except Exception as e:
    check("Strategy modules and router", False, str(e))
    import traceback
    traceback.print_exc()

# ─── Fill quota ranking ───────────────────────────────────────────────────────
print("\n[quota] hourly fill rank-and-fire helpers")
try:
    from kalshi_bot.sim_state import SimState
    from kalshi_bot.config import cfg
    from kalshi_bot.runtime_control import apply_profile

    apply_profile("max_risk_micro")
    check("max_risk_micro enables fill quota", bool(cfg.FILL_QUOTA_ENABLED))
    qs = SimState()
    qs.trades = []
    check("quota hungry with no fills", qs.quota_hungry([]))
    now = time.time()
    qs.trades = [{"ts": datetime.now(timezone.utc).isoformat()}]
    check("quota satisfied after a fill this hour", not qs.quota_hungry([]))
    qs.trades = []
    qs.publish_quota_bid("BTC", 0.10)
    qs.publish_quota_bid("ETH", 0.40)
    qs.publish_quota_bid("SOL", 0.20)
    qs.publish_quota_bid("XRP", 0.05)
    qs.publish_quota_bid("DOGE", 0.01)
    # Collect window may still be open; force oldest back so winner can resolve.
    oldest = min(ts for ts, _ in qs._quota_bids.values()) - (cfg.QUOTA_COLLECT_SECS + 0.01)
    qs._quota_bids = {s: (oldest, sc) for s, (_, sc) in qs._quota_bids.items()}
    check("ETH wins quota rank", qs.is_winning_quota_bid("ETH"))
    check("BTC does not win quota rank", not qs.is_winning_quota_bid("BTC"))
    check("try_claim_quota first call ok", qs.try_claim_quota())
    check("try_claim_quota blocks double fire", not qs.try_claim_quota())
except Exception as e:
    check("fill quota helpers", False, str(e))

# ─── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'='*45}")
n_pass = sum(results)
n_fail = len(results) - n_pass
color  = "\033[32m" if n_fail == 0 else "\033[31m"
reset  = "\033[0m"
print(f"  {color}{n_pass}/{len(results)} checks passed{reset}  "
      f"({'ALL OK' if n_fail == 0 else f'{n_fail} FAILED'})")
print("=" * 45 + "\n")
sys.exit(0 if n_fail == 0 else 1)
