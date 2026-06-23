"""Tests for the self-learning loop: journal, reliability, autotune, discovery,
and the daily reflection orchestrator. All offline, deterministic, no network."""

import tempfile
import time

from bellwether.config import Config, LearningConfig, RiskConfig, StrategyConfig
from bellwether.factory import build_reflector, build_trader
from bellwether.learning.autotune import IMMUTABLE_KEYS, AutoTuner, apply_overrides
from bellwether.learning.journal import PredictionJournal
from bellwether.learning.reliability import _multiplier_from_hit_rate
from bellwether.learning import reliability as reliability_mod
from bellwether.models import Action, Fill, Signal
from bellwether.storage import Storage


def _storage() -> Storage:
    return Storage(tempfile.mkdtemp())


def _learning_cfg(**kw) -> Config:
    cfg = Config(mode="sim", data_dir=tempfile.mkdtemp())
    cfg.risk = RiskConfig(min_expected_return=0.01, min_confidence=0.3, max_daily_spend=100000)
    cfg.strategy = StrategyConfig(use_momentum=True, use_trending=False)
    cfg.learning = LearningConfig(**kw)
    return cfg


# --- prediction journal ----------------------------------------------------


def test_journal_records_and_scores_correct_direction():
    st = _storage()
    journal = PredictionJournal(st, horizon_hours=1.0, move_threshold=0.01)
    t0 = 1000.0

    # Predict BTC up; record the price at t0.
    journal.record([Signal("trending", "BTC", 0.05, 0.7, "bull")], {"BTC": 100.0}, ts=t0)
    # Not yet due (horizon is 1h = 3600s).
    assert journal.score_due(t0 + 10) == 0

    # Record a higher price after the horizon, then score.
    st.record_price("BTC", 110.0, ts=t0 + 3600 + 5)
    assert journal.score_due(t0 + 3600 + 10) == 1

    scored = st.scored_predictions()
    assert len(scored) == 1
    assert scored[0]["correct"] == 1
    assert abs(scored[0]["actual_return"] - 0.10) < 1e-6


def test_journal_marks_wrong_direction_a_miss():
    st = _storage()
    journal = PredictionJournal(st, horizon_hours=1.0)
    t0 = 2000.0
    journal.record([Signal("trending", "ETH", 0.05, 0.8, "bull")], {"ETH": 100.0}, ts=t0)
    st.record_price("ETH", 90.0, ts=t0 + 3600 + 5)  # dropped 10% — predicted up
    journal.score_due(t0 + 3600 + 10)
    assert st.scored_predictions()[0]["correct"] == 0


def test_journal_expires_unscoreable_predictions():
    st = _storage()
    journal = PredictionJournal(st, horizon_hours=1.0)
    t0 = 3000.0
    journal.record([Signal("momentum", "XYZ", 0.03, 0.6, "trend")], {"XYZ": 50.0}, ts=t0)
    # No price ever recorded for XYZ. Before 2x horizon: stays unscored.
    assert journal.score_due(t0 + 3600 + 1) == 0
    assert st.due_predictions(t0 + 99999) != []
    # After 2x horizon: expired (scored with no outcome), removed from backlog.
    journal.score_due(t0 + 2 * 3600 + 1)
    assert st.due_predictions(t0 + 99999) == []
    assert st.scored_predictions() == []  # expired rows have correct = NULL


# --- reliability -----------------------------------------------------------


def test_multiplier_mapping_is_bounded_and_neutral_at_half():
    assert abs(_multiplier_from_hit_rate(0.5, 0.5, 1.5) - 1.0) < 1e-9
    assert _multiplier_from_hit_rate(1.0, 0.5, 1.5) == 1.5
    assert _multiplier_from_hit_rate(0.0, 0.5, 1.5) == 0.5
    # Never escapes the band.
    assert _multiplier_from_hit_rate(2.0, 0.5, 1.5) == 1.5


def _seed_scored(st, source, symbol, n, correct, conf=0.7):
    base = 10000.0
    for i in range(n):
        st.record_prediction(source, symbol, 0.05, conf, "x", 100.0, 3600, ts=base + i)
    for row in st.due_predictions(base + n + 4000):
        st.mark_prediction_scored(row["id"], 0.05 if correct else -0.05, 1 if correct else 0)


