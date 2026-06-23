"""The prediction journal: write down what we predicted, then grade it.

Every cycle the engine emits per-strategy signals (symbol, expected return,
confidence, rationale). Today those are blended and thrown away — so there's no
record to learn from. The journal logs each raw signal with the price at the
time, then later scores it: once a prediction's horizon has elapsed, compare the
price then to the price now and ask "did the predicted direction pan out, and
was the confidence calibrated?".

Scoring is deliberately simple and robust:
  • ``correct`` = the signed prediction matched the realized direction, where a
    move smaller than ``move_threshold`` counts as flat (neither side wins).
  • we also keep the realized return so calibration error can be computed later.

Predictions are scored against the recorded price history (``prices`` table),
which the momentum strategy writes every cycle. A prediction we can't score
(the coin left the universe, so no price exists at the horizon) is expired after
a grace window so the backlog can't grow without bound.
"""

from __future__ import annotations

from ..models import Signal
from ..storage import Storage


class PredictionJournal:
    def __init__(self, storage: Storage, horizon_hours: float = 24.0, move_threshold: float = 0.01):
        self._storage = storage
        self._horizon_sec = max(60.0, horizon_hours * 3600.0)
        self._move_threshold = move_threshold

    # --- recording --------------------------------------------------------

    def record(self, signals: list[Signal], prices: dict[str, float], ts: float | None = None) -> int:
        """Log each signal with the price at the time. Returns how many were logged."""
        n = 0
        for sig in signals:
            price = prices.get(sig.symbol)
            if not price or price <= 0:
                continue
            self._storage.record_prediction(
                source=sig.source,
                symbol=sig.symbol,
                expected_return=sig.expected_return,
                confidence=sig.confidence,
                rationale=sig.rationale,
                price=price,
                horizon_sec=self._horizon_sec,
                ts=ts,
            )
            n += 1
        return n

    # --- scoring ----------------------------------------------------------

    def score_due(self, now: float) -> int:
        """Score every prediction whose horizon has elapsed. Returns count scored.

        A prediction with no price at/after its horizon is left for now and
        expired only once it's more than twice its horizon old (the coin likely
        dropped out of the watched universe)."""
        scored = 0
        for pred in self._storage.due_predictions(now):
            target_ts = pred["ts"] + pred["horizon_sec"]
            future_price = self._storage.price_at_or_after(pred["symbol"], target_ts)
            entry = pred["price"]

            if future_price is None or entry <= 0:
                # Expire stale, unscoreable predictions so the backlog stays bounded.
                if now - pred["ts"] > 2 * pred["horizon_sec"]:
                    self._storage.mark_prediction_scored(pred["id"], None, None, ts=now)
                continue

            actual_return = future_price / entry - 1.0
            correct = self._grade(pred["expected_return"], actual_return)
            self._storage.mark_prediction_scored(pred["id"], actual_return, correct, ts=now)
            scored += 1
        return scored

    def _grade(self, predicted: float, actual: float) -> int:
        """1 if the predicted direction matched the realized one (flat = miss)."""
        if abs(actual) < self._move_threshold:
            # Market basically didn't move; only reward a prediction that also
            # expected ~flat. A confident directional call on a flat tape is a miss.
            return 1 if abs(predicted) < self._move_threshold else 0
        return 1 if (predicted >= 0) == (actual >= 0) else 0
