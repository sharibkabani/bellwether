"""Signal engine: blend strategies into ranked trade ideas.

Each strategy contributes a signed expected return per symbol. The engine
combines them into one consensus expectation (weighted by each signal's
confidence and the strategy's configured weight), picks a direction from its
sign, and emits a TradeIdea. Risk checks happen later — the engine only finds
and ranks opportunities.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from ..models import Direction, Instrument, Quote, Signal, TradeIdea
from .base import Strategy


class SignalEngine:
    def __init__(
        self,
        strategies: list[tuple[Strategy, float]],
        weight_fn: Callable[[str, str, float], float] | None = None,
    ):
        self._strategies = strategies  # list of (strategy, base_weight)
        # weight_fn(source, symbol, base_weight) -> effective weight. Lets the
        # learning loop scale a source's trust per coin (reliability) and globally
        # (auto-tuned strategy weight) without the engine knowing the details.
        self._weight_fn = weight_fn or (lambda source, symbol, base: base)
        self.last_signals: list[Signal] = []  # raw per-strategy signals, for the journal

    def generate(
        self, instruments: list[Instrument], quotes: dict[str, Quote]
    ) -> list[TradeIdea]:
        by_symbol = {i.symbol: i for i in instruments}
        grouped: dict[str, list[tuple[Signal, float]]] = defaultdict(list)
        raw: list[Signal] = []
        for strategy, base_weight in self._strategies:
            for sig in strategy.evaluate(instruments, quotes):
                if sig.symbol in by_symbol and sig.symbol in quotes:
                    raw.append(sig)
                    weight = self._weight_fn(sig.source, sig.symbol, base_weight)
                    grouped[sig.symbol].append((sig, weight))
        self.last_signals = raw

        ideas = []
        for symbol, weighted in grouped.items():
            idea = self._combine(by_symbol[symbol], quotes[symbol], weighted)
            if idea is not None:
                ideas.append(idea)

        # Strongest conviction (expected return × confidence) first.
        ideas.sort(key=lambda i: i.expected_return * i.confidence, reverse=True)
        return ideas

    @staticmethod
    def _combine(
        instrument: Instrument, quote: Quote, weighted: list[tuple[Signal, float]]
    ) -> TradeIdea | None:
        num = 0.0
        den = 0.0
        rationales = []
        for sig, weight in weighted:
            w = sig.confidence * weight
            num += sig.expected_return * w
            den += w
            rationales.append(f"{sig.source}: {sig.expected_return:+.1%} ({sig.rationale})")
        if den == 0:
            return None

        net_er = num / den
        avg_confidence = den / sum(w for _, w in weighted)
        direction = Direction.LONG if net_er >= 0 else Direction.SHORT

        return TradeIdea(
            instrument=instrument,
            quote=quote,
            direction=direction,
            expected_return=abs(net_er),
            confidence=min(1.0, avg_confidence),
            rationale=" | ".join(rationales),
        )
