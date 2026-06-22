from bellwether.models import Action, Order, OrderType
from bellwether.venues.paper import PaperVenue


def test_lists_instruments_and_quotes():
    venue = PaperVenue(starting_cash=5000)
    instruments = venue.list_instruments()
    assert len(instruments) > 5
    quotes = venue.quotes(instruments)
    for inst in instruments:
        q = quotes[inst.symbol]
        assert q.last > 0
        assert q.bid <= q.last <= q.ask


def test_category_filter():
    venue = PaperVenue(starting_cash=5000)
    majors = venue.list_instruments(categories=["Major"])
    assert majors and all(i.category == "Major" for i in majors)


def test_buy_fills_fractional_and_debits_cash():
    venue = PaperVenue(starting_cash=100000)
    inst = venue.list_instruments()[0]  # BTC
    order = Order(
        symbol=inst.symbol, action=Action.BUY, quantity=0.05,
        order_type=OrderType.LIMIT, limit_price=10_000_000,  # generous
    )
    fill = venue.place_order(order)
    assert fill is not None and abs(fill.quantity - 0.05) < 1e-9
    assert fill.commission > 0  # percentage fee
    assert venue.balance() < 100000


def test_buy_rejected_when_limit_too_low():
    venue = PaperVenue(starting_cash=100000)
    inst = venue.list_instruments()[0]
    order = Order(symbol=inst.symbol, action=Action.BUY, quantity=0.01,
                  order_type=OrderType.LIMIT, limit_price=0.01)
    assert venue.place_order(order) is None


def test_insufficient_funds():
    venue = PaperVenue(starting_cash=5.0)
    inst = venue.list_instruments()[0]
    order = Order(symbol=inst.symbol, action=Action.BUY, quantity=1.0,
                  order_type=OrderType.LIMIT, limit_price=10_000_000)
    assert venue.place_order(order) is None


def test_unknown_symbol_no_fill():
    venue = PaperVenue(starting_cash=5000)
    order = Order(symbol="NOPE", action=Action.BUY, quantity=1.0,
                  order_type=OrderType.LIMIT, limit_price=10_000_000)
    assert venue.place_order(order) is None
