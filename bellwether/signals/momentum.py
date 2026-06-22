"""Price-momentum strategy.

Keeps a rolling window of each symbol's price and estimates a signed expected
return by extrapolating the recent trend: if a stock has been rising, the
momentum thesis is that it continues. Fully deterministic, no network — useful
on its own and as a baseline against the AI signal. Price history persists to
SQLite so the signal survives restarts.
"""

from __future__ import annotations

from collections import defaultdict, deque

from ..models import Instrument, Quote, Signal
from .base import Strategy


class MomentumStrategy(Strategy):
    name = "momentum"

    def __init__(self, window: int = 6, lookahead: int = 3, storage=None):
        self._window = window
        self._lookahead = lookahead
        self._storage = storage
        self._history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))

    def _update_history(self, symbol: str, price: float) -> list[float]:
        if self._storage is not None:
            self._storage.record_price(symbol, price)
            return self._storage.recent_prices(symbol, self._window)
        hist = self._history[symbol]
        hist.append(price)
        return list(hist)

    def evaluate(
        self, instruments: list[Instrument], quotes: dict[str, Quote]
    ) -> list[Signal]:
        signals = []
        for inst in instruments:
            quote = quotes.get(inst.symbol)
            if quote is None or quote.last <= 0:
                continue
            prices = self._update_history(inst.symbol, quote.last)
            if len(prices) < 3:
                continue

            # Average per-step return over the window, projected forward.
            steps = len(prices) - 1
            per_step = (prices[-1] / prices[0] - 1) / steps
            expected_return = per_step * self._lookahead

            strength = min(1.0, abs(expected_return) / 0.05)  # 5% move => full
            liquidity = min(1.0, quote.volume / 2_000_000)
            confidence = 0.4 + 0.4 * strength + 0.2 * liquidity

            signals.append(
                Signal(
                    source=self.name,
                    symbol=inst.symbol,
                    expected_return=expected_return,
                    confidence=round(confidence, 3),
                    rationale=f"{steps}-step trend {expected_return:+.1%}",
                )
            )
        return signals
