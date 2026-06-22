import tempfile

from bellwether.models import Action, Fill, Quote
from bellwether.portfolio import Portfolio
from bellwether.storage import Storage


def _pf(cash=5000.0):
    return Portfolio(Storage(tempfile.mkdtemp()), cash)


def _quotes(**prices):
    return {s: Quote(s, last=p, bid=p * 0.999, ask=p * 1.001) for s, p in prices.items()}


def test_reconcile_sets_cash_to_real_wallet():
    pf = _pf(5000)
    pf.reconcile(cash=8123.45, balances={}, quotes={})
    assert abs(pf.cash - 8123.45) < 1e-6


def test_reconcile_keeps_avg_cost_of_tracked_position():
    pf = _pf(5000)
    pf.apply_fill(Fill("BTC", Action.BUY, 0.1, 60000.0))  # bot opened: avg 60000
    # Broker says we actually hold 0.1 BTC; price now 64000.
    pf.reconcile(cash=4000.0, balances={"BTC": 0.1}, quotes=_quotes(BTC=64000.0))
    pos = pf.position("BTC")
    assert pos is not None
    assert abs(pos.quantity - 0.1) < 1e-9
    assert abs(pos.avg_cost - 60000.0) < 1e-6  # cost basis preserved for stop-loss


def test_reconcile_adopts_untracked_holding_at_current_price():
    pf = _pf(5000)
    pf.reconcile(cash=1000.0, balances={"ETH": 2.0}, quotes=_quotes(ETH=3000.0))
    pos = pf.position("ETH")
    assert pos is not None and abs(pos.quantity - 2.0) < 1e-9
    assert abs(pos.avg_cost - 3000.0) < 1e-6  # neutral basis → no false exit


def test_reconcile_drops_positions_broker_no_longer_shows():
    pf = _pf(5000)
    pf.apply_fill(Fill("SOL", Action.BUY, 10.0, 150.0))
    assert pf.position("SOL") is not None
    # Broker shows no SOL (sold elsewhere / never settled).
    pf.reconcile(cash=5000.0, balances={}, quotes=_quotes(SOL=150.0))
    assert pf.position("SOL") is None


def test_reconcile_ignores_dust():
    pf = _pf(5000)
    # 0.000001 BTC at 60k = $0.06 < $1 dust threshold → not a position.
    pf.reconcile(cash=5000.0, balances={"BTC": 0.000001}, quotes=_quotes(BTC=60000.0))
    assert pf.position("BTC") is None


def test_reconcile_updates_quantity_to_broker_truth():
    pf = _pf(5000)
    pf.apply_fill(Fill("BTC", Action.BUY, 0.1, 60000.0))
    # Broker actually shows 0.0995 (e.g. fee taken in kind) → trust the broker.
    pf.reconcile(cash=4000.0, balances={"BTC": 0.0995}, quotes=_quotes(BTC=60000.0))
    assert abs(pf.position("BTC").quantity - 0.0995) < 1e-9


def test_reconcile_rebaselines_pnl_on_first_live_sync():
    storage = Storage(tempfile.mkdtemp())
    pf = Portfolio(storage, 5000.0)            # config notional baseline
    assert storage.get_meta("baseline_equity") == "5000.0"
    pf.reconcile(cash=20000.0, balances={}, quotes={})  # real wallet has $20k
    # Baseline re-set to the real equity so all-time P&L starts from reality.
    assert abs(float(storage.get_meta("baseline_equity")) - 20000.0) < 1e-6
    assert storage.get_meta("live_baseline_set") == "1"
