"""The trader: orchestrates one trading cycle and the long-running loop.

A cycle is: refresh instruments + quotes → close any position that hit
stop-loss or take-profit → generate directional signals → risk-check and size
new entries → execute → snapshot equity. The loop runs a cycle every
``poll_interval_sec`` and fires the daily report once per day at the configured
hour. This is the always-on engine the user asked for.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field

from .config import Config
from .executor import Executor
from .models import Fill, Quote, TradeIdea
from .portfolio import Portfolio
from .risk import RiskManager
from .signals.engine import SignalEngine
from .storage import Storage
from .venues.base import Venue


@dataclass
class CycleReport:
    ts: float
    equity: float
    cash: float
    open_positions: int
    entries: list[Fill] = field(default_factory=list)
    exits: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0
    top_ideas: list[TradeIdea] = field(default_factory=list)
    halted: bool = False
    entries_skipped: bool = False  # live reconcile failed → entries withheld this cycle


class Trader:
    def __init__(
        self,
        cfg: Config,
        venue: Venue,
        portfolio: Portfolio,
        risk: RiskManager,
        engine: SignalEngine,
        storage: Storage,
    ):
        self._cfg = cfg
        self._venue = venue
        self._portfolio = portfolio
        self._risk = risk
        self._engine = engine
        self._storage = storage
        self._executor = Executor(venue, portfolio, risk)
        # The prediction journal: log every signal so it can be scored later.
        self._journal = None
        if cfg.learning.enabled:
            from .learning.journal import PredictionJournal

            self._journal = PredictionJournal(
                storage,
                horizon_hours=cfg.learning.prediction_horizon_hours,
                move_threshold=cfg.learning.move_threshold,
            )

    def _market_snapshot(self) -> tuple[list, dict[str, Quote]]:
        instruments = self._venue.list_instruments(self._cfg.strategy.categories or None)
        quotes = self._venue.quotes(instruments)
        return instruments, quotes

    def run_cycle(self, run_entries: bool = True) -> CycleReport:
        """Run one cycle. Exits/trailing are always checked; new-entry hunting
        (and the LLM call + prediction journaling) only happens when
        ``run_entries`` is true, so the loop can manage open risk frequently
        while sourcing new ideas on a slower, cheaper cadence."""
        instruments, quotes = self._market_snapshot()

        # 0. Live only: reconcile against the real wallet so sizing/risk use the
        # broker's actual cash and positions, not a local notional. If the
        # reconcile fails (network/API), withhold NEW entries this cycle rather
        # than risk sizing against stale state — exits are still allowed.
        entries_skipped = False
        if getattr(self._venue, "reconciles", False):
            snapshot = self._venue.account_snapshot()
            if snapshot is None:
                entries_skipped = True
            else:
                self._portfolio.reconcile(snapshot[0], snapshot[1], quotes)

        # 1. Exits first — cut losers / bank winners before adding risk.
        exit_orders = self._risk.check_exits(self._portfolio, quotes)
        exit_result = self._executor.execute(exit_orders)

        from .executor import ExecutionResult

        ideas: list[TradeIdea] = []
        if not run_entries or entries_skipped:
            entry_result = ExecutionResult()
        else:
            # 2. Generate and rank directional trade ideas.
            ideas = self._engine.generate(instruments, quotes)

            # 2b. Journal every raw signal (with the price now) so the learning
            # loop can score these predictions against reality at the horizon.
            if self._journal is not None and self._engine.last_signals:
                prices = {sym: q.last for sym, q in quotes.items()}
                self._journal.record(self._engine.last_signals, prices)

            # 3. Risk-check, size, and execute new entries.
            entry_orders = self._risk.approve_entries(ideas, self._portfolio, quotes)
            entry_result = self._executor.execute(entry_orders, record_spend=True)

        # 4. Snapshot equity for the report and drawdown tracking.
        equity = self._portfolio.equity(quotes)
        peak = self._portfolio.update_peak_equity(equity)
        self._storage.record_equity(equity)
        halted = self._risk.kill_switch_triggered(equity, peak)

        return CycleReport(
            ts=time.time(),
            equity=equity,
            cash=self._portfolio.cash,
            open_positions=len(self._portfolio.positions()),
            entries=entry_result.fills,
            exits=exit_result.fills,
            realized_pnl=exit_result.realized_pnl,
            top_ideas=ideas[:5],
            halted=halted,
            entries_skipped=entries_skipped,
        )

    def run_forever(self, on_cycle=None, on_daily_report=None) -> None:
        last_report_day = self._storage.get_meta("last_report_day", "")
        last_entry_ts = 0.0
        entry_interval = max(self._cfg.entry_interval_sec, self._cfg.poll_interval_sec)
        while True:
            now_ts = time.time()
            run_entries = (now_ts - last_entry_ts) >= entry_interval
            report = self.run_cycle(run_entries=run_entries)
            if run_entries:
                last_entry_ts = now_ts
            if on_cycle:
                on_cycle(report)

            now = _dt.datetime.now()
            today = now.date().isoformat()
            if now.hour >= self._cfg.daily_report_hour and today != last_report_day:
                if on_daily_report:
                    on_daily_report()
                last_report_day = today
                self._storage.set_meta("last_report_day", today)

            time.sleep(self._cfg.poll_interval_sec)
