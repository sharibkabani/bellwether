"""Reliability weights: learn who to trust, mechanically and bounded.

Each (strategy, coin) earns a trust multiplier from its scored track record.
Consistently-right sources get quietly up-weighted, consistently-wrong ones
down-weighted — but always within a hard band (default 0.5x-1.5x) so a hot or
cold streak can never let one source run away with the book.

Two guards keep this from overfitting to noise:
  • a minimum sample count before a multiplier moves off neutral (1.0);
  • Bayesian-style regularization of the hit rate toward the 0.5 prior, so early
    evidence nudges gently and only a large, consistent sample moves it far.

The multiplier feeds the signal engine's blend; it does not touch risk limits.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..config import LearningConfig
from ..storage import Storage


@dataclass
class SourceStat:
    source: str
    symbol: str
    samples: int
    hit_rate: float
    avg_confidence: float
    calibration_error: float   # |avg_confidence - hit_rate|: >0 with sign = over/under-confident
    multiplier: float


def _multiplier_from_hit_rate(hit_rate: float, lo: float, hi: float) -> float:
    """Map a hit rate to a bounded multiplier: 0.5 -> 1.0 (neutral), 1.0 -> hi, 0.0 -> lo."""
    if hit_rate >= 0.5:
        m = 1.0 + (hit_rate - 0.5) / 0.5 * (hi - 1.0)
    else:
        m = 1.0 - (0.5 - hit_rate) / 0.5 * (1.0 - lo)
    return max(lo, min(hi, m))


def compute(storage: Storage, cfg: LearningConfig) -> list[SourceStat]:
    """Recompute and persist reliability for every (source, symbol) with data."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in storage.scored_predictions():
        grouped[(p["source"], p["symbol"])].append(p)

    stats: list[SourceStat] = []
    for (source, symbol), rows in grouped.items():
        samples = len(rows)
        hits = sum(1 for r in rows if r["correct"])
        avg_conf = sum(r["confidence"] for r in rows) / samples

        # Regularize the hit rate toward the 0.5 prior (slow learning).
        prior = cfg.reliability_prior_strength
        reg_hit_rate = (hits + 0.5 * prior) / (samples + prior)
        raw_hit_rate = hits / samples

        if samples >= cfg.min_samples_to_adapt:
            multiplier = _multiplier_from_hit_rate(reg_hit_rate, cfg.reliability_min, cfg.reliability_max)
        else:
            multiplier = 1.0  # not enough evidence yet — stay neutral

        calibration_error = avg_conf - raw_hit_rate  # +overconfident, -underconfident
        storage.set_reliability(
            source, symbol, samples, raw_hit_rate, calibration_error, multiplier
        )
        stats.append(
            SourceStat(source, symbol, samples, raw_hit_rate, avg_conf, calibration_error, multiplier)
        )
    return stats
