"""Notifier interface — one method, send the daily report through some channel."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..report import ReportData


class Notifier(ABC):
    @abstractmethod
    def send(self, report: ReportData) -> None:
        ...
