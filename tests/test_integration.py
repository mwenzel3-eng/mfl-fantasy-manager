"""End-to-end test: fixtures in, full analysis out.

This exercises the whole read path - client, hydration, pool, optimiser,
waivers and injury report - against a mocked MFL server, so it catches
integration bugs that the unit tests cannot.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_server.config import Settings
from mcp_server.context import load_snapshot
from mcp_server.lineup import compare_lineups
from mcp_server.mfl_api import MFLClient
from mcp_server.waivers import recommend_claims, recommend_moves

from .conftest import load

RESPONSES = {
    "league": "league.json",
    "rosters": "rosters.json",
    "players": "players.json",
    "projectedScores": "projected_scores.json",
    "whoShouldIStart": "wsis.json",
    "injuries": "injuries.json",
    "freeAgents": "free_agents.json",
    "nflByeWeeks": "bye_weeks.json",
}


def make_handler(*, writes_allowed_calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if "mfl_status" in str(url):
            return httpx.Response(200, json=load("mfl_status.json"))
        if "/export" in url.path:
            req_type = url.params.get("TYPE")
            if req_type not in RESPONSES:
                return httpx.Response(200, json={"error": f"unhandled {req_type}"})
            return httpx.Response(200, json=load(RESPONSES[req_type]))
        if "/import" in url.path:
            if writes_allowed_calls is not None:
                writes_allowed_calls.append(url.params.get("TYPE", ""))
            return httpx.Response(200, json={"response": {"ok": "1"}})
        return httpx.Response(404, json={"error": "not found"})

    return handler


async def test_snapshot_builds_from_fixtures(settings: Settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    assert snap.week == 3
    assert snap.league.name == "Test League"
    assert len(snap.roster) == 14
    # Injuries were merged into the roster players.
    hurt = {r.player_id for r in snap.roster if r.player.is_out}
    assert "0006" in hurt and "0011" in hurt


async def test_end_to_end_lineup_is_legal_and_avoids_the_out_wr(settings: Settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    lineup = snap.best_lineup()
    assert len(lineup.starters) == 7
    assert len(lineup.starter_ids) == len(set(lineup.starter_ids))

    # 0006 is Out; a healthy reserve WR (0031) must take the third WR slot.
    assert "0006" not in lineup.starter_ids
    assert "0031" in lineup.starter_ids

    # One QB, two RB, three WR, one flex.
    positions = sorted(s.position for s in lineup.starters)
    assert positions.count("QB") == 1
    assert positions.count("RB") == 2
    assert positions.count("WR") == 3
    assert positions.count("FLX") == 1


async def test_end_to_end_recommends_healthy_waiver_moves(settings: Settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    moves = recommend_moves(snap.roster, snap.free_agent_values(60), snap.pool, snap.league)
    assert moves
    # 0023 has the best projection but is Out, so it must never be recommended.
    assert "0023" not in {m.add.player_id for m in moves}
    assert all(not m.add.player.is_out for m in moves)

    claims = recommend_claims(snap.roster, snap.free_agent_values(60), snap.pool, snap.league)
    assert claims
    assert all(claim.available for claim, _d, _w in claims)


async def test_end_to_end_injury_report_suggests_one_ir_move(settings: Settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    report = snap.injuries
    assert report.report_timestamp == 1789000000
    # 0006 (Out, active roster) needs IR; 0011 is already on IR.
    assert [a.player_id for a in report.ir_suggestions] == ["0006"]


async def test_end_to_end_isolated_from_unavailable_optional_feeds(settings: Settings):
    """A missing consensus/bye feed should degrade, not break the snapshot."""
    from dataclasses import replace

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if "mfl_status" in str(url):
            return httpx.Response(200, json=load("mfl_status.json"))
        if url.params.get("TYPE") in {"whoShouldIStart", "nflByeWeeks"}:
            return httpx.Response(200, json={"error": "not available"})
        if url.params.get("TYPE") in RESPONSES:
            return httpx.Response(200, json=load(RESPONSES[url.params["TYPE"]]))
        return httpx.Response(404, json={"error": "nope"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    assert snap.best_lineup().starters
    assert snap.pool.value("0001").wsis == 50.0  # default when consensus is gone


async def test_job_waivers_does_not_write_when_disabled(settings: Settings, capsys):
    from jobs.wednesday_waivers import body

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
        from jobs._common import JobResult

        result = JobResult("wednesday-waivers")
        await body(client, snap, result)

    text = "\n".join(result.lines)
    assert result.error is None
    assert result.changed is False
    assert "Dry run" in text
    assert "Execute" not in text


async def test_job_lineup_does_not_write_when_disabled(settings: Settings):
    from jobs._common import JobResult
    from jobs.thursday_lineup import body

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
        result = JobResult("thursday-lineup")
        await body(client, snap, result)

    text = "\n".join(result.lines)
    assert result.error is None
    assert result.changed is False
    assert "Dry run" in text


async def test_job_injuries_does_not_write_when_disabled(settings: Settings):
    from jobs._common import JobResult
    from jobs.sunday_injury_check import body

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
        result = JobResult("sunday-injury-check")
        await body(client, snap, result)

    text = "\n".join(result.lines)
    assert result.error is None
    assert result.changed is False
    assert "Dry run" in text
    # It should still surface the player that needs an IR move.
    assert "E. Receiver" in text


async def test_job_lineup_writes_when_enabled(writable_settings: Settings):
    from dataclasses import replace

    from jobs._common import JobResult
    from jobs.thursday_lineup import body

    calls: list[str] = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler(writes_allowed_calls=calls)))
    async with MFLClient(writable_settings, client=http) as client:
        client._cookie = "C"
        client._logged_in = True
        snap = await load_snapshot(client, week=3)
        result = JobResult("thursday-lineup")
        await body(client, snap, result)

    assert calls == ["lineup"], "expected exactly one lineup import"
    assert result.changed is True


async def test_job_waivers_writes_only_the_single_best_move(writable_settings: Settings):
    from jobs._common import JobResult
    from jobs.wednesday_waivers import body

    calls: list[str] = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler(writes_allowed_calls=calls)))
    async with MFLClient(writable_settings, client=http) as client:
        client._cookie = "C"
        client._logged_in = True
        snap = await load_snapshot(client, week=3)
        result = JobResult("wednesday-waivers")
        await body(client, snap, result)

    # Deliberately conservative: one move per week, not a shopping spree.
    assert calls == ["fcfsWaiver"]
    assert result.changed is True
