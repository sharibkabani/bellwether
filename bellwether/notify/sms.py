"""SMS notifier — sends a short digest via Twilio's REST API.

Uses ``requests`` against Twilio's API directly (no extra dependency). Twilio
SMS costs roughly a cent per message plus ~$1/mo for a number, and needs a
Twilio account: set ``TWILIO_ACCOUNT_SID`` and ``TWILIO_AUTH_TOKEN``, plus
``sms_from`` (your Twilio number) and ``sms_to`` (your phone).
"""

from __future__ import annotations

import requests

from ..report import ReportData, render_sms
from .base import Notifier


class SMSNotifier(Notifier):
    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_number: str):
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._to = to_number

    def send(self, report: ReportData) -> None:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json"
        resp = requests.post(
            url,
            data={"From": self._from, "To": self._to, "Body": render_sms(report)},
            auth=(self._sid, self._token),
            timeout=15,
        )
        resp.raise_for_status()
