"""The daily reflection job: the learning loop, run end to end.

Trading runs every 15 minutes; this runs once a day and does the actual
learning, in order:

  1. Score every prediction whose horizon has elapsed (build the track record).
  2. Recompute bounded reliability weights per (strategy, coin).
  3. Build a scorecard (hit rate + calibration per source/coin, realized P&L).
  4. Have the model write lessons from that scorecard into its journal.
  5. Auto-tune selection aggressiveness within hard bounds (never risk limits).
  6. Discover/graduate/retire coins in the watch universe.

It returns a ``ReflectionSummary`` so the daily email can show, in plain terms,
what the bot learned and every self-change it made.
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import Config
from ..models import Action
from ..storage import Storage
from . import reliability as _reliability
from .autotune import AutoTuneChange, AutoTuner
from .discovery import DiscoverySummary, UniverseDiscovery
from .journal import PredictionJournal
from .memory import ReflectionMemory
from .reliability import SourceStat


@dataclass
class ReflectionSummary:
    day: str
    scored: int = 0
    overall_samples: int = 0
    overall_hit_rate: float = 0.0
    by_source: dict = field(default_factory=dict)     # source -> (samples, hit_rate, calib_err)
    lessons: str = ""
    changes: list[AutoTuneChange] = field(default_factory=list)
    discovery: DiscoverySummary | None = None
    reliability: list[SourceStat] = field(default_factory=list)
    ran: bool = True


def _realized_pnl_by_symbol(storage: Storage) -> dict[str, float]:
    """Replay the full fill log per symbol (average cost) to attribute realized P&L."""
    pnl: dict[str, float] = defaultdict(float)
    state: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # symbol -> [qty, avg_cost]
    for fill in storage.fills_since(0.0):
        qty, avg = state[fill.symbol]
        delta = fill.quantity if fill.action is Action.BUY else -fill.quantity
        new_qty = qty + delta
        if qty != 0 and (qty > 0) != (delta > 0):  # reducing/closing
            closed = min(abs(delta), abs(qty))
            pnl[fill.symbol] += closed * (fill.price - avg) * (1 if qty > 0 else -1)
        if abs(new_qty) < 1e-9:
            state[fill.symbol] = [0.0, 0.0]
        elif qty == 0 or (qty > 0) == (delta > 0):
            state[fill.symbol] = [new_qty, (abs(qty) * avg + fill.quantity * fill.price) / abs(new_qty)]
        else:
            state[fill.symbol] = [new_qty, avg if abs(delta) <= abs(qty) else fill.price]
    return dict(pnl)


class Reflector:
    def __init__(
        self,
        cfg: Config,
        storage: Storage,
        memory: ReflectionMemory,
        venue=None,
        nominate_client=None,
        known_symbols: set[str] | None = None,
    ):
        self._cfg = cfg
        self._storage = storage
        self._memory = memory
        self._venue = venue
        self._nominate_client = nominate_client
        self._known = known_symbols or set()
        self._journal = PredictionJournal(
            storage,
            horizon_hours=cfg.learning.prediction_horizon_hours,
            move_threshold=cfg.learning.move_threshold,
        )

    def run(self, now: float | None = None) -> ReflectionSummary:
        now = now or _dt.datetime.now().timestamp()
        day = _dt.date.today().isoformat()
        lc = self._cfg.learning
        if not lc.enabled:
            return ReflectionSummary(day=day, ran=False)

        # 1. Score what's due.
        scored = self._journal.score_due(now)

        # 2. Reliability weights.
        stats = _reliability.compute(self._storage, lc)

        # 3. Aggregate the scorecard.
        by_source, overall, by_coin = self._aggregate(stats)
        pnl = _realized_pnl_by_symbol(self._storage)
        scorecard = {
            "day": day,
            "overall": {"samples": overall[0], "hit_rate": overall[1], "avg_confidence": overall[2]},
            "by_source": by_source,
            "by_coin": by_coin,
            "realized_pnl_by_coin": pnl,
        }

        # 4. Reflection memory (the model writes its journal) — fail-soft.
        lessons = ""
        if lc.reflect_use_llm:
            lessons = self._memory.generate_lessons(_scorecard_text(scorecard))
        self._memory.save(day, lessons, scorecard)

        # 5. Bounded auto-tuning.
        per_source_hr = {s: (v[0], v[1]) for s, v in by_source.items()}
        changes = AutoTuner(self._storage, self._cfg).run(day, overall, per_source_hr)

        # 6. Universe discovery.
        discovery = self._discover(now)

        return ReflectionSummary(
            day=day,
            scored=scored,
            overall_samples=overall[0],
            overall_hit_rate=overall[1],
            by_source={s: (v[0], v[1], v[3]) for s, v in by_source.items()},
            lessons=lessons,
            changes=changes,
            discovery=discovery,
            reliability=stats,
        )

    # --- helpers ----------------------------------------------------------

    def _aggregate(self, stats: list[SourceStat]):
        """Roll per (source, coin) stats up to per-source, overall, and per-coin."""
        src: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0, 0.0])  # samples, hits, conf*n, calibsum
        coin: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])           # samples, hits
        for s in stats:
            a = src[s.source]
            a[0] += s.samples
            a[1] += s.hit_rate * s.samples
            a[2] += s.avg_confidence * s.samples
            a[3] += s.calibration_error * s.samples
            c = coin[s.symbol]
            c[0] += s.samples
            c[1] += s.hit_rate * s.samples

        by_source = {}
        tot_n = tot_hits = tot_conf = 0.0
        for source, (n, hits, conf, calib) in src.items():
            if n <= 0:
                continue
            by_source[source] = (int(n), hits / n, conf / n, calib / n)  # samples, hit_rate, avg_conf, calib_err
            tot_n += n
            tot_hits += hits
            tot_conf += conf
        overall = (int(tot_n), tot_hits / tot_n if tot_n else 0.0, tot_conf / tot_n if tot_n else 0.0)
        by_coin = {sym: (int(n), hits / n) for sym, (n, hits) in coin.items() if n > 0}
        return by_source, overall, by_coin

    def _discover(self, now: float) -> DiscoverySummary | None:
        lc = self._cfg.learning
        if not lc.discovery_enabled or self._venue is None:
            return None
        discover = getattr(self._venue, "discover_candidates", None)
        if discover is None:
            return None
        try:
            candidates = discover(lc.discovery_min_volume_usd)
        except Exception:
            candidates = []
        ud = UniverseDiscovery(self._storage, lc, self._known, client=self._nominate_client)
        return ud.run(candidates, now=now)


def _scorecard_text(scorecard: dict) -> str:
    """Render the scorecard as compact text for the reflection prompt."""
    lines = [f"Date: {scorecard['day']}"]
    ov = scorecard["overall"]
    lines.append(
        f"Overall: {ov['hit_rate']:.0%} hit rate over {ov['samples']} scored predictions, "
        f"avg confidence {ov['avg_confidence']:.0%}."
    )
    lines.append("By strategy:")
    for source, v in scorecard["by_source"].items():
        n, hr, conf, calib = v
        tag = "overconfident" if calib > 0.05 else ("underconfident" if calib < -0.05 else "calibrated")
        lines.append(f"  - {source}: {hr:.0%} hit rate ({n} preds), avg conf {conf:.0%} [{tag}]")
    lines.append("By coin (hit rate, samples):")
    for sym, (n, hr) in sorted(scorecard["by_coin"].items(), key=lambda kv: kv[1][1]):
        pnl = scorecard["realized_pnl_by_coin"].get(sym)
        pnl_s = f", realized ${pnl:,.2f}" if pnl is not None else ""
        lines.append(f"  - {sym}: {hr:.0%} ({n}){pnl_s}")
    return "\n".join(lines)
