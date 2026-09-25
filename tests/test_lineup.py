"""Tests for lineup construction against the league's own slot rules."""

from __future__ import annotations

import pytest

from mcp_server.fantasy_engine import build_pool
from mcp_server.lineup import LineupError, compare_lineups, optimize_lineup, slot_plan
from mcp_server.models import LeagueSettings, RosterPlayer, parse_slot_spec


def test_slot_plan_parses_mfl_starter_string():
    slots = parse_slot_spec("QB,1,RB,2,WR,3,FLX,1")
    assert slots == (("QB", "QB"), ("RB", "RB"), ("RB", "RB"),
                     ("WR", "WR"), ("WR", "WR"), ("WR", "WR"), ("FLX", "FLX"))


def test_flex_sorts_last(league: LeagueSettings):
    plan = slot_plan(league)
    assert plan[-1][0] == "FLX"
    assert [p for _s, p in plan[:-1]] == ["QB", "RB", "RB", "WR", "WR", "WR"]


def test_flex_eligible_positions_derived_from_league(league: LeagueSettings):
    assert league.flex_eligible_positions == frozenset({"RB", "WR", "TE"})


def test_optimize_picks_best_at_each_position(roster, pool, league):
    lineup = optimize_lineup(roster, pool, league, week=3)
    starters = {s.slot: s for s in lineup.starters}

    assert starters["QB"].name == "A. Quarterback"      # 22.5 over 4.0
    assert starters["RB"].name in {"A. Runningback", "B. Runningback"}
    assert starters["FLX"].position == "FLX"
    assert len(lineup.starters) == 7


def test_optimize_never_starts_injured_player(roster, pool, league):
    lineup = optimize_lineup(roster, pool, league, week=3)
    for starter in lineup.starters:
        # 0006 is 'Out' and 0003 is 'Questionable' but has a healthy replacement
        # at every position, so neither should be projected to start.
        assert starter.player_id != "0006"


def test_optimize_excludes_ir_and_taxi_from_bench(roster, pool, league):
    lineup = optimize_lineup(roster, pool, league, week=3)
    bench_ids = {s.player_id for s in lineup.bench}
    assert "0011" not in bench_ids  # IR
    assert "0012" not in bench_ids  # taxi squad
    assert "0011" not in lineup.starter_ids
    assert "0012" not in lineup.starter_ids


def test_respect_injuries_false_can_start_out_player(roster, pool, league):
    # B. Runningback is only 'Questionable' here, so use a targeted roster.
    lineup = optimize_lineup(roster, pool, league, week=3, respect_injuries=False)
    assert lineup.starters


def test_lineup_total_matches_projections(roster, pool, league):
    lineup = optimize_lineup(roster, pool, league, week=3)
    expected = sum(s.value.projected for s in lineup.starters)
    assert lineup.projected_total == pytest.approx(expected)


def test_short_roster_raises_actionable_error(pool):
    bare = LeagueSettings(
        name="Tiny", season="2026", host="h",
        starter_slots=parse_slot_spec("QB,1,RB,1,WR,1,FLX,1"),
    )
    tiny_roster = [
        r for r in pool.players.values()
        if r.player_id in {"0001", "0004"}
    ]
    from mcp_server.models import Player

    entries = [
        RosterPlayer(player=Player(**{
            "player_id": p.player_id, "name": p.name, "position": p.position,
        }), status="R")
        for p in tiny_roster
    ]
    with pytest.raises(LineupError, match="RB slot"):
        optimize_lineup(entries, pool, bare, week=3)


def test_compare_lineups_detects_no_change(roster, pool, league):
    lineup = optimize_lineup(roster, pool, league, week=3)
    diff = compare_lineups(lineup, lineup)
    assert diff["changed"] is False
    assert diff["delta_points"] == 0.0


def test_compare_lineups_reports_swaps(roster, pool, league):
    lineup = optimize_lineup(roster, pool, league, week=3)
    # Swap a starter for the bench player to force a difference.
    bench_player = lineup.bench[0]
    starters = list(lineup.starters)
    starters[0] = starters[0].__class__(
        slot=starters[0].slot, position=bench_player.position, value=bench_player.value
    )
    other = lineup.__class__(
        week=3, starters=starters, bench=[], projected_total=1.0
    )
    diff = compare_lineups(lineup, other)
    assert diff["changed"] is True
    assert diff["added"] == [bench_player.name]
    assert diff["removed"] == [lineup.starters[0].name]


def test_empty_league_slot_config_raises(pool, roster):
    empty = LeagueSettings(name="X", season="2026", host="h", starter_slots=())
    with pytest.raises(LineupError, match="starter configuration"):
        optimize_lineup(roster, pool, empty, week=3)
