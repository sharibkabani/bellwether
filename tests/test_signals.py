from bellwether.models import Direction, Instrument, Quote, Signal
from bellwether.signals.base import Strategy
from bellwether.signals.engine import SignalEngine
from bellwether.signals.momentum import MomentumStrategy
from bellwether.signals.trending import TrendingStrategy


class FixedStrategy(Strategy):
    """Returns a preset expected return — for deterministic engine tests."""

    name = "fixed"

    def __init__(self, expected_return, confidence=0.9):
        self._er = expected_return
        self._conf = confidence

    def evaluate(self, instruments, quotes):
        return [Signal(self.name, i.symbol, self._er, self._conf, "fixed") for i in instruments]


def _inst(symbol="AAPL"):
    return Instrument(symbol=symbol, name=symbol, category="Tech")


def _quotes(symbol="AAPL", last=100.0):
    return {symbol: Quote(symbol, last=last, bid=last - 0.05, ask=last + 0.05, volume=2_000_000)}


def test_engine_picks_long_on_positive_expectation():
    engine = SignalEngine([(FixedStrategy(+0.05), 1.0)])
    ideas = engine.generate([_inst()], _quotes())
    assert len(ideas) == 1
    assert ideas[0].direction is Direction.LONG
    assert abs(ideas[0].expected_return - 0.05) < 1e-6


def test_engine_picks_short_on_negative_expectation():
    engine = SignalEngine([(FixedStrategy(-0.04), 1.0)])
    ideas = engine.generate([_inst()], _quotes())
    assert ideas[0].direction is Direction.SHORT
    assert abs(ideas[0].expected_return - 0.04) < 1e-6


def test_engine_blends_weighted_sources():
    # +0.02 (w1) and +0.08 (w2) at equal confidence -> ~ +0.06.
    engine = SignalEngine([(FixedStrategy(0.02), 1.0), (FixedStrategy(0.08), 2.0)])
    ideas = engine.generate([_inst()], _quotes())
    assert 0.055 < ideas[0].expected_return < 0.065
    assert ideas[0].direction is Direction.LONG


def test_momentum_needs_history_then_signals_direction():
    strat = MomentumStrategy(window=6)
    inst = [_inst()]
    assert strat.evaluate(inst, _quotes(last=100)) == []
    assert strat.evaluate(inst, _quotes(last=102)) == []
    sigs = strat.evaluate(inst, _quotes(last=105))  # rising -> positive
    assert len(sigs) == 1
    assert sigs[0].expected_return > 0


def test_trending_graceful_without_client():
    strat = TrendingStrategy(client=None)
    assert strat.available is False
    assert strat.evaluate([_inst()], _quotes()) == []


class _StubClient:
    """A fake LLM that returns canned JSON — no network."""

    name = "stub"

    def __init__(self, payload: str):
        self._payload = payload

    def complete_json(self, system, user, schema=None):
        return self._payload


def test_trending_parses_client_json():
    payload = (
        '{"assessments": [{"symbol": "BTC", "expected_return": 0.08, '
        '"confidence": 0.7, "rationale": "ETF inflows trending"}]}'
    )
    strat = TrendingStrategy(client=_StubClient(payload))
    sigs = strat.evaluate([_inst("BTC")], _quotes("BTC"))
    assert len(sigs) == 1
    assert sigs[0].symbol == "BTC"
    assert abs(sigs[0].expected_return - 0.08) < 1e-9
    assert sigs[0].confidence == 0.7


def test_trending_tolerates_markdown_fenced_json():
    # Open models often wrap JSON in ```json fences — must still parse.
    payload = '```json\n{"assessments": [{"symbol": "ETH", "expected_return": -0.03, "confidence": 0.6, "rationale": "x"}]}\n```'
    strat = TrendingStrategy(client=_StubClient(payload))
    sigs = strat.evaluate([_inst("ETH")], _quotes("ETH"))
    assert len(sigs) == 1 and sigs[0].expected_return < 0


def test_trending_failsoft_on_bad_json():
    strat = TrendingStrategy(client=_StubClient("not json at all"))
    assert strat.evaluate([_inst("BTC")], _quotes("BTC")) == []
