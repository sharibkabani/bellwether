"""Order executor: send approved orders to the venue and book the fills.

The thin seam between decision and reality. It submits each order, applies any
resulting fill to the portfolio, and (for entries) tallies new spend so the risk
manager's daily limit stays accurate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Fill, Order
from .portfolio import Portfolio
from .risk import RiskManager
from .venues.base import Venue


@dataclass
class ExecutionResult:
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0
    spent: float = 0.0


class Executor:
    def __init__(self, venue: Venue, portfolio: Portfolio, risk: RiskManager):
        self._venue = venue
        self._portfolio = portfolio
        self._risk = risk

    def execute(self, orders: list[Order], record_spend: bool = False) -> ExecutionResult:
        result = ExecutionResult()
        for order in orders:
            fill = self._venue.place_order(order)
            if fill is None:
                continue
            realized = self._portfolio.apply_fill(fill)
            result.fills.append(fill)
            result.realized_pnl += realized
            result.spent += fill.notional
        if record_spend and result.spent > 0:
            self._risk.record_entry_spend(result.spent)
        return result
