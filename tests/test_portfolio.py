import tempfile

from bellwether.models import Action, Fill, Quote
from bellwether.portfolio import Portfolio
from bellwether.storage import Storage


def _portfolio(cash=10000.0):
    storage = Storage(tempfile.mkdtemp())
    return Portfolio(storage, cash), storage


def test_long_buy_then_sell_realizes_pnl():
    pf, _ = _portfolio()
    pf.apply_fill(Fill("AAPL", Action.BUY, 10, 100.0))   # cost $1000
    assert pf.position("AAPL").quantity == 10
    assert abs(pf.cash - (10000 - 1000)) < 1e-6

    realized = pf.apply_fill(Fill("AAPL", Action.SELL, 10, 120.0))  # +$200
    assert abs(realized - 200.0) < 1e-6
    assert pf.position("AAPL") is None
    assert abs(pf.realized_pnl - 200.0) < 1e-6


def test_weighted_average_cost_on_add():
    pf, _ = _portfolio()
    pf.apply_fill(Fill("X", Action.BUY, 10, 100.0))
    pf.apply_fill(Fill("X", Action.BUY, 10, 200.0))
    pos = pf.position("X")
    assert pos.quantity == 20
    assert abs(pos.avg_cost - 150.0) < 1e-6


def test_short_open_then_cover_realizes_pnl():
    pf, _ = _portfolio()
    # Short 10 @ $100 -> cash += $1000, position -10 @ 100.
    pf.apply_fill(Fill("X", Action.SELL, 10, 100.0))
    assert pf.position("X").quantity == -10
    assert abs(pf.cash - (10000 + 1000)) < 1e-6
    # Cover 10 @ $90 -> short profit +$100.
    realized = pf.apply_fill(Fill("X", Action.BUY, 10, 90.0))
    assert abs(realized - 100.0) < 1e-6
    assert pf.position("X") is None


def test_flip_long_to_short_books_partial_realized():
    pf, _ = _portfolio()
    pf.apply_fill(Fill("X", Action.BUY, 10, 100.0))         # long 10 @100
    realized = pf.apply_fill(Fill("X", Action.SELL, 15, 110.0))  # sell 15 @110
    # Closed 10 @ +10 = +$100 realized; remaining -5 short @ 110.
    assert abs(realized - 100.0) < 1e-6
    pos = pf.position("X")
    assert pos.quantity == -5
    assert abs(pos.avg_cost - 110.0) < 1e-6


def test_equity_accounts_for_short_correctly():
    pf, _ = _portfolio(10000.0)
    pf.apply_fill(Fill("X", Action.SELL, 10, 100.0))  # short; cash 11000
    # Price drops to 90 -> short gains $100 -> equity 10100.
    eq = pf.equity({"X": Quote("X", last=90.0)})
    assert abs(eq - 10100.0) < 1e-6


def test_state_persists_across_reload():
    storage = Storage(tempfile.mkdtemp())
    pf = Portfolio(storage, 10000.0)
    pf.apply_fill(Fill("X", Action.BUY, 5, 50.0))
    cash_before = pf.cash
    pf2 = Portfolio(storage, 10000.0)
    assert abs(pf2.cash - cash_before) < 1e-6
    assert pf2.position("X").quantity == 5
