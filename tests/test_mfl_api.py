"""Tests for the MFL API client: URL construction, auth, and the write gate."""

from __future__ import annotations

import httpx
import pytest

from mcp_server.config import ConfigError, Settings
from mcp_server.mfl_api import API_HOST, MFLError, MFLClient, WritesDisabledError

from .conftest import load


def make_client(settings: Settings, **kwargs) -> tuple[MFLClient, httpx.MockTransport]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("mfl_status.json"))

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return MFLClient(settings, client=http), transport


async def test_status_uses_dynamic_endpoint(settings: Settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load("mfl_status.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        status = await client.status()

    assert str(seen[0].url).startswith(f"https://{API_HOST}/fflnetdynamic2026/mfl_status.json")
    assert status["current_week"] == 3
    assert status["lineup_week"] == 4
    assert status["year"] == "2026"


async def test_export_targets_league_host_and_injects_credentials(settings: Settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load("league.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        await client.league()

    url = seen[0].url
    assert "www99.myfantasyleague.com" in str(url)
    assert url.params["TYPE"] == "league"
    assert url.params["L"] == "99999"
    assert url.params["JSON"] == "1"
    # APIKEY takes precedence per the MFL docs, and is sent on exports.
    assert url.params["APIKEY"] == "TESTAPIKEY"


async def test_cookies_are_used_when_no_apikey(settings: Settings):
    from dataclasses import replace

    settings = replace(settings, apikey=None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load("league.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        client._cookie = "COOKIEVALUE=="
        await client.league()

    assert "MFL_USER_ID=COOKIEVALUE==" in seen[0].headers["cookie"]
    assert "APIKEY" not in seen[0].url.params


async def test_login_extracts_cookie(settings: Settings):
    from dataclasses import replace

    settings = replace(settings, apikey=None)
    body = '<response><status cookie_name="MFL_USER_ID" cookie_value="abc123=="/></response>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        cookie = await client.login()
    assert cookie == "abc123=="


async def test_login_reports_bad_credentials(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<response><error>Invalid login</error></response>")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        with pytest.raises(MFLError, match="rejected"):
            await client.login()


async def test_rate_limit_is_not_retried(settings: Settings):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="Too Many Requests")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        with pytest.raises(MFLError, match="rate limit"):
            await client.status()
    # MFL says explicitly: do not retry.
    assert calls == 1


async def test_api_error_payload_raises(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "Invalid league ID"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        with pytest.raises(MFLError, match="Invalid league ID"):
            await client.league()


async def test_status_needs_no_credentials(settings: Settings):
    """mfl_status is public, so it must work before anything is configured."""
    from dataclasses import replace

    anonymous = replace(settings, username=None, password=None, apikey=None)
    assert anonymous.can_read is False

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("mfl_status.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(anonymous, client=http) as client:
        status = await client.status()
    assert status["current_week"] == 3


async def test_league_data_still_requires_credentials(settings: Settings):
    from dataclasses import replace

    anonymous = replace(settings, username=None, password=None, apikey=None)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with MFLClient(anonymous, client=http) as client:
        with pytest.raises(ConfigError, match="MFL_APIKEY"):
            await client.league()


# -- write gate ---------------------------------------------------------


async def test_writes_refused_when_dry_run(settings: Settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with MFLClient(settings, client=http) as client:
        with pytest.raises(WritesDisabledError, match="ENABLE_WRITES"):
            await client.set_lineup(3, ["0001"])


async def test_writes_refused_when_dry_run_flag_still_set(writable_settings: Settings):
    from dataclasses import replace

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with MFLClient(replace(writable_settings, dry_run=True), client=http) as client:
        with pytest.raises(WritesDisabledError, match="DRY_RUN"):
            await client.set_lineup(3, ["0001"])


async def test_writes_refused_without_password(settings: Settings):
    from dataclasses import replace

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    stripped = replace(settings, password=None, dry_run=False, enable_writes=True)
    async with MFLClient(stripped, client=http) as client:
        with pytest.raises(WritesDisabledError, match="MFL_USERNAME"):
            await client.set_lineup(3, ["0001"])


async def test_lineup_import_parameters(writable_settings: Settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": {"ok": "1"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(writable_settings, client=http) as client:
        client._cookie = "C"
        client._logged_in = True
        await client.set_lineup(4, ["0001", "0002"], comments="hello", tiebreakers=["0009"])

    url = seen[0].url
    assert url.path.endswith("/import")
    assert url.params["TYPE"] == "lineup"
    assert url.params["W"] == "4"
    assert url.params["STARTERS"] == "0001,0002"
    assert url.params["COMMENTS"] == "hello"
    assert url.params["TIEBREAKERS"] == "0009"


async def test_fcfs_move_uses_add_and_drop(writable_settings: Settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": {"ok": "1"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(writable_settings, client=http) as client:
        client._cookie = "C"
        client._logged_in = True
        await client.fcfs_move(add="20", drop=["30", "31"])

    url = seen[0].url
    assert url.params["TYPE"] == "fcfsWaiver"
    # Ids are normalised to their zero-padded string form.
    assert url.params["ADD"] == "0020"
    assert url.params["DROP"] == "0030,0031"


async def test_waiver_request_pic_format(writable_settings: Settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": {"ok": "1"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(writable_settings, client=http) as client:
        client._cookie = "C"
        client._logged_in = True
        await client.submit_waiver_round([("20", "30"), ("21", "31")], round_number=1)

    url = seen[0].url
    assert url.params["TYPE"] == "waiverRequest"
    assert url.params["PICKS"] == "0020_0030,0021_0031"
    assert url.params["ROUND"] == "1"
    assert url.params["REPLACE"] == "1"


async def test_player_ids_keep_leading_zeros(settings: Settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load("players.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        players = await client.players()
        await client.projected_scores(players=["1", "0020"])

    assert players[0].player_id == "0001"
    assert seen[-1].url.params["PLAYERS"] == "0001,0020"
