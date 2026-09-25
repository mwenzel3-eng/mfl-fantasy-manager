"""SMS/notification delivery.

Pluggable so the scheduling jobs do not depend on any particular provider.
The default provider is ``log``, which writes the message to the logger and to
stdout; that means the weekly jobs are useful on day one without signing up for
anything.

Twilio support is included because it is the cheapest reliable option and has a
simple REST API, but no Twilio code runs unless ``SMS_PROVIDER=twilio`` and
credentials are present.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

# SMS is a low-bandwidth channel. Keep messages short enough to avoid
# multi-segment billing surprises.
MAX_SMS_CHARS = 320


class Notifier(Protocol):
    async def send(self, message: str) -> str: ...

    @property
    def available(self) -> bool: ...


@dataclass
class LogNotifier:
    """Default provider: log the message instead of sending it."""

    async def send(self, message: str) -> str:
        text = truncate(message)
        log.info("SMS [log provider] %s", text)
        return f"logged: {text}"

    @property
    def available(self) -> bool:
        return True


@dataclass
class NullNotifier:
    """Drops everything. Useful in tests and for quiet dry runs."""

    async def send(self, message: str) -> str:
        log.debug("SMS suppressed: %s", truncate(message))
        return "suppressed"

    @property
    def available(self) -> bool:
        return True


@dataclass
class TwilioNotifier:
    sid: str
    token: str
    from_number: str
    to_number: str
    timeout: float = 20.0

    async def send(self, message: str) -> str:
        text = truncate(message)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        auth = (self.sid, self.token)
        data = {
            "From": self.from_number,
            "To": self.to_number,
            "Body": text,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, data=data, auth=auth)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Twilio rejected the message (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        log.info("SMS sent via Twilio to %s", mask(self.to_number))
        return f"sent:{self.sid}"

    @property
    def available(self) -> bool:
        return bool(self.sid and self.token and self.from_number and self.to_number)


def build_notifier(settings) -> Notifier:
    """Pick a notifier from configuration, degrading to log on misconfiguration."""
    provider = (settings.sms_provider or "log").lower()
    if provider in {"none", "off", "null"}:
        return NullNotifier()
    if provider == "twilio":
        if not (settings.twilio_sid and settings.twilio_token
                and settings.twilio_from and settings.notify_to):
            log.warning(
                "SMS_PROVIDER=twilio but Twilio credentials are incomplete; "
                "falling back to the log provider."
            )
            return LogNotifier()
        return TwilioNotifier(
            sid=settings.twilio_sid,
            token=settings.twilio_token,
            from_number=settings.twilio_from,
            to_number=settings.notify_to,
        )
    if provider not in {"log", ""}:
        log.warning("Unknown SMS_PROVIDER %r; using the log provider.", provider)
    return LogNotifier()


def truncate(message: str, limit: int = MAX_SMS_CHARS) -> str:
    text = " ".join(message.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def mask(number: str) -> str:
    if len(number) <= 4:
        return "****"
    return "*" * (len(number) - 4) + number[-4:]
