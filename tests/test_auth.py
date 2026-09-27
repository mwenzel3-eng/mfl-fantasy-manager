"""Authentication regression tests.

MFL has two unrelated credential systems and they are easy to conflate:

* ``APIKEY`` - export (read) only, owner-level, tied to one user/franchise
  context.
* ``MFL_USER_ID`` cookie - from ``/login``, required for every import (write).

The bugs these tests guard against are both silent: a request that should be
authenticated goes out anonymous, and MFL answers anonymous requests with an
HTTP 200 and an unhelpful body, so the failure surfaces far from its cause.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from mcp_server.config import Settings
from mcp_server.errors import MFLError
from mcp_server.mfl_api import API_HOST, MFLClient, _error_text, _extract_cookie

from .conftest import load

LOGIN_BODY = (
    '<status cookie_name="MFL_USER_ID" '
    'cookie_value="dXNlcjpwYXNzd29yZA==" >MFL</status>'
)


class Recorder:
    """A mock MFL that records every request it is handed."""

    def __init__(self, *, login_body: str = LOGIN_BODY, responses: dict[str, Any] | None = None):
        self.requests: list[httpx.Request] = []
        self.login_body = login_body
        self.responses = responses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/login"):
            return httpx.Response(200, text=self.login_body)
        if path.endswith("/mfl_status.json"):
            return httpx.Response(200, json=load("mfl_status.json"))
        return httpx.Response(200, json=self.responses.get("default", load("league.json")))

    # -- helpers ---------------------------------------------------------
    @property
    def logins(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/login")]

    @property
    def api_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if not r.url.path.endswith("/login")]

    def client(self, settings: Settings) -> MFLClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return MFLClient(settings, client=http)


def cookie_of(request: httpx.Request) -> str | None:
    header = request.headers.get("cookie")
    if not header:
        return None
    return header.split("=", 1)[1]


# -- the bug: username/password config sent anonymous reads --------------


async def test_password_only_config_logs_in_before_the_first_read(settings: Settings):
    """A config with no API key must still authenticate, not go anonymous.

    Before this was fixed, nothing ever called login() implicitly, so every
    export went out with no credentials at all and MFL replied with a body
    containing no league host.
    """
    settings = replace(settings, apikey=None)
    rec = Recorder()

    async with rec.client(settings) as client:
        await client.league()

    assert len(rec.logins) == 1, "expected an implicit login before the first export"
    login_body = rec.logins[0].content.decode()
    assert "USERNAME=testuser" in login_body
    assert "PASSWORD=testpassword" in login_body

    export = rec.api_calls[0]
    assert cookie_of(export) == "dXNlcjpwYXNzd29yZA=="
    assert "APIKEY" not in export.url.params


async def test_status_stays_public_and_never_logs_in(settings: Settings):
    """mfl_status is credential-free by design; it must not trigger a login."""
    settings = replace(settings, apikey=None)
    rec = Recorder()

    async with rec.client(settings) as client:
        await client.status()

    assert rec.logins == []
    assert len(rec.api_calls) == 1


async def test_apikey_read_does_not_waste_a_login(settings: Settings):
    """An export with a working API key should not spend a request logging in."""
    rec = Recorder()

    async with rec.client(settings) as client:
        await client.league()

    assert rec.logins == []
    assert rec.api_calls[0].url.params["APIKEY"] == "TESTAPIKEY"


async def test_login_happens_once_across_many_calls(settings: Settings):
    settings = replace(settings, apikey=None)
    rec = Recorder()

    async with rec.client(settings) as client:
        await client.league()
        await client.league_settings()
        await client.league()

    assert len(rec.logins) == 1, "the session cookie must be reused"


# -- the rule: imports never use APIKEY ---------------------------------


async def test_import_never_sends_apikey_even_when_configured(writable_settings: Settings):
    """APIKEY is export-only. Sending it on an import breaks the write.

    writable_settings carries both an API key and a password, which is the
    realistic post-setup configuration and the one where this rule is easiest
    to get wrong.
    """
    assert writable_settings.apikey, "precondition: this config has an API key"
    rec = Recorder()

    async with rec.client(writable_settings) as client:
        await client.set_lineup(week=4, starters=["0001", "0002"])

    assert len(rec.logins) == 1, "an import must acquire a session cookie first"
    imp = rec.api_calls[0]
    assert imp.url.path.endswith("/import")
    assert "APIKEY" not in imp.url.params, "APIKEY must never be sent on an import"
    assert cookie_of(imp) == "dXNlcjpwYXNzd29yZA=="


# -- failure modes should be legible ------------------------------------


async def test_bad_password_reports_bad_password(settings: Settings):
    settings = replace(settings, apikey=None)
    rec = Recorder(login_body="<error>Invalid username or password</error>")

    async with rec.client(settings) as client:
        with pytest.raises(MFLError, match="rejected the supplied username or password"):
            await client.league()


async def test_login_without_a_cookie_is_reported_clearly(settings: Settings):
    settings = replace(settings, apikey=None)
    rec = Recorder(login_body="<status>ok</status>")

    async with rec.client(settings) as client:
        with pytest.raises(MFLError, match="no MFL_USER_ID cookie"):
            await client.league()


async def test_missing_league_host_degrades_instead_of_failing(settings: Settings):
    """A missing host must not block real work.

    Observed against the live API: MFL returns the league, including its name,
    but omits 'host' from the JSON export. The host is only a performance hint,
    since the api host serves league requests too and redirects.
    """
    from dataclasses import replace

    settings = replace(settings, host=None)
    rec = Recorder(responses={"default": {"league": {"name": "Last Man Standing"}}})

    async with rec.client(settings) as client:
        assert await client.ensure_league_host() == API_HOST
        # And the failure is cached, so we do not re-probe on every request.
        assert client._resolved_host == API_HOST
        # League calls still go somewhere valid.
        await client.league()
        assert rec.api_calls


async def test_league_export_without_a_league_object_degrades(settings: Settings):
    from dataclasses import replace

    settings = replace(settings, host=None)
    rec = Recorder(responses={"default": {"unexpected": True}})

    async with rec.client(settings) as client:
        assert await client.ensure_league_host() == API_HOST


# -- helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ('<status cookie_name="MFL_USER_ID" cookie_value="abc">MFL</status>', "abc"),
        # Attribute order is not a contract.
        ('<status cookie_value="xyz" cookie_name="MFL_USER_ID">MFL</status>', "xyz"),
        ("<status cookie_name='MFL_USER_ID' cookie_value='sq'></status>", "sq"),
        ('<status cookie_value="with-dashes-and_9">MFL</status>', "with-dashes-and_9"),
        ("<status>no cookie here</status>", None),
        ("", None),
    ],
)
def test_extract_cookie(body: str, expected: str | None):
    assert _extract_cookie(body) == expected


@pytest.mark.parametrize(
    "error,fragment",
    [
        ({"$t": "Invalid league ID 80000"}, "Invalid league ID 80000"),
        ({"$t": "a", "e": "b"}, "a; b"),
        ([{"$t": "one"}, {"$t": "two"}], "one; two"),
        ("plain", "plain"),
    ],
)
def test_error_text_unwraps_the_t_key(error: Any, fragment: str):
    """MFL signals failure with HTTP 200, so this is a common error path."""
    assert fragment in _error_text(error)
