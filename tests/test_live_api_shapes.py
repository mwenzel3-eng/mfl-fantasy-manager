"""Regression tests for response shapes verified against the live MFL API.

Each test here corresponds to something the live API did that a reasonable
person would not have guessed, and each failed silently rather than loudly:

* ``export?TYPE=league`` returns ``baseURL``, not ``host``.
* ``abilities`` nests the franchise id as ``franchise.id``, not ``franchise_id``,
  and the league export's ``user`` element is null for non-owners.
* ``freeAgents`` is spelled camelCase in the response, so reading
  ``free_agents`` silently yields zero free agents.
* ``myleagues`` returns its list under the key ``leagues``.
* The bye-week export is ``nflByeWeeks``, and ``injuries``/``nflByeWeeks``/
  ``myleagues``/``playerRanks`` reject a league id outright.
"""

from __future__ import annotations

import httpx

from mcp_server.config import Settings
from mcp_server.mfl_api import _GLOBAL_EXPORTS, MFLClient

from .test_auth import Recorder


def test_global_exports_exclude_the_types_that_need_a_league():
    """projectedScores and players accept L; the globals reject it."""
    assert "injuries" in _GLOBAL_EXPORTS
    assert "nflByeWeeks" in _GLOBAL_EXPORTS
    assert "myleagues" in _GLOBAL_EXPORTS
    assert "playerRanks" in _GLOBAL_EXPORTS
    # These are league-scoped and must keep their L, verified live.
    assert "players" not in _GLOBAL_EXPORTS
    assert "projectedScores" not in _GLOBAL_EXPORTS
    assert "whoShouldIStart" not in _GLOBAL_EXPORTS


async def test_league_scoped_export_keeps_the_league_id(settings: Settings):
    rec = Recorder()
    async with rec.client(settings) as client:
        await client.export("rosters")
    assert rec.api_calls[0].url.params["L"] == "99999"


async def test_global_export_omits_the_league_id(settings: Settings):
    """MFL rejects these outright, with a message about the api host."""
    rec = Recorder()
    async with rec.client(settings) as client:
        await client.export("injuries", W=4)
    assert "L" not in rec.api_calls[0].url.params
    assert rec.api_calls[0].url.params["W"] == "4"


async def test_global_export_is_routed_to_the_api_host(settings: Settings):
    """Global feeds must not go to the league's own server."""
    from dataclasses import replace

    rec = Recorder()
    async with rec.client(replace(settings, host="www42.myfantasyleague.com")) as client:
        await client.export("nflByeWeeks", W=4)
    assert "api.myfantasyleague.com" in str(rec.api_calls[0].url)
    assert "www42" not in str(rec.api_calls[0].url)


async def test_league_scoped_export_uses_the_league_host(settings: Settings):
    from dataclasses import replace

    rec = Recorder()
    async with rec.client(replace(settings, host="www42.myfantasyleague.com")) as client:
        await client.export("rosters")
    assert "www42.myfantasyleague.com" in str(rec.api_calls[0].url)


async def test_league_host_is_read_from_baseurl(settings: Settings):
    """MFL moved 'host' to a full 'baseURL' in the JSON export."""
    from dataclasses import replace

    rec = Recorder(responses={"default": {"league": {"name": "L", "baseURL": "https://www42.myfantasyleague.com"}}})
    async with rec.client(replace(settings, host=None)) as client:
        assert await client.ensure_league_host() == "https://www42.myfantasyleague.com"
    # And it is then used for league requests.
    async with rec.client(replace(settings, host=None)) as client:
        await client.ensure_league_host()
        await client.export("rosters")
    assert "www42" in str(rec.api_calls[-1].url)


async def test_legacy_host_attribute_still_works(settings: Settings):
    from dataclasses import replace

    rec = Recorder(responses={"default": {"league": {"host": "www47.myfantasyleague.com"}}})
    async with rec.client(replace(settings, host=None)) as client:
        assert await client.ensure_league_host() == "www47.myfantasyleague.com"


async def test_free_agents_reads_the_camelcase_key(settings: Settings):
    """Reading 'free_agents' returned zero, which looked like an empty pool."""
    rec = Recorder(responses={"default": {"freeAgents": {"player": [{"id": "0001"}, {"id": "0002"}]}}})
    async with rec.client(settings) as client:
        ids = await client.free_agents()
    assert ids == ["0001", "0002"]


async def test_free_agents_reads_the_live_league_unit_nesting(settings: Settings):
    """The live export nests the list under freeAgents.leagueUnit.player.

    Reading freeAgents.player returns nothing, which is why the code once fell
    back to players?STATUS=freeagent - a parameter the API ignores, so the
    "fallback" returned every player in the league and the job ranked a team
    defense as its best available add.
    """
    rec = Recorder(
        responses={
            "default": {
                "freeAgents": {
                    "leagueUnit": {"player": [{"id": "0009"}, {"id": "0010"}]}
                }
            }
        }
    )
    async with rec.client(settings) as client:
        ids = await client.free_agents()
    assert ids == ["0009", "0010"]


