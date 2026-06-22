"""Integration tests for the live-trading safety path: reconcile against the
real wallet, and withhold new entries when that reconcile fails."""

import tempfile

from bellwether.config import Config, RiskConfig
from bellwether.models import Action, Fill, Instrument, Quote, Signal
from bellwether.portfolio import Portfolio
from bellwether.risk import RiskManager
from bellwether.signals.base import Strategy
from bellwether.signals.engine import SignalEngine
from bellwether.storage import Storage
from bellwether.trader import Trader


class _FakeKraken:
    """A live-style venue with a controllable account snapshot."""

    reconciles = True

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.orders = []

    def list_instruments(self, categories=None):
        return [Instrument("BTC", "Bitcoin", "Major", "XBTUSD")]

    def quotes(self, instruments):
        return {"BTC": Quote("BTC", last=60000.0, bid=59990.0, ask=60010.0)}

    def account_snapshot(self):
        return self._snapshot

    def place_order(self, order):
        self.orders.append(order)
        return Fill(order.symbol, order.action, order.quantity, order.limit_price or 60000.0)

    def balance(self):
        return 0.0


class _AlwaysBuy(Strategy):
    name = "alwaysbuy"

    def evaluate(self, instruments, quotes):
        return [Signal(self.name, i.symbol, expected_return=0.10, confidence=0.9, rationale="x")
                for i in instruments]


def _build(venue):
    storage = Storage(tempfile.mkdtemp())
    cfg = Config(mode="kraken", data_dir="x")
    cfg.risk = RiskConfig(min_expected_return=0.01, min_confidence=0.3, starting_bankroll=5000)
    pf = Portfolio(storage, cfg.risk.starting_bankroll)
    risk = RiskManager(cfg.risk, storage)
    engine = SignalEngine([(_AlwaysBuy(), 1.0)])
    return Trader(cfg, venue, pf, risk, engine, storage), pf, storage


def test_reconcile_applied_and_entry_allowed():
    venue = _FakeKraken(snapshot=(10000.0, {}))  # real wallet: $10k, no positions
    trader, pf, storage = _build(venue)
    report = trader.run_cycle()
    assert report.entries_skipped is False
    # Cash started from the reconciled wallet ($10k), then some was spent buying.
    assert 8000.0 < pf.cash < 10000.0
    # The strong signal produced a real BUY order.
    buys = [o for o in venue.orders if o.action is Action.BUY]
    assert len(buys) == 1 and buys[0].symbol == "BTC"
    storage.close()


def test_entries_withheld_when_reconcile_fails():
    venue = _FakeKraken(snapshot=None)  # snapshot failed → cannot confirm wallet
    trader, pf, storage = _build(venue)
    report = trader.run_cycle()
    assert report.entries_skipped is True
    # Despite a strong buy signal, NO order was placed (entries withheld).
    assert [o for o in venue.orders if o.action is Action.BUY] == []
    storage.close()


def test_reconcile_brings_in_real_position():
    venue = _FakeKraken(snapshot=(2000.0, {"BTC": 0.05}))  # wallet already holds BTC
    trader, pf, storage = _build(venue)
    trader.run_cycle()
    pos = pf.position("BTC")
    assert pos is not None and abs(pos.quantity - 0.05) < 1e-9
    # Already holding BTC → "one position per symbol" blocks a new buy; cash untouched.
    assert abs(pf.cash - 2000.0) < 1e-6
    storage.close()
