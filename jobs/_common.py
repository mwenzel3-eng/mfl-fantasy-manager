"""Shared plumbing for the scheduled jobs.

Every job follows the same shape: load a snapshot, do one thing, notify, and
return a non-zero exit code on failure so GitHub Actions flags the run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from mcp_server.config import Settings, get_settings
from mcp_server.context import load_snapshot
from mcp_server.mfl_api import MFLClient
from mcp_server.notify import build_notifier

logging.basicConfig(
    level=os.environ.get("MFL_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("jobs")


class JobResult:
    """Outcome of a job, including whether anything actually changed."""

    def __init__(self, name: str, *, force: bool = False) -> None:
        self.name = name
        self.lines: list[str] = []
        self.changed = False
        self.error: str | None = None
        # Set via ``--force``: bypass the projected-gain threshold. It never
        # bypasses the write switches, so this is still not enough to write.
        self.force = force

    def say(self, line: str) -> None:
        self.lines.append(line)
        log.info("%s", line)

    def as_text(self) -> str:
        header = f"[{self.name}]"
        if self.error:
            header += f" ERROR: {self.error}"
        return "\n".join([header, *self.lines])

    def exit_code(self) -> int:
        return 1 if self.error else 0


async def run_job(
    name: str,
    body,
    *,
    settings: Settings | None = None,
    week: int | None = None,
    force: bool = False,
) -> JobResult:
    """Execute ``body(client, snap, result)`` with setup and teardown handled."""
    settings = settings or get_settings()
    result = JobResult(name, force=force)
    client = MFLClient(settings)
    notifier = build_notifier(settings)
    try:
        snap = await load_snapshot(client, week=week)
        result.say(f"League: {snap.league.name or settings.league_id} (week {snap.week})")
        if settings.writes_allowed:
            result.say("Write access: ENABLED")
        else:
            result.say("Write access: disabled (report only)")
        await body(client, snap, result)
    except Exception as exc:  # noqa: BLE001 - job boundary
        result.error = str(exc)
        log.exception("Job %s failed", name)
    finally:
        await client.aclose()

    text = result.as_text()
    print(text)
    try:
        await notifier.send(text)
    except Exception as exc:  # noqa: BLE001 - notification must not mask the job
        log.warning("Notification failed: %s", exc)
    return result


def main_wrapper(coro_factory) -> None:
    """Entry point helper: run a coroutine and exit with its status."""
    sys.exit(asyncio.run(coro_factory()).exit_code())