async def test_free_agents_never_falls_back_to_every_player(settings: Settings):
    """An unreadable response must yield no free agents, not the whole league."""
    rec = Recorder(responses={"default": {"freeAgents": {}}})
    async with rec.client(settings) as client:
        ids = await client.free_agents()
    assert ids == []
    # Exactly one request: no players?STATUS=freeagent second attempt.
    assert [r.url.params.get("TYPE") for r in rec.api_calls] == ["freeAgents"]


async def test_free_agent_entries_expose_claim_status(settings: Settings):
    rec = Recorder(
        responses={
            "default": {
                "freeAgents": {
                    "leagueUnit": {
                        "player": [{"id": "0009", "status": "locked"}, {"id": "0010"}]
                    }
                }
            }
        }
    )
    async with rec.client(settings) as client:
        entries = await client.free_agent_entries()
    assert [e.get("status") for e in entries] == ["locked", None]


async def test_projected_scores_reads_the_camelcase_key(settings: Settings):
    """The export uses playerScore, not the documented player_score."""
    rec = Recorder(
        responses={
            "default": {
                "projectedScores": {
                    "playerScore": [{"id": "0001", "score": "9.5"}, {"id": "0002", "score": ""}],
                    "week": "4",
                }
            }
        }
    )
    async with rec.client(settings) as client:
        scores = await client.projected_scores(week=4)
    assert scores["0001"] == 9.5
    assert scores["0002"] == 0.0


async def test_projected_scores_still_accepts_snake_case(settings: Settings):
    rec = Recorder(
        responses={
            "default": {"projectedScores": {"player_score": [{"id": "0001", "score": "3.5"}]}}
        }
    )
    async with rec.client(settings) as client:
        assert (await client.projected_scores(week=4))["0001"] == 3.5


async def test_franchise_id_reads_the_nested_abilities_shape(settings: Settings):
    """abilities.franchise.id, not abilities.franchise_id as documented."""
    rec = Recorder(
        responses={"default": {"abilities": {"franchise": {"id": "0005", "ability": []}}}}
    )
    async with rec.client(settings) as client:
        assert await client.my_franchise_id() == "0005"


async def test_franchise_id_still_accepts_the_documented_flat_shape(settings: Settings):
    rec = Recorder(responses={"default": {"abilities": {"franchise_id": "0003"}}})
    async with rec.client(settings) as client:
        assert await client.my_franchise_id() == "0003"


async def test_franchise_id_falls_back_to_the_commissioner_list(settings: Settings):
    """The league export's 'user' element is null unless you own the league."""
    rec = Recorder(
        responses={
            "default": {
                "league": {
                    "commish_username": "someone,guy",
                    "franchises": {
                        "franchise": [
                            {"id": "0001", "owner_name": "someone"},
                            {"id": "0002", "owner_name": "other"},
                        ]
                    },
                }
            }
        }
    )
    async with rec.client(settings) as client:
        assert await client.my_franchise_id() == "0001"


async def test_franchise_id_explicit_setting_wins(settings: Settings):
    from dataclasses import replace

    rec = Recorder()
    async with rec.client(replace(settings, franchise_id="0008")) as client:
        assert await client.my_franchise_id() == "0008"
    assert rec.api_calls == [], "an explicit id needs no network call"


async def test_my_leagues_reads_the_leagues_key(settings: Settings):
    """The TYPE is 'myleagues' but the response key is 'leagues'."""
    rec = Recorder(
        responses={
            "default": {"leagues": {"league": [{"league_id": "1", "host": "www42.myfantasyleague.com"}]}}
        }
    )
    async with rec.client(settings) as client:
        rows = await client.my_leagues()
    assert rows[0]["host"] == "www42.myfantasyleague.com"
    assert "L" not in rec.api_calls[0].url.params


async def test_bye_weeks_uses_the_nfl_prefixed_type(settings: Settings):
    rec = Recorder(responses={"default": {"nflByeWeeks": {"team": [{"id": "NE", "bye": "5"}]}}})
    async with rec.client(settings) as client:
        await client.bye_weeks(4)
    assert rec.api_calls[0].url.params["TYPE"] == "nflByeWeeks"


# -- 429 must not be walked into repeatedly ------------------------------


