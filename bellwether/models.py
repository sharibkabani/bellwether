"""Core domain models for trading crypto on Kraken.

Kraken trades real crypto assets at dollar prices, in fractional quantities
(you buy 0.012 BTC, not whole shares). A position is a signed quantity: positive
is long; Kraken spot is long-only, so shorts only appear if margin trading is
explicitly enabled. P&L is the plain ``quantity × (price − cost)``. Money is
dollars (floats) and quantities are floats throughout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    """The directional view a signal/idea expresses."""

    LONG = "long"
    SHORT = "short"

    @property
    def action(self) -> "Action":
        """The order action that opens this direction."""
        return Action.BUY if self is Direction.LONG else Action.SELL


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "MKT"
    LIMIT = "LMT"


@dataclass
class Instrument:
    """A tradable asset. ``pair`` is the venue's market code (e.g. Kraken's
    ``XBTUSD`` for BTC/USD); ``symbol`` is the friendly ticker (``BTC``).
    ``category`` is a coarse theme used for filtering and reporting."""

    symbol: str
    name: str = ""
    category: str = ""
    pair: str = ""


@dataclass
class Quote:
    """A point-in-time quote in dollars."""

    symbol: str
    last: float
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    prev_close: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    def fill_price(self, action: Action) -> float:
        """The price you'd realistically transact at for an aggressive order."""
        if action is Action.BUY:
            return self.ask if self.ask > 0 else self.last
        return self.bid if self.bid > 0 else self.last


@dataclass
class Position:
    """A holding: ``quantity`` signed shares at ``avg_cost`` dollars/share.

    Positive quantity is long, negative is short.
    """

    symbol: str
    quantity: float
    avg_cost: float

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def direction(self) -> Direction:
        return Direction.LONG if self.quantity > 0 else Direction.SHORT

    def cost_basis(self) -> float:
        """Signed cost basis in dollars."""
        return self.quantity * self.avg_cost

    def market_value(self, quote: Quote) -> float:
        """Signed market value (a short is a negative value / liability)."""
        return self.quantity * quote.last

    def unrealized_pnl(self, quote: Quote) -> float:
        # quantity is signed, so this is correct for both long and short.
        return self.quantity * (quote.last - self.avg_cost)

    def unrealized_pnl_pct(self, quote: Quote) -> float:
        basis = abs(self.cost_basis())
        return self.unrealized_pnl(quote) / basis if basis else 0.0


@dataclass
class Order:
    """An intent to trade, before it reaches a venue. ``quantity`` is always a
    positive share count; ``action`` carries the direction."""

    symbol: str
    action: Action
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    pair: str = ""
    rationale: str = ""


@dataclass
class Fill:
    symbol: str
    action: Action
    quantity: float
    price: float
    ts: float = field(default_factory=time.time)
    commission: float = 0.0
    rationale: str = ""

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass
class Signal:
    """One strategy's directional opinion on a symbol.

    ``expected_return`` is the predicted price move as a signed fraction over the
    strategy's horizon (e.g. +0.04 = +4%, −0.03 = −3%). ``confidence`` in [0,1]
    scales trust; ``weight`` lets the engine blend sources.
    """

    source: str
    symbol: str
    expected_return: float
    confidence: float
    rationale: str = ""
    weight: float = 1.0


@dataclass
class TradeIdea:
    """A candidate trade from the engine, pre-risk-check."""

    instrument: Instrument
    quote: Quote
    direction: Direction
    expected_return: float  # magnitude, always positive
    confidence: float
    rationale: str

    @property
    def entry_price(self) -> float:
        return self.quote.fill_price(self.direction.action)
