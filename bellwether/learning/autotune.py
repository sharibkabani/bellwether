"""Bounded config auto-tuning — the dangerous part, fenced off.

Self-learning bots blow up when they edit their own risk settings: one that
loosens its stop-loss to avoid "mistakes", or levers up after a lucky streak,
eventually hands everything back. So the line is hard and structural:

  IMMUTABLE (human-owned, the bot can never touch these):
    max_position_per_trade, max_daily_spend, max_drawdown_pct, stop_loss_pct,
    take_profit_pct, max_total_exposure, max_open_positions.

  ADAPTABLE (selection aggressiveness only, within hard bounds):
    • min_confidence  — nudged within [floor, ceiling] by a small step;
    • strategy weights — nudged within [weight_min, weight_max] by a small step.

Every change is small, evidence-gated (require enough scored samples), clamped
to its band, and written to the changelog so it shows up in the daily email.
The immutable keys are enforced here in code, not just by convention.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..storage import Storage

# Capital-protection limits the bot may never write. Enforced, not advisory.
IMMUTABLE_KEYS = frozenset(
    {
        "max_position_per_trade",
        "max_daily_spend",
        "max_drawdown_pct",
        "stop_loss_pct",
        "take_profit_pct",
        "max_total_exposure",
        "max_open_positions",
        "starting_bankroll",
        "position_pct",
    }
)


@dataclass
class AutoTuneChange:
    field: str
    old_value: float
    new_value: float
    reason: str


def effective_min_confidence(storage: Storage, base: float) -> float:
    ov = storage.get_override("min_confidence")
    return ov if ov is not None else base


def effective_weight(storage: Storage, source: str, base: float) -> float:
    ov = storage.get_override(f"weight:{source}")
    return ov if ov is not None else base


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class AutoTuner:
    def __init__(self, storage: Storage, cfg: Config):
        self._storage = storage
        self._cfg = cfg

    def _set_override(self, key: str, value: float) -> None:
        if key in IMMUTABLE_KEYS:
            raise ValueError(f"refusing to auto-tune immutable capital-protection key: {key}")
        self._storage.set_override(key, value)

    def run(
        self,
        day: str,
        overall: tuple[int, float, float],          # (samples, hit_rate, avg_confidence)
        per_source: dict[str, tuple[int, float]],   # source -> (samples, hit_rate)
    ) -> list[AutoTuneChange]:
        lc = self._cfg.learning
        if not lc.autotune_enabled:
            return []
        changes: list[AutoTuneChange] = []
        changes += self._tune_min_confidence(day, overall)
        changes += self._tune_weights(day, per_source)
        return changes

    # --- min_confidence ---------------------------------------------------

    def _tune_min_confidence(self, day: str, overall: tuple[int, float, float]) -> list[AutoTuneChange]:
        lc = self._cfg.learning
        samples, hit_rate, _avg_conf = overall
        if samples < lc.min_samples_to_adapt:
            return []

        current = effective_min_confidence(self._storage, self._cfg.risk.min_confidence)
        target = current
        reason = ""
        if hit_rate < 0.45:
            target = current + lc.min_confidence_step  # too many wrong entries → be pickier
            reason = f"overall hit rate {hit_rate:.0%} over {samples} preds — raising selectivity"
        elif hit_rate > 0.60:
            target = current - lc.min_confidence_step  # consistently right → allow more entries
            reason = f"overall hit rate {hit_rate:.0%} over {samples} preds — easing selectivity"

        target = _clamp(target, lc.min_confidence_floor, lc.min_confidence_ceiling)
        if abs(target - current) < 1e-9:
            return []

        self._set_override("min_confidence", target)
        self._cfg.risk.min_confidence = target  # apply live to the running risk manager
        self._storage.record_change(day, "min_confidence", f"{current:.2f}", f"{target:.2f}", reason)
        return [AutoTuneChange("min_confidence", current, target, reason)]

    # --- strategy weights -------------------------------------------------

    def _tune_weights(self, day: str, per_source: dict[str, tuple[int, float]]) -> list[AutoTuneChange]:
        lc = self._cfg.learning
        base = {
            "momentum": self._cfg.strategy.momentum_weight,
            "trending": self._cfg.strategy.trending_weight,
        }
        changes: list[AutoTuneChange] = []
        for source, (samples, hit_rate) in per_source.items():
            if samples < lc.min_samples_to_adapt:
                continue
            current = effective_weight(self._storage, source, base.get(source, 1.0))
            target = current
            reason = ""
            if hit_rate > 0.55:
                target = current + lc.weight_step
                reason = f"{source} hit rate {hit_rate:.0%} over {samples} preds — trusting more"
            elif hit_rate < 0.45:
                target = current - lc.weight_step
                reason = f"{source} hit rate {hit_rate:.0%} over {samples} preds — trusting less"

            target = _clamp(target, lc.weight_min, lc.weight_max)
            if abs(target - current) < 1e-9:
                continue

            self._set_override(f"weight:{source}", target)
            self._storage.record_change(
                day, f"weight:{source}", f"{current:.2f}", f"{target:.2f}", reason
            )
            changes.append(AutoTuneChange(f"weight:{source}", current, target, reason))
        return changes


def apply_overrides(cfg: Config, storage: Storage) -> None:
    """Seed the in-memory config from persisted bot overrides at startup, so a
    restart keeps the bot's learned selection settings (within the same bounds)."""
    mc = storage.get_override("min_confidence")
    if mc is not None:
        cfg.risk.min_confidence = _clamp(
            mc, cfg.learning.min_confidence_floor, cfg.learning.min_confidence_ceiling
        )
