"""Thursday lineup job.

Lineups lock shortly before kickoff, so this runs Thursday afternoon: late
enough to see Thursday morning injury news, early enough to fix anything the
optimizer finds before the deadline.

What it does:
1. Compares the currently submitted lineup against the optimiser's proposal.
2. Submits the change when writes are enabled and the projected gain clears
   the safety threshold.
3. Otherwise just reports the difference.
"""

from __future__ import annotations

from mcp_server.context import Snapshot
from mcp_server.lineup import LineupError, compare_lineups
from mcp_server.mfl_api import MFLError, MFLClient
from mcp_server.safety import should_submit_lineup

from ._common import JobResult, run_job

JOB_NAME = "thursday-lineup"


async def body(client: MFLClient, snap: Snapshot, result: JobResult) -> None:
    try:
        current = snap.current_lineup()
        proposed = snap.best_lineup()
    except LineupError as exc:
        result.error = f"Could not build a lineup: {exc}"
        return

    result.say("")
    result.say("Currently submitted:")
    result.say(f"  {', '.join(current.starter_names) or '(no lineup submitted)'}")
    result.say(f"  projected {current.projected_total:.1f}")

    result.say("")
    result.say(proposed.summary())

    diff = compare_lineups(current, proposed)
    if not diff["changed"]:
        result.say("")
        result.say("Lineup already optimal. Nothing to do.")
        return

    result.say("")
    result.say(
        f"Change: +{', '.join(diff['added']) or 'nothing'} "
        f"/ -{', '.join(diff['removed']) or 'nothing'} "
        f"({diff['delta_points']:+.1f} projected)"
    )

    if not snap.settings.writes_allowed:
        result.say("Dry run: lineup not submitted.")
        return

    submit, why = should_submit_lineup(
        snap.settings,
        current_ids=current.starter_ids,
        proposed_ids=proposed.starter_ids,
        current_total=current.projected_total,
        proposed_total=proposed.projected_total,
    )
    if not submit and not result.force:
        result.say(f"Not submitting: {why}")
        return

    if result.force and not submit:
        result.say(f"Submitting anyway (--force): {why}")

    result.say(f"Submitting: {why}")
    try:
        payload = await client.set_lineup(
            snap.week,
            proposed.starter_ids,
            comments="Set by mfl-fantasy-manager (Thursday job)",
        )
    except MFLError as exc:
        result.error = f"Lineup submission failed: {exc}"
        return
    result.changed = True
    result.say("Lineup accepted by MFL.")
    result.say(str(payload)[:400])


async def run(*, week: int | None = None, force: bool = False) -> JobResult:
    return await run_job(JOB_NAME, body, week=week, force=force)


def main() -> None:
    from ._common import main_wrapper

    main_wrapper(run)


if __name__ == "__main__":
    main()
