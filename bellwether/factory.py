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


def _base_universe_entries(cfg: Config) -> list[dict]:
    return list(cfg.strategy.universe or _DEFAULT_UNIVERSE)


def _base_symbols(cfg: Config) -> set[str]:
    """The human-curated universe — never auto-retired or re-discovered."""
    return {e["symbol"].upper() for e in _base_universe_entries(cfg)}


def _universe(cfg: Config, storage: "Storage | None" = None) -> list[Instrument]:
    entries = _base_universe_entries(cfg)
    # Fold in coins the learning loop discovered (active + on-probation), so the
    # bot trades and journals them. New discoveries take effect on next start.
    if storage is not None and cfg.learning.enabled and cfg.learning.discovery_enabled:
        known = {e["symbol"].upper() for e in entries}
        for d in storage.discovered(["active", "probation"]):
            if d["symbol"].upper() in known:
                continue
            entries.append(
                {
                    "symbol": d["symbol"],
                    "pair": d["pair"] or f"{d['symbol']}USD",
                    "name": d["name"] or d["symbol"],
                    "category": d["category"] or "Discovered",
                }
            )
    return [
        Instrument(
            symbol=e["symbol"],
            name=e.get("name", e["symbol"]),
            category=e.get("category", ""),
            pair=e.get("pair", f"{e['symbol']}USD"),
        )
        for e in entries
    ]


def _build_llm_client(cfg: Config):
    """The shared LLM client for signals, reflection, and discovery (or None)."""
    if not cfg.llm.enabled:
        return None
    from .signals.llm import build_client

    return build_client(
        provider=cfg.llm.provider,
        model=cfg.llm.model,
        base_url=cfg.llm.base_url,
        api_key=cfg.llm_api_key(),
        max_tokens=cfg.llm.max_tokens,
    )


def build_venue(cfg: Config, allow_live: bool = False, storage: "Storage | None" = None) -> Venue:
    if not cfg.is_live:
        from .venues.paper import PaperVenue

        return PaperVenue(starting_cash=cfg.risk.starting_bankroll)

    from .venues.kraken import KrakenVenue

    # mode: kraken always uses REAL Kraken prices. Without --live it paper-fills
    # (no keys, no risk); with --live it places real orders (keys required).
    return KrakenVenue(
        instruments=_universe(cfg, storage),
        api_key=cfg.kraken_api_key,
        api_secret=cfg.kraken_api_secret,
        paper=not allow_live,
        api_base=cfg.kraken.api_base,
        starting_cash=cfg.risk.starting_bankroll,
    )


def build_memory(cfg: Config, storage: "Storage", client=None):
    from .learning.memory import ReflectionMemory

    return ReflectionMemory(storage, cfg.learning.memory_file, client=client)


def build_engine(cfg: Config, storage: "Storage | None" = None) -> SignalEngine:
    weight_fn = None
    lessons_provider = None
    if storage is not None and cfg.learning.enabled:
        from .learning.autotune import effective_weight

        def weight_fn(source: str, symbol: str, base: float, _s=storage) -> float:
            # Auto-tuned global strategy weight × learned per-coin reliability.
            return effective_weight(_s, source, base) * _s.reliability_multiplier(source, symbol)

        lessons_provider = build_memory(cfg, storage).lessons_text

    strategies = []
    if cfg.strategy.use_momentum:
        strategies.append((MomentumStrategy(storage=storage), cfg.strategy.momentum_weight))
    if cfg.strategy.use_trending and cfg.llm.enabled:
        client = _build_llm_client(cfg)
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
                lessons_provider=lessons_provider,
            )
            strategies.append((trending, cfg.strategy.trending_weight))
    if not strategies:
        strategies.append((MomentumStrategy(storage=storage), 1.0))
    return SignalEngine(strategies, weight_fn=weight_fn)


def build_reflector(cfg: Config, storage: "Storage", venue: "Venue | None" = None):
    """The daily learning job. Shares one LLM client for reflection + nomination."""
    from .learning.reflect import Reflector

    client = _build_llm_client(cfg) if cfg.learning.enabled else None
    memory = build_memory(cfg, storage, client=client)
    return Reflector(
        cfg,
        storage,
        memory,
        venue=venue,
        nominate_client=client,
        known_symbols=_base_symbols(cfg),
    )


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
    # Seed the in-memory config from the bot's persisted (bounded) overrides so a
    # restart preserves learned selection settings.
    if cfg.learning.enabled:
        from .learning.autotune import apply_overrides

        apply_overrides(cfg, storage)
    venue = build_venue(cfg, allow_live=allow_live, storage=storage)
    portfolio = Portfolio(storage, cfg.risk.starting_bankroll)
    probation_pct = cfg.learning.probation_size_pct if cfg.learning.enabled else 1.0
    risk = RiskManager(cfg.risk, storage, probation_size_pct=probation_pct)
    engine = build_engine(cfg, storage)
    trader = Trader(cfg, venue, portfolio, risk, engine, storage)
    return trader, venue, portfolio, storage
