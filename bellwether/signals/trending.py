"""AI trending-topic strategy.

For each coin, an LLM acts as a calibrated analyst and returns the expected
return over a short horizon (a signed fraction) plus a confidence, reasoning
about how the asset's narrative is trending. The model is pluggable (see
``llm.py``) and defaults to a free open-source model via Ollama — so running the
AI signal costs nothing.

The strategy degrades gracefully: if no LLM client is configured/reachable or a
call fails, it returns no signals and the bot falls back to momentum-only — so
Bellwether always runs.
"""

from __future__ import annotations

import datetime as _dt
import json

from ..models import Instrument, Quote, Signal
from ..news import NewsFeed
from .base import Strategy
from .llm import LLMClient, extract_json

# Kept for providers that support strict JSON-schema output (e.g. Anthropic).
_SCHEMA = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "expected_return": {"type": "number"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["symbol", "expected_return", "confidence", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assessments"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a calibrated crypto analyst trading a fast-turnover book. For each "
    "coin, estimate the EXPECTED RETURN over roughly the NEXT 1-3 DAYS as a "
    "signed decimal fraction (e.g. +0.06 for +6%, -0.04 for -4%), and your "
    "confidence (0-1). Focus on near-term catalysts, momentum, and sentiment "
    "that can move price within days — and whether the market looks mispriced "
    "right now. Be well-calibrated: crypto is volatile and noisy, so keep "
    "confidence modest unless you have genuine conviction. Respond with ONLY a "
    "JSON object, no prose."
)

# Open-source models don't enforce a schema, so we spell the shape out.
_FORMAT = (
    'Respond with a JSON object of exactly this shape:\n'
    '{"assessments": [{"symbol": "BTC", "expected_return": 0.05, '
    '"confidence": 0.7, "rationale": "one sentence"}]}\n'
    "Include one entry for every symbol listed. expected_return is signed "
    "(negative if you expect a drop). Output JSON only."
)


class TrendingStrategy(Strategy):
    name = "trending"

    def __init__(
        self,
        client: LLMClient | None,
        news: NewsFeed | None = None,
        max_symbols: int = 25,
        per_coin_headlines: int = 3,
        general_headlines: int = 6,
        lessons_provider=None,
    ):
        self._client = client
        self._news = news
        self._max_symbols = max_symbols
        self._per_coin = per_coin_headlines
        self._general = general_headlines
        # Optional callable returning the bot's own trading-journal lessons, so
        # each day's analysis is informed by what past predictions got wrong.
        self._lessons_provider = lessons_provider
        self._error: str | None = None if client else "no LLM client configured"

    @property
    def available(self) -> bool:
        return self._client is not None

    def evaluate(
        self, instruments: list[Instrument], quotes: dict[str, Quote]
    ) -> list[Signal]:
        if self._client is None or not instruments:
            return []

        batch = instruments[: self._max_symbols]
        today = _dt.date.today().isoformat()

        # Pull current headlines once (fail-soft) so "trending" reflects today's
        # news, not the model's training cutoff.
        headlines = []
        if self._news is not None:
            try:
                headlines = self._news.headlines()
            except Exception:
                headlines = []

        lines = []
        for inst in batch:
            q = quotes.get(inst.symbol)
            px = f"${q.last:,.4f}" if q else "n/a"
            chg = ""
            if q and q.prev_close:
                chg = f", {((q.last / q.prev_close) - 1):+.1%} vs prev close"
            line = f"- {inst.symbol} ({inst.name or inst.symbol}, {inst.category}): {px}{chg}"
            if headlines:
                rel = NewsFeed.relevant(headlines, inst.symbol, inst.name, self._per_coin)
                for h in rel:
                    line += f"\n    • news: {h.title}"
            lines.append(line)

        general = ""
        if headlines and self._general > 0:
            top = "\n".join(f"- {h.title} ({h.source})" for h in headlines[: self._general])
            general = f"\nTop crypto headlines right now:\n{top}\n"

        lessons = ""
        if self._lessons_provider is not None:
            try:
                text = self._lessons_provider()
                if text:
                    lessons = f"\nLessons from your own past predictions (apply them):\n{text}\n"
            except Exception:
                lessons = ""

        prompt = (
            f"Today is {today}. Assess these {len(batch)} crypto assets, using the "
            f"recent news where relevant:\n"
            + "\n".join(lines)
            + "\n"
            + general
            + lessons
            + "\n"
            + _FORMAT
        )

        try:
            text = self._client.complete_json(_SYSTEM, prompt, schema=_SCHEMA)
            data = json.loads(extract_json(text))  # tolerate fenced/dirty output
        except Exception as exc:  # network / parse / API error → fail soft
            self._error = str(exc)
            return []

        signals = []
        for a in data.get("assessments", []):
            try:
                er = max(-0.5, min(0.5, float(a["expected_return"])))
                conf = max(0.0, min(1.0, float(a["confidence"])))
            except (KeyError, TypeError, ValueError):
                continue
            signals.append(
                Signal(
                    source=self.name,
                    symbol=str(a.get("symbol", "")),
                    expected_return=er,
                    confidence=conf,
                    rationale=str(a.get("rationale", ""))[:200],
                )
            )
        return signals
