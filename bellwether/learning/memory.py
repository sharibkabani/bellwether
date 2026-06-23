"""Reflection memory: the bot keeps a trading journal.

This is the cleanest "learn from your mistakes" mechanism and it's free — no
retraining, just better context. Each day the model is shown its own scorecard
(hit rate and calibration per coin/strategy, recent P&L) plus yesterday's
lessons, and asked to write a few short, concrete lessons for tomorrow:

    "I've been overconfident on memecoins; my BTC calls track news well; XRP
     rationales keep citing chart patterns that don't pan out."

Those lessons are persisted (DB + ``lessons.md``) and injected into the next
day's analyst prompt. It's exactly how a disciplined human reviews their journal.

Fail-soft throughout: if no model is configured or a call fails, the previous
lessons stand and trading is unaffected.
"""

from __future__ import annotations

import json
import os

from ..signals.llm import LLMClient, extract_json
from ..storage import Storage

_SYSTEM = (
    "You are the trading journal of an autonomous crypto bot. You are given the "
    "bot's own recent scorecard: how often each strategy/coin's directional "
    "predictions were right, whether it was over- or under-confident, and its "
    "realized P&L. Write 3-6 short, concrete, actionable lessons for the next "
    "session — what to trust more, what to distrust, where confidence should be "
    "dialed down. Be specific about coins and strategies. Do NOT recommend "
    "changing risk limits. Respond with ONLY JSON."
)
_FORMAT = (
    'Respond with a JSON object of exactly this shape:\n'
    '{"lessons": ["short lesson one", "short lesson two"]}\n'
    "Each lesson is one sentence. Output JSON only."
)

_MAX_LESSONS_CHARS = 1500


class ReflectionMemory:
    def __init__(self, storage: Storage, memory_file: str, client: LLMClient | None = None):
        self._storage = storage
        self._file = memory_file
        self._client = client

    def lessons_text(self) -> str:
        """The most recent lessons, for injection into the analyst prompt."""
        latest = self._storage.latest_reflection()
        if latest and latest.get("lessons"):
            return str(latest["lessons"])[:_MAX_LESSONS_CHARS]
        # Fall back to the file (e.g. a human-seeded lessons.md).
        try:
            with open(self._file) as f:
                return f.read()[:_MAX_LESSONS_CHARS]
        except OSError:
            return ""

    def generate_lessons(self, scorecard_text: str) -> str:
        """Ask the model to write lessons from the scorecard. Returns '' on failure."""
        if self._client is None:
            return ""
        prior = self.lessons_text()
        prior_block = f"\nYesterday's lessons:\n{prior}\n" if prior else ""
        prompt = f"Recent scorecard:\n{scorecard_text}\n{prior_block}\n{_FORMAT}"
        try:
            text = self._client.complete_json(_SYSTEM, prompt)
            data = json.loads(extract_json(text))
            lessons = data.get("lessons", [])
            if isinstance(lessons, str):
                lessons = [lessons]
            cleaned = [f"- {str(item).strip()}" for item in lessons if str(item).strip()]
            return "\n".join(cleaned)[:_MAX_LESSONS_CHARS]
        except Exception:
            return ""

    def save(self, day: str, lessons: str, scorecard: dict) -> None:
        """Persist lessons to the DB and append to the markdown journal."""
        self._storage.record_reflection(day, lessons, json.dumps(scorecard))
        if not lessons:
            return
        try:
            os.makedirs(os.path.dirname(self._file) or ".", exist_ok=True)
            with open(self._file, "a") as f:
                f.write(f"\n## {day}\n{lessons}\n")
        except OSError:
            pass  # journal file is a convenience; the DB is the source of truth
