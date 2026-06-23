"""Universe discovery: search out new coins, safely.

Daily, pull the venue's tradable USD pairs with their 24h volume, screen for a
liquidity floor, and (optionally) let the model nominate the most promising
additions. New coins never jump straight to full size:

  • they enter on **probation** — watched and traded at a fraction of normal
    size — until they've been live ``probation_days`` and accumulated enough
    scored predictions to judge;
  • a probationer with a winning track record **graduates** to active (full size);
  • any coin whose scored hit rate stays poor over a meaningful sample is
    **retired** (dropped from the watch list).

This keeps the bot discovering without chasing illiquid scams, and prunes the
coins that never pan out. The model's nomination is advisory and fail-soft: if
no model is available it falls back to ranking candidates by liquidity.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import LearningConfig
from ..signals.llm import LLMClient, extract_json
from ..storage import Storage

_NOMINATE_SYSTEM = (
    "You are a crypto analyst screening new coins for an automated trading bot. "
    "From the candidate list (each already passes a liquidity floor), pick the "
    "few most promising to start WATCHING at tiny size. Avoid obvious scams, "
    "low-quality memes, and anything you can't justify. Respond with ONLY JSON."
)


@dataclass
class DiscoverySummary:
    added: list[str] = field(default_factory=list)        # symbols put on probation
    graduated: list[str] = field(default_factory=list)    # probation -> active
    retired: list[str] = field(default_factory=list)      # dropped
    candidates_seen: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.graduated or self.retired)


class UniverseDiscovery:
    def __init__(
        self,
        storage: Storage,
        cfg: LearningConfig,
        known_symbols: set[str],
        client: LLMClient | None = None,
    ):
        self._storage = storage
        self._cfg = cfg
        self._known = {s.upper() for s in known_symbols}  # config universe (never re-added)
        self._client = client

    # --- per-coin track record (across all strategies) -------------------

    def _hit_rates(self) -> dict[str, tuple[int, float]]:
        agg: dict[str, list[int]] = defaultdict(list)
        for p in self._storage.scored_predictions():
            agg[p["symbol"]].append(1 if p["correct"] else 0)
        return {sym: (len(v), sum(v) / len(v)) for sym, v in agg.items() if v}

    # --- main entry -------------------------------------------------------

    def run(self, candidates: list[dict], now: float | None = None) -> DiscoverySummary:
        """``candidates`` is a list of {symbol, pair, name, volume_usd, change_24h}."""
        now = now or time.time()
        summary = DiscoverySummary(candidates_seen=len(candidates))
        hit_rates = self._hit_rates()

        self._graduate_and_retire(hit_rates, now, summary)
        self._admit_new(candidates, now, summary)
        return summary

    # --- graduate / retire existing discoveries --------------------------

    def _graduate_and_retire(self, hit_rates: dict, now: float, summary: DiscoverySummary) -> None:
        for row in self._storage.discovered(["probation", "active"]):
            symbol = row["symbol"]
            samples, hit_rate = hit_rates.get(symbol, (0, 0.0))

            # Retire chronic underperformers (enough evidence, poor hit rate).
            if samples >= self._cfg.retire_min_samples and hit_rate <= self._cfg.retire_max_hit_rate:
                self._storage.set_discovered_status(
                    symbol, "retired", notes=f"hit rate {hit_rate:.0%} over {samples} preds", ts=now
                )
                summary.retired.append(symbol)
                continue

            if row["status"] != "probation":
                continue

            # Graduate a probationer that's been around long enough and is winning.
            age_days = (now - row["added_ts"]) / 86400.0
            if (
                age_days >= self._cfg.probation_days
                and samples >= self._cfg.probation_min_samples
                and hit_rate >= 0.5
            ):
                self._storage.set_discovered_status(
                    symbol, "active", notes=f"graduated: hit rate {hit_rate:.0%} over {samples} preds", ts=now
                )
                summary.graduated.append(symbol)

    # --- admit new probationers ------------------------------------------

    def _admit_new(self, candidates: list[dict], now: float, summary: DiscoverySummary) -> None:
        budget = self._cfg.discovery_max_new_per_day
        if budget <= 0:
            return

        existing = {r["symbol"] for r in self._storage.discovered()}  # any status (don't re-admit retired)
        fresh = [
            c
            for c in candidates
            if c.get("symbol", "").upper() not in self._known
            and c.get("symbol", "").upper() not in existing
            and float(c.get("volume_usd", 0.0)) >= self._cfg.discovery_min_volume_usd
        ]
        if not fresh:
            return

        # Rank by liquidity as the safe default; the model may re-pick within this set.
        fresh.sort(key=lambda c: float(c.get("volume_usd", 0.0)), reverse=True)
        chosen = self._nominate(fresh, budget)

        for c in chosen[:budget]:
            sym = c["symbol"].upper()
            self._storage.upsert_discovered(
                symbol=sym,
                pair=c.get("pair", f"{sym}USD"),
                name=c.get("name", sym),
                category="Discovered",
                status="probation",
                notes=f"vol ${float(c.get('volume_usd', 0)):,.0f}/24h",
                ts=now,
            )
            summary.added.append(sym)

    def _nominate(self, candidates: list[dict], budget: int) -> list[dict]:
        """Let the model nominate from the liquid candidates; fall back to top-by-volume."""
        if self._client is None or len(candidates) <= budget:
            return candidates
        shortlist = candidates[: max(budget * 5, 10)]
        listing = "\n".join(
            f"- {c['symbol']}: ${float(c.get('volume_usd', 0)):,.0f} 24h vol, "
            f"{float(c.get('change_24h', 0)):+.1%} 24h"
            for c in shortlist
        )
        prompt = (
            f"Candidates (already liquid):\n{listing}\n\n"
            f"Pick up to {budget} symbols to start watching.\n"
            'Respond with JSON: {"picks": ["SYM1", "SYM2"]}'
        )
        try:
            text = self._client.complete_json(_NOMINATE_SYSTEM, prompt)
            picks = {str(s).upper() for s in json.loads(extract_json(text)).get("picks", [])}
            ranked = [c for c in shortlist if c["symbol"].upper() in picks]
            if ranked:
                return ranked
        except Exception:
            pass
        return candidates
