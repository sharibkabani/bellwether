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
    min_expected_return: float = 0.03          # require a 3%+ expected move to enter (must clear ~0.5% fees)
    min_confidence: float = 0.55               # require 55%+ signal confidence
    stop_loss_pct: float = 0.07                # cut a loser at -7% (crypto is volatile)
    take_profit_pct: float = 0.25              # hard ceiling: fully exit at +25% (let runners run; trailing usually fires first)
    max_drawdown_pct: float = 0.25             # kill switch: halt if equity down 25%
    allow_short: bool = False                  # Kraken spot is long-only; keep False
    # --- faster turnover: bank gains, keep runners (long positions only) ---
    partial_take_profit_pct: float = 0.05      # at +5%, sell part of the position (quick money)
    partial_take_fraction: float = 0.5         # ...this fraction of it; the rest rides
    trailing_stop_pct: float = 0.04            # then trail the remainder: exit if it falls 4% from its peak
    trail_activate_pct: float = 0.05           # only start trailing once a position has been up this much


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
class LearningConfig:
    """The self-learning loop. Capital-protection limits in RiskConfig
    (max_position_per_trade, max_daily_spend, max_drawdown_pct, stop_loss_pct)
    are human-owned and NEVER touched by the bot. Everything here governs how
    the bot adapts *selection* — who it trusts and what it watches — within
    hard bounds it cannot exceed."""

    enabled: bool = True
    # Prediction journal / scoring
    prediction_horizon_hours: float = 24.0     # when a logged prediction gets scored
    min_samples_to_adapt: int = 20             # require N scored samples before adapting
    move_threshold: float = 0.01               # |move| under 1% counts as "flat" (not a hit)

    # Reliability weights (bounded trust multiplier per strategy x coin)
    reliability_min: float = 0.5
    reliability_max: float = 1.5
    reliability_prior_strength: float = 10.0   # regularize toward 1.0 (slow learning)

    # Bounded config auto-tuning (selection aggressiveness only)
    autotune_enabled: bool = True
    min_confidence_floor: float = 0.50         # bot may not drop below this
    min_confidence_ceiling: float = 0.70       # bot may not rise above this
    min_confidence_step: float = 0.02          # max nudge per reflection
    weight_min: float = 0.5                    # strategy-weight floor
    weight_max: float = 3.0                    # strategy-weight ceiling
    weight_step: float = 0.25                  # max weight nudge per reflection

    # Universe discovery
    discovery_enabled: bool = True
    discovery_min_volume_usd: float = 5_000_000.0   # liquidity floor for new coins
    discovery_max_new_per_day: int = 3
    probation_days: float = 7.0                # watch a new coin this long before graduating
    probation_min_samples: int = 8            # and require this many scored predictions
    probation_size_pct: float = 0.25          # probation coins trade at 25% of normal size
    retire_min_samples: int = 12              # only retire after enough evidence
    retire_max_hit_rate: float = 0.40         # retire a coin whose hit rate stays this low

    # Reflection memory
    memory_file: str = "memory/lessons.md"
    reflect_use_llm: bool = True               # have the model write its own journal


@dataclass
class NewsConfig:
    enabled: bool = True
    feeds: list[str] = field(default_factory=list)  # empty = built-in crypto RSS feeds
    max_headlines: int = 40
    per_coin: int = 3
    general: int = 6


@dataclass
class BlogConfig:
    """Publish a daily public blog of the bot's findings + learnings to GitHub
    Pages. Off by default until a repo is configured."""

    enabled: bool = False
    title: str = "Bellwether — an autonomous AI crypto trading journal"
    repo_url: str = ""            # e.g. https://github.com/<you>/<repo>.git (NO token here)
    branch: str = "main"
    subdir: str = ""              # "" = repo root, or "docs" (match your Pages source)
    base_url: str = ""            # e.g. https://<you>.github.io/<repo> (for links/meta)
    include_dollars: bool = False  # publish % returns + narrative only; never $ balances


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
    poll_interval_sec: int = 900               # seconds between cycles (exits/trailing checked every cycle)
    entry_interval_sec: int = 900              # how often to hunt NEW entries (LLM + journal); >= poll_interval
    daily_report_hour: int = 17
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    blog: BlogConfig = field(default_factory=BlogConfig)
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
    def github_token(self) -> str:
        """Token used to push the blog to GitHub Pages (write access to the repo)."""
        return os.environ.get("GITHUB_TOKEN", "") or os.environ.get("BLOG_GITHUB_TOKEN", "")

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
    if "learning" in data:
        cfg.learning = _build(LearningConfig, data["learning"])
    if "news" in data:
        cfg.news = _build(NewsConfig, data["news"])
    if "kraken" in data:
        cfg.kraken = _build(KrakenConfig, data["kraken"])
    if "blog" in data:
        cfg.blog = _build(BlogConfig, data["blog"])
    if "notify" in data:
        cfg.notify = _build(NotifyConfig, data["notify"])
    return cfg
