"""Configuration loading.

Settings come from a YAML file with environment-variable overrides for anything
sensitive. Live Kraken trading needs an API key/secret (set in the environment,
never the YAML); paper mode and public market data need no credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class RiskConfig:
    starting_bankroll: float = 5000.0          # paper starting cash (USD)
    max_position_per_trade: float = 800.0      # max $ in any single position
    max_total_exposure: float = 4000.0         # max gross $ deployed
    max_daily_spend: float = 2000.0            # max $ of new entries per day
    max_open_positions: int = 6
    position_pct: float = 0.15                 # base fraction of equity per trade
    min_expected_return: float = 0.04          # require a 4%+ expected move to enter
    min_confidence: float = 0.55               # require 55%+ signal confidence
    stop_loss_pct: float = 0.10                # close a position down 10% (crypto is volatile)
    take_profit_pct: float = 0.20              # close a position up 20%
    max_drawdown_pct: float = 0.25             # kill switch: halt if equity down 25%
    allow_short: bool = False                  # Kraken spot is long-only; keep False


@dataclass
class StrategyConfig:
    use_momentum: bool = True
    use_trending: bool = True                  # the AI / Claude signal
    momentum_weight: float = 1.0
    trending_weight: float = 2.0               # trust the AI signal more
    categories: list[str] = field(default_factory=list)   # empty = all
    max_symbols_per_cycle: int = 25            # cap AI calls per cycle for cost
    # Kraken trading universe: list of {symbol, pair, name, category}.
    universe: list[dict] = field(default_factory=list)


@dataclass
class LLMConfig:
    enabled: bool = True
    provider: str = "groq"        # groq | ollama | openrouter | openai | anthropic
    model: str = ""               # blank = the provider's default model
    base_url: str = ""            # blank = the provider's default endpoint
    max_tokens: int = 2000


@dataclass
class NewsConfig:
    enabled: bool = True
    feeds: list[str] = field(default_factory=list)  # empty = built-in crypto RSS feeds
    max_headlines: int = 40
    per_coin: int = 3
    general: int = 6


@dataclass
class KrakenConfig:
    api_base: str = "https://api.kraken.com"


@dataclass
class NotifyConfig:
    channel: str = "console"                   # console | email | sms
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    sms_to: str = ""
    sms_from: str = ""


@dataclass
class Config:
    mode: str = "sim"                          # sim (offline simulator) | kraken (real prices)
    data_dir: str = "./bellwether-data"
    poll_interval_sec: int = 900               # 15 min between trading cycles
    daily_report_hour: int = 17
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    # --- secrets / env ---
    def llm_api_key(self) -> str:
        """API key for the configured LLM provider (empty for Ollama/local)."""
        env_var = {
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }.get(self.llm.provider, "")
        return os.environ.get(env_var, "") if env_var else ""

    @property
    def kraken_api_key(self) -> str:
        return os.environ.get("KRAKEN_API_KEY", "")

    @property
    def kraken_api_secret(self) -> str:
        return os.environ.get("KRAKEN_API_SECRET", "")

    @property
    def smtp_password(self) -> str:
        return os.environ.get("SMTP_PASSWORD", "")

    @property
    def twilio_sid(self) -> str:
        return os.environ.get("TWILIO_ACCOUNT_SID", "")

    @property
    def twilio_token(self) -> str:
        return os.environ.get("TWILIO_AUTH_TOKEN", "")

    @property
    def is_live(self) -> bool:
        return self.mode == "kraken"


def _build(cls, data: dict[str, Any]):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | None = None) -> Config:
    data: dict[str, Any] = {}
    if path and os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    cfg = _build(Config, data)
    if "risk" in data:
        cfg.risk = _build(RiskConfig, data["risk"])
    if "strategy" in data:
        cfg.strategy = _build(StrategyConfig, data["strategy"])
    if "llm" in data:
        cfg.llm = _build(LLMConfig, data["llm"])
    if "news" in data:
        cfg.news = _build(NewsConfig, data["news"])
    if "kraken" in data:
        cfg.kraken = _build(KrakenConfig, data["kraken"])
    if "notify" in data:
        cfg.notify = _build(NotifyConfig, data["notify"])
    return cfg