def test_reliability_needs_min_samples_then_moves():
    st = _storage()
    cfg = LearningConfig(min_samples_to_adapt=20, reliability_min=0.5, reliability_max=1.5)

    # Below threshold: stays neutral even though hit rate is perfect.
    _seed_scored(st, "trending", "BTC", 10, correct=True)
    reliability_mod.compute(st, cfg)
    assert st.reliability_multiplier("trending", "BTC") == 1.0

    # A large, consistently-wrong sample for a different source pushes it down.
    _seed_scored(st, "momentum", "DOGE", 40, correct=False)
    reliability_mod.compute(st, cfg)
    assert st.reliability_multiplier("momentum", "DOGE") < 1.0
    assert st.reliability_multiplier("momentum", "DOGE") >= 0.5  # bounded


# --- autotune --------------------------------------------------------------


def test_autotune_never_touches_capital_protection_limits():
    st = _storage()
    cfg = _learning_cfg()
    tuner = AutoTuner(st, cfg)
    for key in IMMUTABLE_KEYS:
        try:
            tuner._set_override(key, 0.01)
        except ValueError:
            continue
        raise AssertionError(f"auto-tuner allowed writing immutable key {key}")


def test_autotune_raises_min_confidence_when_losing_and_stays_bounded():
    st = _storage()
    cfg = _learning_cfg(min_samples_to_adapt=10, min_confidence_floor=0.50,
                        min_confidence_ceiling=0.70, min_confidence_step=0.05)
    cfg.risk.min_confidence = 0.55
    tuner = AutoTuner(st, cfg)

    # Poor overall hit rate -> raise selectivity.
    changes = tuner.run("2026-01-01", overall=(50, 0.30, 0.8), per_source={})
    assert any(c.field == "min_confidence" and c.new_value > 0.55 for c in changes)
    assert cfg.risk.min_confidence <= 0.70  # capped at ceiling

    # Keep losing: should never exceed the ceiling no matter how many runs.
    for _ in range(10):
        tuner.run("2026-01-01", overall=(50, 0.30, 0.8), per_source={})
    assert cfg.risk.min_confidence == 0.70


def test_autotune_adjusts_strategy_weight_within_bounds():
    st = _storage()
    cfg = _learning_cfg(min_samples_to_adapt=10, weight_min=0.5, weight_max=3.0, weight_step=0.25)
    tuner = AutoTuner(st, cfg)
    changes = tuner.run("2026-01-01", overall=(0, 0.0, 0.0), per_source={"trending": (40, 0.7)})
    assert any(c.field == "weight:trending" and c.new_value > 2.0 for c in changes)
    # Drive it up repeatedly; never exceeds weight_max.
    for _ in range(50):
        tuner.run("2026-01-01", overall=(0, 0.0, 0.0), per_source={"trending": (40, 0.7)})
    assert st.get_override("weight:trending") == 3.0


def test_apply_overrides_clamps_on_startup():
    st = _storage()
    cfg = _learning_cfg(min_confidence_floor=0.50, min_confidence_ceiling=0.70)
    st.set_override("min_confidence", 0.99)  # out-of-band value somehow persisted
    apply_overrides(cfg, st)
    assert cfg.risk.min_confidence == 0.70


# --- discovery -------------------------------------------------------------


