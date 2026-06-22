"""Email notifier — sends the HTML digest over SMTP.

Free and dependency-light (stdlib ``smtplib``). For Gmail, use an App Password
(set ``SMTP_PASSWORD``); ``email_from`` is your address and ``email_to`` is
where the digest lands.
"""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..report import ReportData, render_html, render_text
from .base import Notifier


class EmailNotifier(Notifier):
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        to_addr: str,
        from_addr: str,
    ):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._to = to_addr
        self._from = from_addr or username

    def send(self, report: ReportData) -> None:
        msg = MIMEMultipart("alternative")
        sign = "+" if report.day_pnl >= 0 else "-"
        msg["Subject"] = (
            f"Bellwether {report.date}: {sign}${abs(report.day_pnl):,.2f} "
            f"({report.day_pnl_pct:+.1f}%)"
        )
        msg["From"] = self._from
        msg["To"] = self._to
        msg.attach(MIMEText(render_text(report), "plain"))
        msg.attach(MIMEText(render_html(report), "html"))

        with smtplib.SMTP(self._host, self._port) as server:
            server.starttls()
            server.login(self._username, self._password)
            server.sendmail(self._from, [self._to], msg.as_string())
