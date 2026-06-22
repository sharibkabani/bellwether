"""Console notifier — prints the report as a rich terminal panel."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from ..report import ReportData, render_text
from .base import Notifier


class ConsoleNotifier(Notifier):
    def __init__(self) -> None:
        self._console = Console()

    def send(self, report: ReportData) -> None:
        color = "green" if report.day_pnl >= 0 else "red"
        self._console.print(
            Panel(render_text(report), title="📈 Bellwether", border_style=color)
        )
