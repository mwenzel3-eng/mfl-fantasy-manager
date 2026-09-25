"""Sunday injury check.

Sunday is when the weekend's worst news is known but waiver processing has not
yet run, so it is the last cheap opportunity to react to an injury.

What it does:
1. Reports out and questionable players on the roster, plus anyone on a bye.
2. Suggests IR moves, and optionally makes them when writes are enabled.
3. Flags rostered players whose injury makes next week's lineup thin.
"""

from __future__ import annotations

from mcp_server.context import Snapshot
from mcp_server.mfl_api import MFLError, MFLClient

from ._common import JobResult, run_job

JOB_NAME = "sunday-injury-check"
MAX_IR_MOVES = 3


async def body(client: MFLClient, snap: Snapshot, result: JobResult) -> None:
    report = snap.injuries
    result.say("")
    result.say(report.summary())

    if not report.has_action_items:
        result.say("")
        result.say("No action needed.")
        return

    result.say("")
    result.say(f"IR moves identified: {len(report.ir_suggestions)}")
    if not snap.settings.writes_allowed:
        result.say("Dry run: no IR moves made.")
        return

    for alert in report.ir_suggestions[:MAX_IR_MOVES]:
        result.say(f"IR: {alert.name} ({alert.status} {alert.detail})".rstrip())
        try:
            await client.set_ir(alert.player_id, activate=True)
        except MFLError as exc:
            result.error = f"IR move failed for {alert.name}: {exc}"
            return
        result.changed = True
        result.say(f"  moved {alert.name} to IR")

    if len(report.ir_suggestions) > MAX_IR_MOVES:
        result.say(
            f"{len(report.ir_suggestions) - MAX_IR_MOVES} further IR moves "
            "skipped; raise MAX_IR_MOVES if you want them handled."
        )


async def run(*, week: int | None = None) -> JobResult:
    return await run_job(JOB_NAME, body, week=week)


def main() -> None:
    from ._common import main_wrapper

    main_wrapper(run)


if __name__ == "__main__":
    main()
