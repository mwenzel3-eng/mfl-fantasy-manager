"""Tests for the player pool, value adjustment and config loading."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mcp_server.config import ConfigError, load_settings
from mcp_server.fantasy_engine import build_pool, drop_candidates
from mcp_server.models import (
    OUT_STATUSES,
    Player,
    as_list,
    franchise_id,
    parse_slot_spec,
    player_id,
)


# -- models -------------------------------------------------------------


def test_as_list_normalises_mfl_shapes():
    assert as_list(None) == []
    assert as_list({"a": 1}) == [{"a": 1}]
    assert as_list([1, 2]) == [1, 2]


def test_player_ids_are_zero_padded_strings():
    assert player_id(1) == "0001"
    assert player_id("531") == "0531"
    assert player_id("12345") == "12345"
    assert player_id(None) == ""


def test_franchise_ids_are_four_digits():
    assert franchise_id(1) == "0001"
    assert franchise_id("12") == "0012"


def test_parse_slot_spec_handles_empty_and_odd_input():
    assert parse_slot_spec("") == ()
    # Trailing count with no position is ignored rather than crashing.
    assert parse_slot_spec("QB,1,2") == (("QB", "QB"),)


def test_player_out_and_questionable_classification():
    out = Player("1", "X", "WR", injury_status="Out")
    q = Player("2", "Y", "WR", injury_status="Questionable")
    healthy = Player("3", "Z", "WR")
    assert out.is_out and not out.is_questionable
    assert q.is_questionable and not q.is_out
    assert not healthy.is_out and not healthy.is_questionable
    assert "Out" in OUT_STATUSES


def test_flex_eligible_positions():
    assert Player("1", "X", "RB").is_flex_eligible
    assert Player("1", "X", "WR").is_flex_eligible
    assert not Player("1", "X", "QB").is_flex_eligible
    assert not Player("1", "X", "K").is_flex_eligible


# -- engine -------------------------------------------------------------


def test_pool_ranks_by_adjusted_value(pool):
    ranked = pool.ranked()
    adjusted = [v.adjusted for v in ranked]
    assert adjusted == sorted(adjusted, reverse=True)


def test_pool_projection_rank_ignores_injuries(pool):
    # 0023 has the second-best raw projection but is Out, so it ranks below the
    # healthy players even though nothing is wrong with its projection.
    assert pool.value("0023").projection_rank == 2
    assert pool.rank_of("0023") == 2
    assert pool.value("0001").projection_rank == 1


def test_out_player_is_devalued_heavily(pool):
    healthy = pool.value("0004")          # 16.0 projected, healthy
    out = pool.value("0023")              # 20.0 projected, Out with ACL
    assert out.projected > healthy.projected
    assert out.adjusted < healthy.adjusted
    assert out.available is False


def test_questionable_player_is_devalued_moderately(pool):
    healthy = pool.value("0006")          # 6.5 projected, Out
    q = pool.value("0003")                # 9.5 projected, Questionable
    assert q.projected > healthy.projected
    # Questionable is punished less than Out, so it lands between them.
    assert healthy.adjusted < q.adjusted < 20.0


def test_wsis_nudges_close_projections(pool):
    from mcp_server.fantasy_engine import PlayerValue

    a = PlayerValue(player=Player("1", "A", "RB"), projected=10.0, wsis=90.0, available=True)
    b = PlayerValue(player=Player("2", "B", "RB"), projected=10.0, wsis=10.0, available=True)
    assert a.adjusted > b.adjusted


def test_projection_rank_is_assigned(pool):
    assert pool.rank_of("0001") == 1
    assert pool.value("0001").projection_rank == 1


def test_top_by_position_filters_correctly(pool):
    wrs = pool.top_by_position("WR", limit=10)
    assert all(v.player.position == "WR" for v in wrs)


def test_startable_only_excludes_out_players(pool):
    available = pool.ranked(startable_only=True)
    assert all(v.available for v in available)
    assert not any(v.player_id == "0023" for v in available)


def test_pool_handles_players_without_projection_data(players):
    empty = build_pool(players, week=1)
    assert empty.value("0001").projected == 0.0
    assert empty.ranked()


def test_drop_candidates_respects_min_value(pool, roster, league):
    ranked = drop_candidates(roster, pool, league=league, min_value=6.0)
    assert all(value >= 6.0 for _entry, value in ranked)


# -- config -------------------------------------------------------------


def test_load_settings_requires_league_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for key in ("MFL_LEAGUE_ID", "MFL_YEAR", "MFL_USERNAME", "MFL_PASSWORD", "MFL_APIKEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError, match="MFL_LEAGUE_ID"):
        load_settings(env_file=tmp_path / "missing.env")


def test_load_settings_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in list(dict(__import__("os").environ)):
        if key.startswith("MFL_"):
            monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "MFL_LEAGUE_ID=12345\n"
        "MFL_YEAR=2026\n"
        "MFL_USERNAME=me\n"
        "MFL_PASSWORD=secret\n"
        "MFL_DRY_RUN=0\n"
        "MFL_ENABLE_WRITES=yes\n"
    )
    settings = load_settings(env_file=env)
    assert settings.league_id == "12345"
    assert settings.dry_run is False
    assert settings.enable_writes is True
    assert settings.writes_allowed is True


def test_secret_values_are_not_in_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("MFL_YEAR", "2026")
    monkeypatch.setenv("MFL_PASSWORD", "hunter2")
    settings = load_settings(env_file=tmp_path / "none.env")
    assert "hunter2" not in repr(settings)


def test_write_switch_defaults_are_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("MFL_YEAR", "2026")
    monkeypatch.setenv("MFL_USERNAME", "me")
    monkeypatch.setenv("MFL_PASSWORD", "pw")
    settings = load_settings(env_file=tmp_path / "none.env")
    assert settings.dry_run is True
    assert settings.enable_writes is False
    assert settings.writes_allowed is False


def test_invalid_numeric_env_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MFL_LEAGUE_ID", "12345")
    monkeypatch.setenv("MFL_REQUEST_DELAY", "soon")
    with pytest.raises(ConfigError, match="MFL_REQUEST_DELAY"):
        load_settings(env_file=tmp_path / "none.env")
