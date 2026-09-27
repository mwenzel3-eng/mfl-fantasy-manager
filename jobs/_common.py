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

from mcp_server import __version__
from mcp_server.config import REPO_ROOT, Settings, get_settings
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


def _git_commit() -> str | None:
    """Short SHA of the checked-out commit, or None outside a git checkout.

    Lets a job log prove which code actually ran, which is the difference
    between "the fix is broken" and "CI ran the previous commit".
    """
    try:
        head = REPO_ROOT / ".git" / "HEAD"
        if not head.is_file():
            return None
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            packed = REPO_ROOT / ".git" / ref[5:]
            if packed.is_file():
                return packed.read_text().strip()[:7]
            return ref[5:].rsplit("/", 1)[-1][:7]
        return ref[:7]
    except OSError:
        return None


def _write_step_summary(result: JobResult) -> None:
    """Publish the report to the Actions run page.

    Without this the only copy is at the very bottom of a log that also
    contains pip output, which is easy to miss entirely: a run can look like
    it produced nothing when it did. No-op outside Actions.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    commit = _git_commit() or "unknown"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"## {result.name}\n\n")
            handle.write(
                f"`{__version__}` · commit `{commit}` · "
                f"{'FAILED' if result.error else 'OK'}"
                + (" · writes applied\n\n" if result.changed else "\n\n")
            )
            if result.error:
                handle.write(f"**Error:** {result.error}\n\n")
            body = "\n".join(result.lines) or "_(no output)_"
            handle.write(f"```text\n{body}\n```\n")
    except OSError as exc:
        log.warning("Could not write the step summary: %s", exc)


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
    # Stamp the code identity into the job log. Without this, a stale CI run and
    # a fresh local run are indistinguishable when the error text is the same,
    # which is exactly the confusion this line exists to prevent. Logged rather
    # than said(), so it stays out of the notification body the user reads.
    log.info(
        "mfl-fantasy-manager %s (commit %s, dry_run=%s, writes=%s)",
        __version__,
        _git_commit() or "unknown",
        settings.dry_run,
        "on" if settings.writes_allowed else "off",
    )
    try:
        snap = await load_snapshot(client, week=week)
        result.say(f"League: {snap.league.name or settings.league_id} (week {snap.week})")
        # Always report the roster size. Every job reasons about your own team,
        # and a run that analysed an empty roster reports "nothing to do" just
        # as confidently as one that worked - which is how an empty roster can
        # hide behind a green tick for weeks.
        result.say(f"Roster: {len(snap.roster)} players")
        if not snap.roster:
            result.error = (
                "Your franchise returned no players, so there is nothing to "
                "analyse. Check that MFL_LEAGUE_ID points at the league you mean "
                "and that the account can see your roster."
            )
        elif settings.writes_allowed:
            result.say("Write access: ENABLED")
        else:
            result.say("Write access: disabled (report only)")
        # With no roster there is nothing for the body to reason about, and
        # running it anyway would overwrite the reason above with a generic
        # "nothing to do".
        if not result.error:
            await body(client, snap, result)
    except Exception as exc:  # noqa: BLE001 - job boundary
        result.error = str(exc)
        log.exception("Job %s failed", name)
    finally:
        await client.aclose()

    text = result.as_text()
    print(text)
    _write_step_summary(result)
    try:
        await notifier.send(text)
    except Exception as exc:  # noqa: BLE001 - notification must not mask the job
        log.warning("Notification failed: %s", exc)
    return result


def main_wrapper(coro_factory) -> None:
    """Entry point helper: run a coroutine and exit with its status."""
    sys.exit(asyncio.run(coro_factory()).exit_code())
