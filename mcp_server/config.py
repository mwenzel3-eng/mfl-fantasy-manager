"""Configuration loaded from the environment (and an optional .env file).

Credential handling notes, based on the MFL Developer's Program docs:

* Two auth mechanisms exist. A ``MFL_USER_ID`` cookie obtained from the
  ``login`` endpoint, or the ``APIKEY`` query parameter.
* ``APIKEY`` only works for **export** requests, and only at owner level. It
  cannot be used for commissioner-level calls, and it cannot be used at all for
  **import** (write) requests. So writes always require a live login.
* The cookie value is a Base64 string and may contain ``+``, ``/`` and ``=``.
  We let httpx handle the encoding rather than URL-escaping by hand.
* Requests without a league parameter must go to ``api.myfantasyleague.com``.

Secrets are only ever read from the environment. Nothing in this module logs or
echoes a password, API key or cookie.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .errors import WritesDisabledError

try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False

REPO_ROOT = Path(__file__).resolve().parent.parent
API_HOST = "api.myfantasyleague.com"

# MFL grants registered clients roughly 2.5x the request limit, but only if the
# User-Agent registered with them matches what the client actually sends. That
# makes this string effectively frozen: putting a version in it would silently
# drop the registration on every release, with no error to notice. So the
# version is reported in logs and in the status payload instead, and this
# string stays stable for the life of the project.
CLIENT_NAME = "mfl-fantasy-manager"
CLIENT_URL = "https://github.com/mwenzel3-eng/mfl-fantasy-manager"
DEFAULT_USER_AGENT = f"{CLIENT_NAME} (+{CLIENT_URL})"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or contradictory."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    league_id: str
    year: str
    username: str | None = None
    # Secret fields are excluded from repr so that Settings can be logged or
    # printed in a traceback without leaking credentials.
    password: str | None = field(default=None, repr=False)
    apikey: str | None = field(default=None, repr=False)
    host: str | None = None
    dry_run: bool = True
    enable_writes: bool = False
    request_delay: float = 1.0
    timeout: float = 30.0
    cache_ttl: float = 21 * 3600.0
    cache_dir: Path | None = None
    user_agent: str = DEFAULT_USER_AGENT
    franchise_id: str | None = None
    sms_provider: str = "log"
    twilio_sid: str | None = None
    twilio_token: str | None = field(default=None, repr=False)
    twilio_from: str | None = None
    notify_to: str | None = None
    league_timezone: str = "America/Phoenix"
    extra: dict[str, str] = field(default_factory=dict, repr=False)

    # -- capability checks -------------------------------------------------

    @property
    def has_cookie_auth(self) -> bool:
        """True when we hold the credentials needed to obtain a login cookie."""
        return bool(self.username and self.password)

    @property
    def has_apikey_auth(self) -> bool:
        return bool(self.apikey)

    @property
    def can_read(self) -> bool:
        return self.has_cookie_auth or self.has_apikey_auth

    @property
    def writes_allowed(self) -> bool:
        """Writes need BOTH an explicit opt-in and writes actually enabled.

        ``MFL_ENABLE_WRITES`` is the hard switch; ``MFL_DRY_RUN`` is the
        softer one. Default posture is no writes at all.
        """
        return self.enable_writes and not self.dry_run and self.has_cookie_auth

    def require_read(self) -> None:
        if not self.can_read:
            raise ConfigError(
                "No MFL credentials configured. Set MFL_APIKEY (read-only, export "
                "only) or MFL_USERNAME + MFL_PASSWORD (required for writes)."
            )

    def require_writes(self) -> None:
        self.require_read()
        if not self.writes_allowed:
            reasons = []
            if not self.enable_writes:
                reasons.append("MFL_ENABLE_WRITES is not set to 1")
            if self.dry_run:
                reasons.append("MFL_DRY_RUN is not set to 0")
            if not self.has_cookie_auth:
                reasons.append(
                    "MFL_USERNAME/MFL_PASSWORD are required because the APIKEY "
                    "cannot be used for import requests"
                )
            raise WritesDisabledError(
                "Refusing to run a write operation: " + "; ".join(reasons)
            )


def load_settings(env_file: Path | str | None = None) -> Settings:
    """Build :class:`Settings` from the environment.

    ``env_file`` defaults to ``.env`` in the repo root if it exists.
    """
    path = Path(env_file) if env_file else REPO_ROOT / ".env"
    if path.is_file():
        load_dotenv(path)

    league_id = (os.environ.get("MFL_LEAGUE_ID") or "").strip()
    if not league_id:
        raise ConfigError(
            "MFL_LEAGUE_ID is required. Find it in your league URL: "
            "myfantasyleague.com/2026/index?L=12345"
        )
    # Franchise ids are zero-padded 4-digit strings; normalise defensively.
    league_id = league_id.zfill(5) if len(league_id) <= 5 else league_id

    year = (os.environ.get("MFL_YEAR") or "").strip()
    if not year:
        year = _default_year()

    cache_dir_raw = os.environ.get("MFL_CACHE_DIR") or ""
    cache_dir = Path(cache_dir_raw).expanduser() if cache_dir_raw else None

    known = {
        "MFL_LEAGUE_ID", "MFL_YEAR", "MFL_USERNAME", "MFL_PASSWORD", "MFL_APIKEY",
        "MFL_HOST", "MFL_DRY_RUN", "MFL_ENABLE_WRITES", "MFL_REQUEST_DELAY",
        "MFL_TIMEOUT", "MFL_CACHE_TTL", "MFL_CACHE_DIR", "MFL_USER_AGENT",
        "MFL_FRANCHISE_ID", "SMS_PROVIDER", "TWILIO_SID", "TWILIO_TOKEN",
        "TWILIO_FROM", "SMS_TO", "MFL_TIMEZONE",
    }

    return Settings(
        league_id=league_id,
        year=year,
        username=(os.environ.get("MFL_USERNAME") or "").strip() or None,
        password=os.environ.get("MFL_PASSWORD") or None,
        apikey=(os.environ.get("MFL_APIKEY") or "").strip() or None,
        host=(os.environ.get("MFL_HOST") or "").strip().lower() or None,
        dry_run=_env_bool("MFL_DRY_RUN", True),
        enable_writes=_env_bool("MFL_ENABLE_WRITES", False),
        request_delay=_env_float("MFL_REQUEST_DELAY", 1.0),
        timeout=_env_float("MFL_TIMEOUT", 30.0),
        cache_ttl=_env_float("MFL_CACHE_TTL", 21 * 3600.0),
        cache_dir=cache_dir,
        user_agent=os.environ.get("MFL_USER_AGENT") or DEFAULT_USER_AGENT,
        franchise_id=(os.environ.get("MFL_FRANCHISE_ID") or "").strip() or None,
        sms_provider=(os.environ.get("SMS_PROVIDER") or "log").strip().lower(),
        twilio_sid=os.environ.get("TWILIO_SID") or None,
        twilio_token=os.environ.get("TWILIO_TOKEN") or None,
        twilio_from=os.environ.get("TWILIO_FROM") or None,
        notify_to=os.environ.get("SMS_TO") or None,
        league_timezone=os.environ.get("MFL_TIMEZONE") or "America/Phoenix",
        extra={k: v for k, v in os.environ.items() if k not in known},
    )


def _default_year() -> str:
    """NFL seasons straddle calendar years, so guess the current season."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # The season rolls over in late July/August.
    return str(now.year if now.month >= 7 else now.year - 1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
