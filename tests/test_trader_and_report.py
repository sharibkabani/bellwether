import tempfile

import pytest

from bellwether.config import Config, RiskConfig, StrategyConfig
from bellwether.factory import build_trader, build_venue
from bellwether.report import build_report, render_html, render_sms, render_text


def _sim_config():
    cfg = Config(mode="sim", data_dir=tempfile.mkdtemp())
    # Make the bot eager so a single run actually trades in the simulator.
    cfg.risk = RiskConfig(min_expected_return=0.01, min_confidence=0.3, max_daily_spend=100000)
    cfg.strategy = StrategyConfig(use_momentum=True, use_trending=False)
    return cfg


def test_full_cycle_runs_in_sim_mode():
    cfg = _sim_config()
    trader, venue, portfolio, storage = build_trader(cfg)
    last = None
    for _ in range(8):  # build momentum history, then trade
        last = trader.run_cycle()
    assert last is not None and last.equity > 0
    storage.close()


def test_entry_cadence_gating_skips_new_entries():
    cfg = _sim_config()
    trader, venue, portfolio, storage = build_trader(cfg)
    # Build enough price history that entries WOULD fire if allowed.
    for _ in range(6):
        trader.run_cycle(run_entries=True)
    # An exit-only cycle must not generate ideas or new entries.
    report = trader.run_cycle(run_entries=False)
    assert report.entries == []
    assert report.top_ideas == []
    storage.close()


def test_report_renders_all_formats():
    cfg = _sim_config()
    trader, venue, portfolio, storage = build_trader(cfg)
    for _ in range(8):
        trader.run_cycle()
    instruments = venue.list_instruments()
    quotes = venue.quotes(instruments)
    data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, "sim")

    assert "Bellwether daily report" in render_text(data)
    html = render_html(data)
    assert "<table" in html and "Bellwether" in html
    sms = render_sms(data)
    assert "Bellwether" in sms and len(sms) < 320


def test_kraken_live_without_keys_raises(monkeypatch):
    # Live Kraken (allow_live=True) with no API keys must raise (offline check).
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    cfg = Config(mode="kraken", data_dir=tempfile.mkdtemp())
    with pytest.raises(RuntimeError):
        build_venue(cfg, allow_live=True)


def test_kraken_paper_mode_builds_without_keys(monkeypatch):
    # Paper Kraken (allow_live=False) needs no keys — uses public data + sim fills.
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    cfg = Config(mode="kraken", data_dir=tempfile.mkdtemp())
    venue = build_venue(cfg, allow_live=False)  # should not raise (no network yet)
    assert venue.name == "kraken"
