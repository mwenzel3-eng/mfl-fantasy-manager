"""Shared test fixtures.

Tests run entirely offline: every MFL response is served from
``tests/fixtures``, and the safety switch defaults to read-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from mcp_server.config import Settings
from mcp_server.fantasy_engine import build_pool
from mcp_server.models import (
    LeagueSettings,
    Player,
    RosterEntry,
    RosterPlayer,
    norm_roster_status,
    parse_slot_spec,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Week the fixtures represent.
WEEK = 3
# Week with a New England bye, used to exercise the bye-week path.
BYE_WEEK = 5


@pytest.fixture(autouse=True)
def isolated_mfl_env():
    """Snapshot and restore MFL_* environment variables around every test.

    ``load_dotenv`` mutates ``os.environ`` in place, so without this a test that
    loads a ``.env`` file would leak its settings into every test after it. The
    settings cache is cleared too, since ``get_settings`` memoises on first use.
    """
    from mcp_server.config import get_settings

    saved = {k: v for k, v in os.environ.items() if k.startswith(("MFL_", "SMS_", "TWILIO_"))}
    for key in saved:
        del os.environ[key]
    get_settings.cache_clear()
    yield
    for key in [k for k in os.environ if k.startswith(("MFL_", "SMS_", "TWILIO_"))]:
        del os.environ[key]
    os.environ.update(saved)
    get_settings.cache_clear()


def apply_to_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Publish a :class:`Settings` into the environment.

    Used by tests that exercise code paths which resolve settings from the
    ambient environment (the MCP tools do, by design) rather than taking an
    injected object.
    """
    from mcp_server.config import get_settings

    values = {
        "MFL_LEAGUE_ID": settings.league_id,
        "MFL_YEAR": settings.year,
        "MFL_USERNAME": settings.username,
        "MFL_PASSWORD": settings.password,
        "MFL_APIKEY": settings.apikey,
        "MFL_DRY_RUN": "1" if settings.dry_run else "0",
        "MFL_ENABLE_WRITES": "1" if settings.enable_writes else "0",
    }
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        league_id="99999",
        year="2026",
        username="testuser",
        password="testpassword",
        apikey="TESTAPIKEY",
        host="www99.myfantasyleague.com",
        dry_run=True,
        enable_writes=False,
        request_delay=0.0,
        timeout=5.0,
        cache_ttl=0.0,
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def writable_settings(settings: Settings) -> Settings:
    """Same credentials, but with both write switches flipped on."""
    from dataclasses import replace

    return replace(settings, dry_run=False, enable_writes=True)


@pytest.fixture
def players() -> list[Player]:
    return [Player.from_json(p) for p in load("players.json")["players"]["player"]]


@pytest.fixture
def league() -> LeagueSettings:
    raw = load("league.json")["league"]
    def counts(spec: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for pos, _ in parse_slot_spec(spec):
            out[pos] = out.get(pos, 0) + 1
        return out

    return LeagueSettings(
        name=raw["name"],
        season=raw["seasonYear"],
        host=raw["host"],
        roster_positions=counts(raw["roster_positions"]),
        starter_slots=parse_slot_spec(raw["starters"]),
        roster_limits=counts(raw["roster_limits"]),
        ir_slots=3,
        taxi_slots=2,
        current_week=raw["currentWeek"],
        final_week=raw["finalWeek"],
        franchise_count=2,
    )


@pytest.fixture
def roster_entries() -> list[RosterEntry]:
    franchise = load("rosters.json")["rosters"]["franchise"][0]
    return [
        RosterEntry(
            player_id=p["id"],
            franchise_id="0001",
            # Normalised here so the fixture exercises the same mapping the
            # client applies to the live "STARTER"/"INJURED_RESERVE" values.
            status=norm_roster_status(p["status"]),
            salary=p.get("salary", ""),
        )
        for p in franchise["player"]
    ]


@pytest.fixture
def roster(roster_entries: list[RosterEntry], players: list[Player]) -> list[RosterPlayer]:
    """My roster joined with player metadata, as ``hydrate`` would produce."""
    index = {p.player_id: p for p in players}
    injuries = {
        i["player_id"]: i for i in load("injuries.json")["injuries"]["injury"]
    }
    out: list[RosterPlayer] = []
    for entry in roster_entries:
        base = index[entry.player_id]
        inj = injuries.get(entry.player_id, {})
        player = Player(
            player_id=base.player_id,
            name=base.name,
            position=base.position,
            team=base.team,
            nfl_team=base.nfl_team,
            status=base.status,
            injury_status=inj.get("status", ""),
            injury_detail=inj.get("details", ""),
            bye_week=base.bye_week,
        )
        out.append(RosterPlayer(player=player, status=entry.status, salary=entry.salary))
    return out


@pytest.fixture
def pool(players: list[Player]) -> Any:
    projected = {
        s["id"]: float(s["score"])
        for s in load("projected_scores.json")["projectedScores"]["playerScore"]
    }
    wsis = {
        s["id"]: float(s["score"])
        for s in load("wsis.json")["whoShouldIStart"]["playerScore"]
    }
    return build_pool(players, week=WEEK, projected=projected, wsis=wsis)
