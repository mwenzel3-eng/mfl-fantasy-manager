"""Wednesday waiver job.

Waivers in most MFL leagues clear overnight Wednesday, so Wednesday evening is
when free agents are at their most plentiful and Wednesday night/Thursday
morning is the last chance to add before lineups lock.

What it does:
1. Ranks free-agent add/drop moves by projected improvement for *this* week.
2. Optionally executes the single best move when writes are enabled.
3. Reports everything either way.
"""

from __future__ import annotations

import sys

from mcp_server.context import Snapshot
from mcp_server.fantasy_engine import explain
from mcp_server.mfl_api import MFLError, MFLClient
from mcp_server.models import SKILL_POSITIONS
from mcp_server.waivers import recommend_moves

from ._common import JobResult, run_job

JOB_NAME = "wednesday-waivers"
CANDIDATE_POOL = 120


async def body(client: MFLClient, snap: Snapshot, result: JobResult) -> None:
    # An empty free-agent list is only good news if the list actually arrived.
    # Without this check a throttled or failed fetch reports "0 free agents"
    # and the run passes, which looks identical to a league with no moves.
    if "free_agents" in snap.degraded:
        result.say("Free agent list could not be fetched, so no advice is possible.")
        result.say(f"  Reason: {snap.degraded['free_agents']}")
        result.error = "free agent list unavailable"
        return

    if "projected_scores" in snap.degraded:
        result.say("Projections were unavailable, so rankings below are unreliable.")
        result.say(f"  Reason: {snap.degraded['projected_scores']}")

    candidates = snap.free_agent_values(limit=CANDIDATE_POOL)
    result.say(f"Free agents available: {len(snap.free_agent_ids)}")

    if not candidates:
        result.say("No free agents with usable projections; nothing to do.")
        return

    moves = recommend_moves(
        snap.roster, candidates, snap.pool, snap.league, limit=8
    )
    if not moves:
        result.say("No add/drop move cleared the value threshold this week.")
        return

    result.say("")
    result.say(f"Top {len(moves)} candidate moves:")
    for index, move in enumerate(moves, start=1):
        result.say(
            f"  {index}. +{move.add.player.name} ({move.add.player.position}) "
            f"for -{move.drop.player.name} ({move.drop.position}) "
            f"[{move.net_gain:+.1f}]"
        )
        result.say(f"     {move.rationale}")

    if not snap.settings.writes_allowed:
        result.say("")
        result.say(
            "Dry run: no move submitted. Set MFL_ENABLE_WRITES=1 and "
            "MFL_DRY_RUN=0 to allow execution."
        )
        return

    best = moves[0]
    if best.add.player.position not in SKILL_POSITIONS and best.add.player.position != "QB":
        result.say("Best target is not an offensive player; skipping execution.")
        return

    result.say("")
    result.say(
        f"Executing: +{best.add.player.name} / -{best.drop.player.name} "
        f"({explain(best.add)})"
    )
    try:
        payload = await client.fcfs_move(
            add=best.add.player_id, drop=[best.drop.player_id]
        )
    except MFLError as exc:
        result.error = f"Move failed: {exc}"
        return
    result.changed = True
    result.say("Move accepted by MFL.")
    result.say(str(payload)[:400])


async def run(*, week: int | None = None) -> JobResult:
    return await run_job(JOB_NAME, body, week=week)


def main() -> None:
    from ._common import main_wrapper

    main_wrapper(run)


if __name__ == "__main__":
    main()
