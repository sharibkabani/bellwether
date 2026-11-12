"""Regression tests for the Nov 2026 hardening pass: live order confirmation,
hard entry floors, stop-out cooldown, per-coin cap, baseline/peak healing,
flat-prediction grading, and stablecoin/alias filtering in discovery."""

import base64
import tempfile

from bellwether.config import LearningConfig, RiskConfig
from bellwether.learning.discovery import UniverseDiscovery, canonical_symbol, is_tradable_symbol
from bellwether.learning.journal import PredictionJournal
from bellwether.models import Action, Direction, Fill, Instrument, Order, OrderType, Quote, TradeIdea
from bellwether.portfolio import Portfolio
from bellwether.report import build_report
from bellwether.risk import MIN_CONFIDENCE_FLOOR, MIN_EDGE_FLOOR, RiskManager
from bellwether.storage import Storage
from bellwether.venues.kraken import KrakenVenue


# --- Kraken live orders ----------------------------------------------------

def _live_venue(monkeypatch, private):
    v = KrakenVenue(
        [Instrument("SOL", "Solana", "L1", "SOLCAD")],
        api_key="k",
        api_secret=base64.b64encode(b"x").decode(),
        paper=False,
    )
    v._FILL_POLL_SEC = 0
    monkeypatch.setattr(v, "_ticker", lambda pair: Quote(pair, last=100.0, bid=99.9, ask=100.1))
    monkeypatch.setattr(v, "_private", private)
    return v


def _order(order_type=OrderType.LIMIT):
    return Order(symbol="SOL", pair="SOLCAD", action=Action.BUY, quantity=0.5,
                 order_type=order_type, limit_price=101.0)


def test_kraken_gets_valid_ordertype_strings(monkeypatch):
    sent = []

    def private(method, data):
        if method == "AddOrder":
            sent.append(data["ordertype"])
            return {"txid": ["T1"]}
        return {"T1": {"status": "closed", "vol_exec": "0.5", "price": "100.05", "fee": "0.13"}}

    v = _live_venue(monkeypatch, private)
    v.place_order(_order(OrderType.LIMIT))
    v.place_order(_order(OrderType.MARKET))
    assert sent == ["limit", "market"]


def test_live_fill_books_real_price_qty_and_fee(monkeypatch):
    def private(method, data):
        if method == "AddOrder":
            return {"txid": ["T1"]}
        return {"T1": {"status": "closed", "vol_exec": "0.4", "price": "100.05", "fee": "0.11"}}

    fill = _live_venue(monkeypatch, private).place_order(_order())
    assert fill is not None
    assert abs(fill.quantity - 0.4) < 1e-9       # partial fill, not the requested 0.5
    assert abs(fill.price - 100.05) < 1e-9       # real avg price, not the 101 limit
    assert abs(fill.commission - 0.11) < 1e-9


def test_unfilled_limit_is_cancelled_and_not_booked(monkeypatch):
    calls = []

    def private(method, data):
        calls.append(method)
        if method == "AddOrder":
            return {"txid": ["T1"]}
        if method == "CancelOrder":
            return {"count": 1}
        status = "canceled" if "CancelOrder" in calls else "open"
        return {"T1": {"status": status, "vol_exec": "0", "price": "0", "fee": "0"}}

    fill = _live_venue(monkeypatch, private).place_order(_order())
    assert fill is None
    assert "CancelOrder" in calls


# --- risk rails ------------------------------------------------------------

def _risk(cfg):
    storage = Storage(tempfile.mkdtemp())
    pf = Portfolio(storage, cfg.starting_bankroll)
    return RiskManager(cfg, storage), pf, storage


def _idea(symbol="SOL", exp=0.05, conf=0.7, last=100.0):
    inst = Instrument(symbol=symbol, name=symbol, category="L1")
    quote = Quote(symbol=symbol, last=last, bid=last - 0.05, ask=last + 0.05)
    return TradeIdea(inst, quote, Direction.LONG, exp, conf, "t")


_LOOSE = dict(min_expected_return=-1.0, min_confidence=0.0, position_pct=1.0,
              max_position_per_trade=1e6, max_total_exposure=1e6, max_daily_spend=1e6,
              max_open_positions=50, starting_bankroll=100.0)


def test_hard_floors_hold_even_when_config_disables_guards():
    risk, pf, _ = _risk(RiskConfig(**_LOOSE))
    zero_edge = _idea(exp=0.0, conf=0.9)               # the Oct 29 DOGE buy
    coin_flip = _idea(exp=0.05, conf=MIN_CONFIDENCE_FLOOR - 0.01)
    assert risk.approve_entries([zero_edge], pf, {"SOL": zero_edge.quote}) == []
    assert risk.approve_entries([coin_flip], pf, {"SOL": coin_flip.quote}) == []
    ok = _idea(exp=MIN_EDGE_FLOOR + 0.01, conf=0.7)
    assert len(risk.approve_entries([ok], pf, {"SOL": ok.quote})) == 1


def test_single_coin_capped_at_half_equity():
    risk, pf, _ = _risk(RiskConfig(**_LOOSE))
    idea = _idea(exp=0.10, conf=1.0)
    (order,) = risk.approve_entries([idea], pf, {"SOL": idea.quote})
    assert order.quantity * idea.quote.last <= 50.0 + 1e-6


