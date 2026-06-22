"""Strategy interface.

A strategy looks at the current instruments and quotes and emits zero or more
Signals — each a directional view (signed expected return) plus a confidence.
Strategies are pluggable and composable; the engine blends whatever is enabled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Instrument, Quote, Signal


class Strategy(ABC):
    name: str = "strategy"

    @abstractmethod
    def evaluate(
        self, instruments: list[Instrument], quotes: dict[str, Quote]
    ) -> list[Signal]:
        """Produce signals for the given instruments."""
