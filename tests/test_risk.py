import tempfile

from bellwether.config import RiskConfig
from bellwether.models import Action, Direction, Fill, Instrument, Quote, TradeIdea
from bellwether.portfolio import Portfolio
from bellwether.risk import RiskManager
from bellwether.storage import Storage


def _setup(cfg=None):
    storage = Storage(tempfile.mkdtemp())
    cfg = cfg or RiskConfig()
    pf = Portfolio(storage, cfg.starting_bankroll)
    return RiskManager(cfg, storage), pf, storage


def _idea(symbol="AAPL", exp=0.06, conf=0.8, direction=Direction.LONG, last=100.0):
    inst = Instrument(symbol=symbol, name=symbol, category="Tech")
    quote = Quote(symbol=symbol, last=last, bid=last - 0.05, ask=last + 0.05)
    return TradeIdea(inst, quote, direction, exp, conf, "t")


def test_rejects_low_expected_return_and_confidence():
    risk, pf, _ = _setup()
    q = {"AAPL": _idea().quote}
    assert risk.approve_entries([_idea(exp=0.001)], pf, q) == []
    assert risk.approve_entries([_idea(conf=0.1)], pf, q) == []


def test_approves_strong_long_and_sizes_positive():
    risk, pf, _ = _setup()
    idea = _idea(exp=0.10, conf=0.9)
    orders = risk.approve_entries([idea], pf, {"AAPL": idea.quote})
    assert len(orders) == 1
    assert orders[0].action is Action.BUY
    assert orders[0].quantity > 0


def test_short_skipped_unless_allowed():
    idea = _idea(exp=0.10, conf=0.9, direction=Direction.SHORT)
    # default: long-only
    risk, pf, _ = _setup(RiskConfig(allow_short=False))
    assert risk.approve_entries([idea], pf, {"AAPL": idea.quote}) == []
    # enabled: produces a SELL-to-open
    risk2, pf2, _ = _setup(RiskConfig(allow_short=True))
    orders = risk2.approve_entries([idea], pf2, {"AAPL": idea.quote})
    assert len(orders) == 1 and orders[0].action is Action.SELL


def test_per_trade_cap_enforced():
    cfg = RiskConfig(max_position_per_trade=300.0, position_pct=1.0)
    risk, pf, _ = _setup(cfg)
    idea = _idea(exp=0.2, conf=1.0, last=100.0)
    orders = risk.approve_entries([idea], pf, {"AAPL": idea.quote})
    cost = orders[0].quantity * idea.entry_price
    assert cost <= 300.0 + idea.entry_price  # within one share of the cap


def test_kill_switch_blocks_entries():
    cfg = RiskConfig(max_drawdown_pct=0.10)
    risk, pf, storage = _setup(cfg)
    storage.set_meta("peak_equity", "20000.0")
    idea = _idea(exp=0.2, conf=1.0)
    assert risk.approve_entries([idea], pf, {"AAPL": idea.quote}) == []
    assert risk.kill_switch_triggered(equity=10000, peak_equity=20000) is True


def test_stop_loss_exit_long():
    cfg = RiskConfig(stop_loss_pct=0.08)
    risk, pf, _ = _setup(cfg)
    pf.apply_fill(Fill("AAPL", Action.BUY, 10, 100.0))
    exits = risk.check_exits(pf, {"AAPL": Quote("AAPL", last=90.0, bid=89.95, ask=90.05)})
    assert len(exits) == 1 and exits[0].action is Action.SELL
    assert "stop-loss" in exits[0].rationale


def test_take_profit_exit_long():
    cfg = RiskConfig(take_profit_pct=0.15)
    risk, pf, _ = _setup(cfg)
    pf.apply_fill(Fill("AAPL", Action.BUY, 10, 100.0))
    exits = risk.check_exits(pf, {"AAPL": Quote("AAPL", last=120.0, bid=119.9, ask=120.1)})
    assert len(exits) == 1 and "take-profit" in exits[0].rationale


def test_short_stop_loss_buys_to_cover():
    cfg = RiskConfig(stop_loss_pct=0.08, allow_short=True)
    risk, pf, _ = _setup(cfg)
    pf.apply_fill(Fill("X", Action.SELL, 10, 100.0))  # short @100
    # Price rises to 110 -> short down ~10% -> stop out by BUYING.
    exits = risk.check_exits(pf, {"X": Quote("X", last=110.0, bid=109.9, ask=110.1)})
    assert len(exits) == 1 and exits[0].action is Action.BUY


