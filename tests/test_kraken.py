import base64

from bellwether.models import Action, Instrument, Order, OrderType, Quote
from bellwether.venues.kraken import KrakenVenue, _round_price


def _venue(paper=True):
    insts = [Instrument(symbol="BTC", name="Bitcoin", category="Major", pair="XBTUSD")]
    return KrakenVenue(insts, paper=paper)


def test_signature_is_stable_and_base64():
    # A valid base64-encoded secret (Kraken secrets are base64).
    secret = base64.b64encode(b"test-secret-key-material").decode()
    v = KrakenVenue([], api_key="k", api_secret=secret, paper=True)
    data = {"nonce": 123, "pair": "XBTUSD", "type": "buy"}
    sig1 = v._sign("/0/private/AddOrder", data)
    sig2 = v._sign("/0/private/AddOrder", data)
    assert sig1 == sig2  # deterministic for identical input
    assert base64.b64decode(sig1)  # valid base64


def test_live_requires_keys():
    import pytest

    with pytest.raises(RuntimeError):
        KrakenVenue([], paper=False)  # no keys


def test_paper_fill_respects_limit_and_charges_fee(monkeypatch):
    v = _venue(paper=True)
    # Stub the network ticker with a fixed quote.
    monkeypatch.setattr(
        v, "_ticker",
        lambda pair: Quote(symbol=pair, last=60000.0, bid=59990.0, ask=60010.0),
    )
    # A generous buy limit fills at the ask.
    order = Order(symbol="BTC", action=Action.BUY, quantity=0.01,
                  order_type=OrderType.LIMIT, limit_price=61000.0, pair="XBTUSD")
    fill = v.place_order(order)
    assert fill is not None
    assert abs(fill.price - 60010.0) < 1e-6  # filled at ask
    assert fill.commission > 0

    # A too-low buy limit does not fill.
    order2 = Order(symbol="BTC", action=Action.BUY, quantity=0.01,
                   order_type=OrderType.LIMIT, limit_price=50000.0, pair="XBTUSD")
    assert v.place_order(order2) is None


def test_round_price_precision():
    assert _round_price(60123.456) == 60123.5   # >=1000 -> 1 dp
    assert _round_price(38.123) == 38.12        # >=10 -> 2 dp
    assert _round_price(0.61234) == 0.612340    # <1 -> 6 dp


def test_paper_venue_does_not_reconcile():
    v = _venue(paper=True)
    assert v.reconciles is False
    assert v.account_snapshot() is None


def test_live_account_snapshot_maps_assets_and_balances(monkeypatch):
    insts = [
        Instrument("BTC", "Bitcoin", "Major", "XBTUSD"),
        Instrument("ETH", "Ethereum", "Major", "ETHUSD"),
    ]
    import base64

    secret = base64.b64encode(b"x").decode()
    v = KrakenVenue(insts, api_key="k", api_secret=secret, paper=False)
    assert v.reconciles is True

    # Stub AssetPairs (public) → maps altname -> base asset code.
    monkeypatch.setattr(v, "_public", lambda method, params: {
        "XXBTZUSD": {"altname": "XBTUSD", "base": "XXBT"},
        "XETHZUSD": {"altname": "ETHUSD", "base": "XETH"},
    })
    # Stub Balance (private) → real wallet holdings.
    monkeypatch.setattr(v, "_private", lambda method, data: {
        "ZUSD": "1234.56", "XXBT": "0.05", "XETH": "0.0",
    })

    snap = v.account_snapshot()
    assert snap is not None
    cash, positions = snap
    assert abs(cash - 1234.56) < 1e-6
    assert abs(positions["BTC"] - 0.05) < 1e-9   # XXBT -> BTC
    assert "ETH" not in positions               # zero balance omitted


def test_live_snapshot_none_when_assetpairs_fails(monkeypatch):
    import base64

    v = KrakenVenue(
        [Instrument("BTC", "Bitcoin", "Major", "XBTUSD")],
        api_key="k", api_secret=base64.b64encode(b"x").decode(), paper=False,
    )
    monkeypatch.setattr(v, "_public", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    # No asset map → snapshot returns None so the trader skips reconciliation.
    assert v.account_snapshot() is None
