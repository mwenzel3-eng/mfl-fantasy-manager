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


async def test_free_agents_falls_back_to_the_players_export(settings: Settings):
    """A renamed key must not silently mean zero free agents."""
    rec = Recorder(responses={"default": {"players": {"player": [{"id": "0009"}]}}})
    async with rec.client(settings) as client:
        ids = await client.free_agents()
    assert ids == ["0009"]
    # The fallback is an independent request, not a parse of the first one.
    types = [r.url.params.get("TYPE") for r in rec.api_calls]
    assert "freeAgents" in types and "players" in types


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
