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


def test_daily_spend_budget_decrements():
    cfg = RiskConfig(max_daily_spend=1000.0)
    risk, pf, _ = _setup(cfg)
    assert risk.remaining_daily_spend() == 1000.0
    risk.record_entry_spend(400.0)
    assert abs(risk.remaining_daily_spend() - 600.0) < 1e-6
