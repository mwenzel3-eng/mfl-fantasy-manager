"""Tests for waiver recommendations, drop selection and depth logic."""

from __future__ import annotations

from mcp_server.fantasy_engine import depth_adjusted_value, drop_candidates
from mcp_server.waivers import claims_to_pic, recommend_claims, recommend_moves


def _free_agents(pool):
    return [pool.value(pid) for pid in ("0020", "0021", "0022", "0023") if pool.value(pid)]


def test_moves_avoid_injured_free_agents(roster, pool, league):
    moves = recommend_moves(roster, _free_agents(pool), pool, league)
    added = {m.add.player_id for m in moves}
    # 0023 is Out (ACL) despite the best projection, so it must not be added.
    assert "0023" not in added


def test_top_move_is_a_healthy_receiver(roster, pool, league):
    moves = recommend_moves(roster, _free_agents(pool), pool, league, limit=3)
    assert moves, "expected at least one recommendation"
    best = moves[0]
    # 0020 (15.5 pts) and 0021 (12.0) are healthy; 0023 (20.0) is Out.
    assert best.add.player_id in {"0020", "0021"}
    assert best.add.available is True


def test_drop_is_same_position_when_possible(roster, pool, league):
    moves = recommend_moves(roster, _free_agents(pool), pool, league, limit=3)
    for move in moves:
        assert move.drop.position == move.add.player.position, (
            f"{move.add.player.position} add should pair with a same-position drop"
        )


def test_never_drop_ir_or_taxi(roster, pool, league):
    for entry, _value in drop_candidates(roster, pool, league=league):
        assert not entry.is_ir
        assert not entry.is_taxi


def test_protected_ids_are_never_dropped(roster, pool, league):
    protect = ["0008", "0009"]  # kicker and defense
    ids = {e.player_id for e, _ in drop_candidates(roster, pool, league=league, protect_ids=protect)}
    assert not ids & set(protect)


def test_drop_candidates_ranked_weakest_first(roster, pool, league):
    ranked = drop_candidates(roster, pool, league=league)
    values = [value for _entry, value in ranked]
    assert values == sorted(values), "drops should be ordered safest-to-cut first"


def test_depth_penalty_reduces_value_of_surplus_position(roster, pool, league):
    wr = pool.value("0020")
    assert wr is not None
    # Roster fixture has three WRs; a league needing three starting WRs means a
    # fourth has limited marginal value, but it should still be positive.
    adjusted = depth_adjusted_value(wr, roster, league=league)
    assert adjusted < wr.adjusted
    assert adjusted > 0


def test_no_moves_when_candidate_pool_is_empty(roster, pool, league):
    assert recommend_moves(roster, [], pool, league) == []


def test_no_moves_when_nothing_is_droppable(pool, league):
    from mcp_server.models import Player, RosterPlayer

    only_ir = [
        RosterPlayer(
            player=Player(player_id="0011", name="J. Injured WR", position="WR"),
            status="IR",
        )
    ]
    assert recommend_moves(only_ir, _free_agents(pool), pool, league) == []


def test_claims_are_ordered_most_wanted_first(roster, pool, league):
    claims = recommend_claims(roster, _free_agents(pool), pool, league, limit=3)
    assert claims
    values = [depth_adjusted_value(c, roster, league=league) for c, _d, _w in claims]
    assert values == sorted(values, reverse=True)


def test_claims_to_pic_formats_add_underscore_drop(roster, pool, league):
    claims = recommend_claims(roster, _free_agents(pool), pool, league, limit=2)
    picks = claims_to_pic(claims)
    assert len(picks) == len(claims)
    for add, drop in picks:
        assert add and drop
        assert add != drop