def _fast_cfg(**kw):
    base = dict(
        stop_loss_pct=0.20, take_profit_pct=0.50,
        partial_take_profit_pct=0.05, partial_take_fraction=0.5,
        trailing_stop_pct=0.04, trail_activate_pct=0.05,
    )
    base.update(kw)
    return RiskConfig(**base)


def test_partial_take_profit_then_trailing_stop():
    risk, pf, _ = _setup(_fast_cfg())
    pf.apply_fill(Fill("AAPL", Action.BUY, 10, 100.0))

    # +6% → sell half (quick money), the rest rides.
    orders = risk.check_exits(pf, {"AAPL": Quote("AAPL", last=106.0, bid=105.9, ask=106.1)})
    assert len(orders) == 1 and "partial take-profit" in orders[0].rationale
    assert abs(orders[0].quantity - 5.0) < 1e-9
    pf.apply_fill(Fill("AAPL", Action.SELL, orders[0].quantity, 106.0))

    # Climb to a new +10% peak — no further exit, and no second partial.
    assert risk.check_exits(pf, {"AAPL": Quote("AAPL", last=110.0, bid=109.9, ask=110.1)}) == []

    # Pull back 4.5% from the 110 peak → trailing stop closes the remainder.
    orders = risk.check_exits(pf, {"AAPL": Quote("AAPL", last=105.0, bid=104.9, ask=105.1)})
    assert len(orders) == 1 and "trailing stop" in orders[0].rationale
    assert abs(orders[0].quantity - 5.0) < 1e-9


def test_trailing_inactive_before_activation_threshold():
    risk, pf, _ = _setup(_fast_cfg())
    pf.apply_fill(Fill("AAPL", Action.BUY, 10, 100.0))
    # Up only +3% (below the 5% activation), then a small pullback → no exit.
    assert risk.check_exits(pf, {"AAPL": Quote("AAPL", last=103.0, bid=102.9, ask=103.1)}) == []
    assert risk.check_exits(pf, {"AAPL": Quote("AAPL", last=101.5, bid=101.4, ask=101.6)}) == []


def test_partial_take_profit_fires_once():
    risk, pf, _ = _setup(_fast_cfg())
    pf.apply_fill(Fill("AAPL", Action.BUY, 10, 100.0))
    o1 = risk.check_exits(pf, {"AAPL": Quote("AAPL", last=106.0, bid=105.9, ask=106.1)})
    assert len(o1) == 1 and "partial" in o1[0].rationale
    pf.apply_fill(Fill("AAPL", Action.SELL, o1[0].quantity, 106.0))
    # Still in profit but no new peak drawdown and partial already taken → nothing.
    o2 = risk.check_exits(pf, {"AAPL": Quote("AAPL", last=107.0, bid=106.9, ask=107.1)})
    assert o2 == []


def test_fresh_entry_clears_stale_exit_state():
    risk, pf, storage = _setup(_fast_cfg(min_expected_return=0.01, min_confidence=0.1,
                                         max_daily_spend=1e9, position_pct=0.1,
                                         max_position_per_trade=1e9, max_total_exposure=1e9))
    # Leftover peak/partial state from a prior position in the same symbol.
    storage.set_meta("exit_peak:AAPL", "999.0")
    storage.set_meta("exit_partial:AAPL", "1")
    idea = _idea(symbol="AAPL", exp=0.1, conf=0.9, last=100.0)
    orders = risk.approve_entries([idea], pf, {"AAPL": idea.quote})
    assert len(orders) == 1
    assert storage.get_meta("exit_peak:AAPL") is None
    assert storage.get_meta("exit_partial:AAPL") is None


def test_daily_spend_budget_decrements():
    cfg = RiskConfig(max_daily_spend=1000.0)
    risk, pf, _ = _setup(cfg)
    assert risk.remaining_daily_spend() == 1000.0
    risk.record_entry_spend(400.0)
    assert abs(risk.remaining_daily_spend() - 600.0) < 1e-6
