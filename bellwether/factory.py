"""Assembly: build the live object graph from a Config.

Centralizes the wiring (which venue, which strategies, which notifier) so the
CLI and tests construct the system the same way. The Kraken venue requires API
keys only for real-money orders (--live); paper mode and the simulator do not.
"""

from __future__ import annotations

from .config import Config
from .models import Instrument
from .portfolio import Portfolio
from .risk import RiskManager
from .signals.engine import SignalEngine
from .signals.momentum import MomentumStrategy
from .signals.trending import TrendingStrategy
from .storage import Storage
from .trader import Trader
from .venues.base import Venue

# Default Kraken universe if none is configured. ``pair`` is Kraken's market
# code (BTC is XBTUSD, DOGE is XDGUSD; most others are <SYMBOL>USD).
_DEFAULT_UNIVERSE = [
    {"symbol": "BTC", "pair": "XBTUSD", "name": "Bitcoin", "category": "Major"},
    {"symbol": "ETH", "pair": "ETHUSD", "name": "Ethereum", "category": "Major"},
    {"symbol": "SOL", "pair": "SOLUSD", "name": "Solana", "category": "L1"},
    {"symbol": "XRP", "pair": "XRPUSD", "name": "XRP", "category": "Payments"},
    {"symbol": "DOGE", "pair": "XDGUSD", "name": "Dogecoin", "category": "Meme"},
    {"symbol": "ADA", "pair": "ADAUSD", "name": "Cardano", "category": "L1"},
    {"symbol": "AVAX", "pair": "AVAXUSD", "name": "Avalanche", "category": "L1"},
    {"symbol": "LINK", "pair": "LINKUSD", "name": "Chainlink", "category": "DeFi"},
]


def _universe(cfg: Config) -> list[Instrument]:
    entries = cfg.strategy.universe or _DEFAULT_UNIVERSE
    return [
        Instrument(
            symbol=e["symbol"],
            name=e.get("name", e["symbol"]),
            category=e.get("category", ""),
            pair=e.get("pair", f"{e['symbol']}USD"),
        )
        for e in entries
    ]


def build_venue(cfg: Config, allow_live: bool = False) -> Venue:
    if not cfg.is_live:
        from .venues.paper import PaperVenue

        return PaperVenue(starting_cash=cfg.risk.starting_bankroll)

    from .venues.kraken import KrakenVenue

    # mode: kraken always uses REAL Kraken prices. Without --live it paper-fills
    # (no keys, no risk); with --live it places real orders (keys required).
    return KrakenVenue(
        instruments=_universe(cfg),
        api_key=cfg.kraken_api_key,
        api_secret=cfg.kraken_api_secret,
        paper=not allow_live,
        api_base=cfg.kraken.api_base,
        starting_cash=cfg.risk.starting_bankroll,
    )


def build_engine(cfg: Config, storage: "Storage | None" = None) -> SignalEngine:
    strategies = []
    if cfg.strategy.use_momentum:
        strategies.append((MomentumStrategy(storage=storage), cfg.strategy.momentum_weight))
    if cfg.strategy.use_trending and cfg.llm.enabled:
        from .signals.llm import build_client

        client = build_client(
            provider=cfg.llm.provider,
            model=cfg.llm.model,
            base_url=cfg.llm.base_url,
            api_key=cfg.llm_api_key(),
            max_tokens=cfg.llm.max_tokens,
        )
        if client is not None:
            news = None
            if cfg.news.enabled:
                from .news import NewsFeed

                news = NewsFeed(
                    feeds=cfg.news.feeds or None,
                    max_headlines=cfg.news.max_headlines,
                )
            trending = TrendingStrategy(
                client,
                news=news,
                max_symbols=cfg.strategy.max_symbols_per_cycle,
                per_coin_headlines=cfg.news.per_coin,
                general_headlines=cfg.news.general,
            )
            strategies.append((trending, cfg.strategy.trending_weight))
    if not strategies:
        strategies.append((MomentumStrategy(storage=storage), 1.0))
    return SignalEngine(strategies)


def build_notifier(cfg: Config):
    channel = cfg.notify.channel
    if channel == "email":
        from .notify.email import EmailNotifier

        return EmailNotifier(
            host=cfg.notify.smtp_host,
            port=cfg.notify.smtp_port,
            username=cfg.notify.email_from,
            password=cfg.smtp_password,
            to_addr=cfg.notify.email_to,
            from_addr=cfg.notify.email_from,
        )
    if channel == "sms":
        from .notify.sms import SMSNotifier

        return SMSNotifier(
            account_sid=cfg.twilio_sid,
            auth_token=cfg.twilio_token,
            from_number=cfg.notify.sms_from,
            to_number=cfg.notify.sms_to,
        )
    from .notify.console import ConsoleNotifier

    return ConsoleNotifier()


def build_trader(cfg: Config, allow_live: bool = False):
    """Construct the full system. Returns (trader, venue, portfolio, storage)."""
    storage = Storage(cfg.data_dir)
    venue = build_venue(cfg, allow_live=allow_live)
    portfolio = Portfolio(storage, cfg.risk.starting_bankroll)
    risk = RiskManager(cfg.risk, storage)
    engine = build_engine(cfg, storage)
    trader = Trader(cfg, venue, portfolio, risk, engine, storage)
    return trader, venue, portfolio, storage
