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


# -- a failed feed must not look like an empty one ------------------------


def make_failing_handler(failing: set[str]):
    """Handler that 500s the named export types, like a throttle or outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        rtype = request.url.params.get("TYPE")
        if rtype in failing:
            return httpx.Response(500, text="server error")
        return make_handler()(request)

    return handler


async def test_snapshot_records_which_feeds_degraded(settings: Settings):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(make_failing_handler({"injuries"}))
    )
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
    assert "injuries" in snap.degraded
    assert "free_agents" not in snap.degraded


async def test_healthy_snapshot_has_no_degraded_feeds(settings: Settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
    assert snap.degraded == {}

async def test_missing_free_agent_list_fails_the_job(settings: Settings):
    """A throttled free-agent fetch must not report success.

    This is the bug that made a green run show no data: the fetch degraded to
    an empty list, the job printed "0 free agents", and exit_code() returned 0
    because nothing raised.
    """
    from jobs._common import JobResult
    from jobs.wednesday_waivers import body

    def handler(request: httpx.Request) -> httpx.Response:
        # Only the free-agent requests fail. The full player pool also uses
        # TYPE=players and is mandatory, so failing it outright would take the
        # whole snapshot down instead of exercising the degradation path.
        rtype = request.url.params.get("TYPE")
        if rtype == "freeAgents":
            return httpx.Response(500, text="server error")
        if rtype == "players" and request.url.params.get("STATUS") == "freeagent":
            return httpx.Response(500, text="server error")
        return make_handler()(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
        result = JobResult("wednesday-waivers")
        await body(client, snap, result)

    assert snap.degraded.get("free_agents")
    assert result.error is not None, "must not pass silently"
    assert result.exit_code() == 1
    text = "\n".join(result.lines)
    assert "could not be fetched" in text
    assert "Free agents available: 0" not in text


async def test_genuinely_empty_free_agent_pool_still_passes(settings: Settings):
    """Zero free agents that genuinely exist is a real, passing result."""
    from jobs._common import JobResult
    from jobs.wednesday_waivers import body

    def handler(request: httpx.Request) -> httpx.Response:
        rtype = request.url.params.get("TYPE")
        # Both the primary key and the players?STATUS=freeagent fallback must
        # be empty, otherwise the fallback legitimately supplies players.
        if rtype == "freeAgents":
            return httpx.Response(200, json={"freeAgents": {"player": []}})
        if rtype == "players" and request.url.params.get("STATUS") == "freeagent":
            return httpx.Response(200, json={"players": {"player": []}})
        return make_handler()(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
        result = JobResult("wednesday-waivers")
        await body(client, snap, result)

    assert "free_agents" not in snap.degraded
    assert result.error is None
    assert result.exit_code() == 0
    assert "Free agents available: 0" in "\n".join(result.lines)


async def test_missing_projections_warn_but_do_not_fail(settings: Settings):
    """Projections are an enrichment: warn, still exit 0."""
    from jobs._common import JobResult
    from jobs.wednesday_waivers import body

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(make_failing_handler({"projectedScores"}))
    )
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
        result = JobResult("wednesday-waivers")
        await body(client, snap, result)

    assert "projected_scores" in snap.degraded
    assert result.error is None
    assert "Projections were unavailable" in "\n".join(result.lines)


def test_step_summary_is_written_for_actions(settings: Settings, tmp_path, monkeypatch):
    """The report must land on the run page, not only in the log tail."""
    from jobs._common import JobResult, _write_step_summary

    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    result = JobResult("wednesday-waivers")
    result.say("Free agents available: 12")
    _write_step_summary(result)

    text = target.read_text()
    assert "wednesday-waivers" in text
    assert "Free agents available: 12" in text
    assert "OK" in text


def test_step_summary_flags_a_failure(settings: Settings, tmp_path, monkeypatch):
    from jobs._common import JobResult, _write_step_summary

    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    result = JobResult("wednesday-waivers")
    result.error = "free agent list unavailable"
    _write_step_summary(result)

    assert "FAILED" in target.read_text()
    assert "free agent list unavailable" in target.read_text()


def test_step_summary_is_skipped_outside_actions(settings: Settings, monkeypatch):
    from jobs._common import JobResult, _write_step_summary

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    result = JobResult("wednesday-waivers")
    result.say("nothing to see")
    _write_step_summary(result)  # must not raise


# -- depth penalty must not be double-counted -----------------------------


async def test_same_position_swap_ignores_the_depth_penalty(settings: Settings):
    """Dropping a player at the same position removes the surplus the depth
    penalty charges for. Applying both rejected nearly every same-position
    swap, because the penalty (0.45/surplus) exceeded the gain threshold.
    """
    from mcp_server.waivers import recommend_moves

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    # The fixtures have a deep WR group, which is exactly the triggering case.
    wr_roster = [r for r in snap.roster if r.position == "WR"]
    assert len(wr_roster) >= 2, "fixture must exercise roster depth"

    moves = recommend_moves(snap.roster, snap.free_agent_values(60), snap.pool, snap.league)
    for move in moves:
        if move.add.player.position == move.drop.position:
            # A same-position swap must not be discounted for depth.
            assert move.add_value == pytest.approx(
                move.add.adjusted, abs=0.001
            ), "depth penalty double-counted on a same-position swap"


async def test_moves_are_found_for_a_deep_roster(settings: Settings):
    """The regression that motivated the fix: zero moves on a deep roster."""
    from mcp_server.waivers import recommend_moves

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)
    moves = recommend_moves(
        snap.roster, snap.free_agent_values(60), snap.pool, snap.league
    )
    assert moves, "expected at least one waiver move"


async def test_cross_position_add_still_pays_the_depth_penalty(settings: Settings):
    """Only same-position swaps are exempt; a kept surplus is real."""
    from mcp_server.waivers import recommend_moves

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    moves = recommend_moves(snap.roster, snap.free_agent_values(60), snap.pool, snap.league)
    cross = [m for m in moves if m.add.player.position != m.drop.position]
    for move in cross:
        assert move.add_value <= move.add.adjusted + 0.001


def test_nearest_miss_explains_an_empty_result(settings: Settings):
    """A rejected run must report the numbers, not just assert emptiness."""
    from mcp_server.waivers import nearest_miss

    assert "No free agents" in nearest_miss([], [], None, None)


def test_nearest_miss_reports_when_nothing_is_droppable(settings: Settings):
    from mcp_server.fantasy_engine import Player, PlayerValue
    from mcp_server.models import RosterPlayer
    from mcp_server.waivers import nearest_miss

    fa = PlayerValue(
        player=Player(player_id="0009", name="FA", position="WR"),
        projected=9.0, wsis=0.0, available=True,
    )
    # An IR player is never droppable, so there is nothing to compare against.
    roster = [
        RosterPlayer(
            player=Player(player_id="0001", name="Injured", position="QB"),
            status="IR",
        )
    ]
    assert "droppable" in nearest_miss(roster, [fa], None, None)


async def test_nearest_miss_reports_the_closest_swap(settings: Settings):
    from mcp_server.waivers import nearest_miss

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
    async with MFLClient(settings, client=http) as client:
        snap = await load_snapshot(client, week=3)

    # A threshold no swap can clear, so the near-miss path is the one exercised.
    import mcp_server.waivers as waivers

    original = waivers.MIN_VALUE_GAIN
    try:
        waivers.MIN_VALUE_GAIN = 10_000.0
        moves = waivers.recommend_moves(
            snap.roster, snap.free_agent_values(60), snap.pool, snap.league
        )
        text = waivers.nearest_miss(
            snap.roster, snap.free_agent_values(60), snap.pool, snap.league
        )
    finally:
        waivers.MIN_VALUE_GAIN = original

    assert moves == []
    assert "Closest call" in text
    assert "threshold" in text


# -- an empty roster must not masquerade as "nothing to do" ----------------


async def test_job_fails_and_reports_size_on_an_empty_roster(settings, monkeypatch):
    """An empty roster must fail loudly, not report 'nothing to do'."""
    import jobs._common as common
    from jobs.wednesday_waivers import body

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("TYPE") == "rosters":
            data = make_handler()(request).json()
            for fr in data.get("rosters", {}).get("franchise", []):
                fr["roster"] = {"player": []}
            return httpx.Response(200, json=data)
        return make_handler()(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def fake_client(_settings):
        return MFLClient(_settings, client=http)

    monkeypatch.setattr(common, "MFLClient", fake_client)
    result = await common.run_job("wednesday-waivers", body, settings=settings)

    text = "\n".join(result.lines)
    assert "Roster: 0 players" in text
    assert result.error is not None
    assert result.exit_code() == 1


async def test_job_reports_a_healthy_roster_size(settings, monkeypatch):
    from jobs.wednesday_waivers import body

    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))

    def fake_client(_settings):
        return MFLClient(_settings, client=http)

    import jobs._common as common
    monkeypatch.setattr(common, "MFLClient", fake_client)
    result = await common.run_job("wednesday-waivers", body, settings=settings)

    text = "\n".join(result.lines)
    assert result.error is None
    assert "Roster: " in text
    assert "players" in text
