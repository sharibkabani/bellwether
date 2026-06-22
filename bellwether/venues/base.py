"""The venue interface.

A venue is anything Bellwether can read instruments/quotes from and place orders
against. The offline simulator and the live Kraken client
both implement this, so the rest of the bot is identical whether it's trading
real money or not — only which class gets constructed changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Fill, Instrument, Order, Quote


class Venue(ABC):
    name: str = "venue"

    @abstractmethod
    def list_instruments(self, categories: list[str] | None = None) -> list[Instrument]:
        """Return the tradable universe, optionally filtered by category."""

    @abstractmethod
    def quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        """Fetch current quotes for the given instruments, keyed by symbol."""

    @abstractmethod
    def place_order(self, order: Order) -> Fill | None:
        """Submit an order. Returns the Fill if it executed, else None."""

    @abstractmethod
    def balance(self) -> float:
        """Available cash in USD."""