def test_stop_loss_benches_coin_so_it_is_not_rebought_same_cycle():
    risk, pf, _ = _risk(RiskConfig(**_LOOSE))
    pf.apply_fill(Fill("SOL", Action.BUY, 0.5, 100.0))
    crashed = {"SOL": Quote("SOL", last=85.0, bid=84.9, ask=85.1)}
    exits = risk.check_exits(pf, crashed)
    assert exits and exits[0].action is Action.SELL
    pf.apply_fill(Fill("SOL", Action.SELL, 0.5, 85.0))
    idea = _idea(exp=0.05, conf=0.8, last=85.0)
    assert risk.approve_entries([idea], pf, crashed) == []          # benched
    assert risk.in_cooldown("SOL")
    import time
    assert not risk.in_cooldown("SOL", now=time.time() + 25 * 3600)  # released after 24h


# --- baseline / peak -------------------------------------------------------

def _q(**prices):
    return {s: Quote(s, last=p, bid=p, ask=p) for s, p in prices.items()}


def test_first_live_sync_resets_peak_from_paper():
    storage = Storage(tempfile.mkdtemp())
    pf = Portfolio(storage, 5000.0)                 # paper era: peak 5000
    pf.reconcile(cash=20.0, balances={"SOL": 0.8}, quotes=_q(SOL=100.0))
    assert float(storage.get_meta("baseline_equity")) == 100.0
    assert float(storage.get_meta("peak_equity")) == 100.0


def test_zero_baseline_self_heals():
    storage = Storage(tempfile.mkdtemp())
    pf = Portfolio(storage, 5000.0)
    storage.set_meta("live_baseline_set", "1")
    storage.set_meta("baseline_equity", "0.0")      # what the CAD bug left behind
    pf.reconcile(cash=99.0, balances={}, quotes={})
    assert float(storage.get_meta("baseline_equity")) == 99.0


def test_real_baseline_is_not_overwritten():
    storage = Storage(tempfile.mkdtemp())
    pf = Portfolio(storage, 5000.0)
    storage.set_meta("live_baseline_set", "1")
    storage.set_meta("baseline_equity", "100.95")
    pf.reconcile(cash=80.0, balances={}, quotes={})
    assert float(storage.get_meta("baseline_equity")) == 100.95


# --- journal grading -------------------------------------------------------

def test_flat_prediction_is_not_graded():
    j = PredictionJournal(Storage(tempfile.mkdtemp()), move_threshold=0.01)
    assert j._grade(0.0001, 0.0) is None    # stablecoin "predict flat, stays flat" earns nothing
    assert j._grade(0.03, 0.002) == 0       # directional call on a flat tape is a miss
    assert j._grade(0.03, 0.02) == 1
    assert j._grade(-0.03, 0.02) == 0


# --- discovery -------------------------------------------------------------

def test_symbol_helpers():
    assert canonical_symbol("xbt") == "BTC"
    assert canonical_symbol("XDG") == "DOGE"
    for s in ("USDT", "USDC", "EUR", "CAD"):
        assert not is_tradable_symbol(s)
    assert is_tradable_symbol("NEAR")


def test_discovery_skips_stables_fiat_and_aliases_of_known_coins():
    storage = Storage(tempfile.mkdtemp())
    cfg = LearningConfig(discovery_max_new_per_day=10, discovery_min_volume_usd=0)
    d = UniverseDiscovery(storage, cfg, known_symbols={"BTC", "DOGE"})
    cands = [{"symbol": s, "pair": f"{s}USD", "volume_usd": 1e9}
             for s in ("USDT", "USDC", "EUR", "XBT", "XDG", "NEAR")]
    summary = d.run(cands)
    assert summary.added == ["NEAR"]


# --- report ----------------------------------------------------------------

def test_report_hides_stale_learning_when_disabled():
    storage = Storage(tempfile.mkdtemp())
    storage.record_reflection("2026-07-21", "old July lesson", "")
    pf = Portfolio(storage, 100.0)
    off = build_report(pf, storage, {}, 100.0, "kraken", learning_enabled=False)
    assert off.lessons == "" and off.reliability == [] and off.discovered == []
    on = build_report(pf, storage, {}, 100.0, "kraken", learning_enabled=True)
    assert "old July lesson" in on.lessons


# --- AI signal token budget ------------------------------------------------

def test_trending_reuses_last_answer_within_refresh_window():
    from bellwether.signals.trending import TrendingStrategy

    calls = []

    class FakeLLM:
        def complete_json(self, system, user, schema=None):
            calls.append(1)
            return '{"assessments": [{"symbol": "SOL", "expected_return": 0.03, "confidence": 0.6, "rationale": "x"}]}'

    insts = [Instrument("SOL", "Solana", "L1")]
    q = {"SOL": Quote("SOL", last=100.0, bid=100.0, ask=100.0)}
    t = TrendingStrategy(FakeLLM(), refresh_sec=1800)
    assert len(t.evaluate(insts, q)) == 1
    assert len(t.evaluate(insts, q)) == 1
    assert len(calls) == 1                      # second cycle served from cache
    t._cache_ts -= 1801
    t.evaluate(insts, q)
    assert len(calls) == 2                      # refreshed after the window


def test_trending_failure_is_logged_not_silent(capsys):
    from bellwether.signals.trending import TrendingStrategy

    class Broken:
        def complete_json(self, *a, **k):
            raise RuntimeError("429 Too Many Requests")

    t = TrendingStrategy(Broken())
    assert t.evaluate([Instrument("SOL")], {"SOL": Quote("SOL", 100.0)}) == []
    assert "429" in capsys.readouterr().err