async def test_a_429_stops_further_requests(settings: Settings):
    """MFL says retrying a throttled request makes it worse."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="Too Many Requests")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        import pytest

        from mcp_server.errors import MFLError

        with pytest.raises(MFLError, match="429"):
            await client.export("rosters")
        with pytest.raises(MFLError, match="Skipped"):
            await client.export("rosters")
        with pytest.raises(MFLError, match="Skipped"):
            await client.export("players")
    assert calls == 1, "no further requests may be sent after a throttle"


async def test_a_429_does_not_poison_a_fresh_client(settings: Settings):
    """The breaker is per client, so a later run starts clean."""
    import pytest

    from mcp_server.errors import MFLError
    from .conftest import load

    def throttled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests")

    http = httpx.AsyncClient(transport=httpx.MockTransport(throttled))
    async with MFLClient(settings, client=http) as client:
        with pytest.raises(MFLError):
            await client.export("rosters")

    ok = Recorder()
    async with ok.client(settings) as client:
        assert client._throttled_at is None
        await client.export("rosters")
    assert ok.api_calls


# -- rosters export: the player list is not nested ------------------------


async def test_roster_players_live_shape_has_no_wrapper(settings: Settings):
    """The live export puts 'player' directly on the franchise."""
    from mcp_server.mfl_api import roster_players

    players = roster_players({"id": "0005", "week": "3", "player": [{"id": "1"}]})
    assert [p["id"] for p in players] == ["1"]


async def test_roster_players_accepts_the_documented_nested_shape(settings: Settings):
    from mcp_server.mfl_api import roster_players

    players = roster_players({"id": "0005", "roster": {"player": [{"id": "2"}]}})
    assert [p["id"] for p in players] == ["2"]


async def test_roster_players_handles_a_genuinely_empty_roster(settings: Settings):
    from mcp_server.mfl_api import roster_players

    assert roster_players({"id": "0005", "player": []}) == []
    assert roster_players({"id": "0005"}) == []


async def test_roster_is_read_from_the_live_shape(settings: Settings):
    """Regression: the roster came back empty for every franchise."""
    from mcp_server.mfl_api import MFLClient

    rec = Recorder(
        responses={
            "default": {
                "rosters": {
                    "franchise": [
                        {"id": "0001", "player": [{"id": "0001", "status": "ROSTER"}]},
                        {"id": "0002", "player": [{"id": "0002", "status": "STARTER"}]},
                    ]
                }
            }
        }
    )
    async with rec.client(settings) as client:
        entries = await client.roster_entries()
    assert {e.franchise_id for e in entries} == {"0001", "0002"}


# -- roster status is spelled out in full ---------------------------------


def test_norm_roster_status_maps_the_live_spellings():
    from mcp_server.models import STATUS_IR, STATUS_ROSTER, STATUS_STARTER, STATUS_TAXI, norm_roster_status

    assert norm_roster_status("ROSTER") == STATUS_ROSTER
    assert norm_roster_status("STARTER") == STATUS_STARTER
    assert norm_roster_status("NONSTARTER") == "NS"
    assert norm_roster_status("INJURED_RESERVE") == STATUS_IR
    assert norm_roster_status("TAXI") == STATUS_TAXI


def test_norm_roster_status_still_accepts_short_codes():
    from mcp_server.models import STATUS_IR, STATUS_ROSTER, norm_roster_status

    assert norm_roster_status("R") == STATUS_ROSTER
    assert norm_roster_status("IR") == STATUS_IR
    assert norm_roster_status("S") == "S"


def test_injured_reserve_players_are_not_droppable():
    """'INJURED_RESERVE' != 'IR' silently left IR players looking droppable."""
    from mcp_server.fantasy_engine import drop_candidates
    from mcp_server.models import Player, RosterPlayer, norm_roster_status

    from mcp_server.fantasy_engine import PlayerValue, build_pool

    roster = [
        RosterPlayer(
            player=Player(player_id="1", name="Healthy", position="WR"),
            status=norm_roster_status("ROSTER"),
        ),
        RosterPlayer(
            player=Player(player_id="2", name="Hurt", position="WR"),
            status=norm_roster_status("INJURED_RESERVE"),
        ),
    ]
    pool = build_pool(
        [
            Player(player_id="1", name="Healthy", position="WR"),
            Player(player_id="2", name="Hurt", position="WR"),
        ],
        week=1,
        projected={"1": 10.0, "2": 1.0},
    )
    assert isinstance(pool.value("1"), PlayerValue)
    droppable = {entry.player_id for entry, _ in drop_candidates(roster, pool)}
    assert "2" not in droppable, "an IR player must never be droppable"
    assert "1" in droppable


# -- player position field ------------------------------------------------


def test_player_position_reads_the_live_field_name():
    from mcp_server.models import Player

    player = Player.from_json({"id": "16167", "name": "Achane, De'Von", "position": "RB", "team": "MIA"})
    assert player.position == "RB"


def test_player_position_still_accepts_pos():
    from mcp_server.models import Player

    assert Player.from_json({"id": "1", "name": "X", "pos": "QB"}).position == "QB"


def test_player_team_falls_back_to_team():
    """This export has 'team' only; there is no 'nfl_team' key."""
    from mcp_server.models import Player

    assert Player.from_json({"id": "1", "name": "X", "position": "RB", "team": "MIA"}).nfl_team == "MIA"