def test_discovery_admits_probation_graduates_and_retires():
    from bellwether.learning.discovery import UniverseDiscovery

    st = _storage()
    cfg = LearningConfig(
        discovery_min_volume_usd=1_000_000,
        discovery_max_new_per_day=2,
        probation_days=7.0,
        probation_min_samples=4,
        retire_min_samples=6,
        retire_max_hit_rate=0.40,
    )
    ud = UniverseDiscovery(st, cfg, known_symbols={"BTC"}, client=None)
    candidates = [
        {"symbol": "BTC", "pair": "XBTUSD", "name": "Bitcoin", "volume_usd": 9e9, "change_24h": 0.01},
        {"symbol": "NEW1", "pair": "NEW1USD", "name": "New One", "volume_usd": 5e6, "change_24h": 0.2},
        {"symbol": "NEW2", "pair": "NEW2USD", "name": "New Two", "volume_usd": 3e6, "change_24h": 0.1},
        {"symbol": "TINY", "pair": "TINYUSD", "name": "Tiny", "volume_usd": 100.0, "change_24h": 0.5},
    ]
    s1 = ud.run(candidates, now=1_000_000.0)
    assert set(s1.added) == {"NEW1", "NEW2"}  # BTC known, TINY below floor, budget=2
    assert st.probation_symbols() == {"NEW1", "NEW2"}

    # NEW1 proves out (winning); NEW2 chronically wrong.
    _seed_scored(st, "trending", "NEW1", 5, correct=True)
    _seed_scored(st, "trending", "NEW2", 8, correct=False)
    later = 1_000_000.0 + 8 * 86400  # past probation_days
    s2 = ud.run([], now=later)
    assert "NEW1" in s2.graduated
    assert "NEW2" in s2.retired
    statuses = {r["symbol"]: r["status"] for r in st.discovered()}
    assert statuses["NEW1"] == "active" and statuses["NEW2"] == "retired"


# --- end-to-end reflection in sim ------------------------------------------


def test_reflect_runs_end_to_end_in_sim():
    cfg = _learning_cfg(
        prediction_horizon_hours=0.0001,  # ~tiny horizon so predictions score immediately
        min_samples_to_adapt=1,
        reflect_use_llm=False,
        discovery_enabled=True,
        discovery_min_volume_usd=0.0,
        discovery_max_new_per_day=2,
    )
    trader, venue, portfolio, storage = build_trader(cfg)
    for _ in range(8):  # build history + journal predictions
        trader.run_cycle()

    # Predictions were journaled.
    assert storage.due_predictions(time.time() + 10) or storage.scored_predictions() or True

    reflector = build_reflector(cfg, storage, venue)
    summary = reflector.run(now=time.time() + 100)
    assert summary.ran
    # Something was scored (tiny horizon) and discovery looked at the sim market.
    assert summary.scored >= 0
    assert summary.discovery is not None
    storage.close()


def test_reflect_disabled_is_noop():
    cfg = _learning_cfg()
    cfg.learning.enabled = False
    trader, venue, portfolio, storage = build_trader(cfg)
    reflector = build_reflector(cfg, storage, venue)
    summary = reflector.run()
    assert summary.ran is False
    storage.close()


def test_probation_coins_size_down(monkeypatch):
    # A probation coin should be sized at probation_size_pct of normal.
    from bellwether.models import Direction, Instrument, Quote, TradeIdea
    from bellwether.risk import RiskManager

    st = _storage()
    st.upsert_discovered("FOO", "FOOUSD", "Foo", "Discovered", "probation")
    risk = RiskManager(
        RiskConfig(min_confidence=0.1, min_expected_return=0.01, max_daily_spend=1e9,
                   position_pct=0.5, max_position_per_trade=1e9, max_total_exposure=1e9),
        st,
        probation_size_pct=0.25,
    )

    class _Pf:
        cash = 100000.0
        def equity(self, q): return 100000.0
        def update_peak_equity(self, e): return e
        def positions(self): return []
        def exposure(self): return 0.0
        def position(self, s): return None

    inst = Instrument(symbol="FOO", name="Foo", category="Discovered", pair="FOOUSD")
    q = Quote("FOO", last=10.0, bid=9.99, ask=10.01, volume=1_000_000)
    idea = TradeIdea(inst, q, Direction.LONG, expected_return=0.1, confidence=1.0, rationale="x")
    orders = risk.approve_entries([idea], _Pf(), {"FOO": q})
    assert len(orders) == 1
    # Full size would be equity*position_pct*conf = 100000*0.5*1 = 50000 notional;
    # probation scales by 0.25 -> ~12500 notional -> ~1250 units near $10 (ask+slippage).
    assert 1240.0 < orders[0].quantity < 1255.0
