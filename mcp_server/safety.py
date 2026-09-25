"""Write protection.

This module exists so that "can this thing change my team?" is answered in
exactly one place, and the answer is no unless someone deliberately said so.

Three independent conditions must all hold before any MFL import runs:

1. ``MFL_ENABLE_WRITES=1`` - the hard opt-in.
2. ``MFL_DRY_RUN=0`` - the soft override, which must be actively turned off.
3. ``MFL_USERNAME`` + ``MFL_PASSWORD`` present, because MFL's APIKEY cannot
   authenticate import requests.

Plus two runtime guards: a minimum projected-point improvement before a
lineup change is submitted, and an explicit ``confirmed=True`` on any tool that
mutates state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .errors import WritesDisabledError

log = logging.getLogger(__name__)

# Refuse to churn a lineup for less than this many projected points.
MIN_LINEUP_GAIN = 0.5


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def check_write(settings: Settings, *, confirmed: bool = False) -> GuardResult:
    """Evaluate every write precondition."""
    if not settings.enable_writes:
        return GuardResult(False, "MFL_ENABLE_WRITES is not set to 1")
    if settings.dry_run:
        return GuardResult(False, "MFL_DRY_RUN is set; set it to 0 to allow writes")
    if not settings.has_cookie_auth:
        return GuardResult(
            False,
            "MFL_USERNAME/MFL_PASSWORD are required because the APIKEY cannot "
            "be used for import requests",
        )
    if not confirmed:
        return GuardResult(False, "confirmed=True was not supplied")
    return GuardResult(True, "writes enabled and confirmed")


def require_write(settings: Settings, *, confirmed: bool = False) -> None:
    result = check_write(settings, confirmed=confirmed)
    if not result.allowed:
        raise WritesDisabledError(f"Write refused: {result.reason}")


def should_submit_lineup(
    settings: Settings,
    *,
    current_ids: list[str],
    proposed_ids: list[str],
    current_total: float,
    proposed_total: float,
) -> tuple[bool, str]:
    """Decide whether a lineup change is worth submitting.

    Skips the write when the lineup is unchanged, or when the projected gain
    is below :data:`MIN_LINEUP_GAIN`.
    """
    if sorted(current_ids) == sorted(proposed_ids):
        return False, "lineup already matches the recommendation"
    gain = proposed_total - current_total
    if gain < MIN_LINEUP_GAIN:
        return False, f"projected gain of {gain:.2f} is below the {MIN_LINEUP_GAIN} threshold"
    return True, f"projected gain of {gain:.2f} points"


def log_writes_enabled(settings: Settings) -> None:
    if settings.writes_allowed:
        log.warning(
            "MFL write access is ENABLED for %s (dry_run=%s). "
            "This tool can change your real roster.",
            settings.league_id,
            settings.dry_run,
        )
    else:
        log.info(
            "MFL writes are disabled for %s; all actions will be reported only.",
            settings.league_id,
        )
