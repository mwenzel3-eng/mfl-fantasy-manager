"""Guards on the client identity sent to MFL.

MFL grants registered clients roughly 2.5x the request limit, but only when the
User-Agent registered with them matches what the client actually sends. That
makes the User-Agent a frozen contract: any edit silently downgrades the client
to unregistered limits, and nothing errors. These tests make that failure mode
loud at CI time instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path



from mcp_server import __version__
from mcp_server.config import (
    CLIENT_NAME,
    CLIENT_URL,
    DEFAULT_USER_AGENT,
    Settings,
)

from .conftest import load

REPO_ROOT = Path(__file__).resolve().parent.parent
# A version looks like 1.2.3, 0.1.0, or a date-ish 2026.9.1.
_VERSION_LIKE = re.compile(r"\d+\.\d+")


def test_user_agent_has_no_version():
    """The whole point: a version here would break MFL client registration."""
    assert not _VERSION_LIKE.search(DEFAULT_USER_AGENT), (
        f"User-Agent {DEFAULT_USER_AGENT!r} contains a version-like string. "
        "MFL matches the registered User-Agent exactly, so bumping the version "
        "here silently downgrades the client to unregistered rate limits. "
        "Report the version in logs and the status payload instead."
    )


def test_user_agent_identifies_the_project():
    assert CLIENT_NAME in DEFAULT_USER_AGENT
    assert CLIENT_URL in DEFAULT_USER_AGENT


# -- the header must be sent regardless of how the client was built --------


async def test_user_agent_is_sent_with_an_injected_client(settings: Settings):
    """A caller-supplied httpx client must not lose the User-Agent.

    The header used to be set only when MFLClient built its own client, so any
    embedder passing a pre-built one sent a bare ``python-httpx/x.y`` and
    silently forfeited the registered rate limit.
    """
    import httpx

    from mcp_server.mfl_api import MFLClient

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, json=load("mfl_status.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        await client.status()

    assert seen == [DEFAULT_USER_AGENT]
    assert not any(ua and "python-httpx" in ua for ua in seen)


async def test_user_agent_is_set_on_a_client_we_build(settings: Settings):
    """The self-built client carries the header at construction time too."""
    from mcp_server.mfl_api import MFLClient

    client = MFLClient(settings)
    try:
        assert client._http().headers["User-Agent"] == DEFAULT_USER_AGENT
    finally:
        await client.aclose()


async def test_custom_user_agent_reaches_the_wire(writable_settings: Settings):
    """A user who registers their own string must have it actually sent."""
    from dataclasses import replace

    import httpx

    from mcp_server.mfl_api import MFLClient

    custom = "lukes-mfl-bot (+https://example.invalid)"
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        if request.url.path.endswith("/login"):
            return httpx.Response(
                200, text='<status cookie_name="MFL_USER_ID" cookie_value="C">MFL</status>'
            )
        return httpx.Response(200, json=load("mfl_status.json"))

    settings = replace(writable_settings, user_agent=custom)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        await client.status()
        await client._send("export", {"TYPE": "league"})

    # Both the public GET and the authenticated export carry it.
    assert set(seen) == {custom}


def test_user_agent_override_wins(settings: Settings):
    """A user who registers a different string must be able to match it."""
    from dataclasses import replace

    custom = replace(settings, user_agent="my-own-client/1.0")
    assert custom.user_agent == "my-own-client/1.0"


def test_package_version_matches_pyproject():
    """Keep the reported version honest without putting it in the User-Agent."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


# -- the status payload reports identity without affecting registration ----


async def test_status_reports_client_identity(settings: Settings):
    import httpx

    from mcp_server.mfl_api import MFLClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("mfl_status.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(settings, client=http) as client:
        status = await client.status()

    assert status["client"]["name"] == CLIENT_NAME
    assert status["client"]["version"] == __version__
    assert status["client"]["user_agent"] == DEFAULT_USER_AGENT
    # The default string is the one we intend people to register.
    assert status["client"]["registered"] is True


async def test_status_flags_a_custom_user_agent_as_unregistered(settings: Settings):
    import httpx
    from dataclasses import replace

    from mcp_server.mfl_api import MFLClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("mfl_status.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with MFLClient(replace(settings, user_agent="something-else"), client=http) as client:
        status = await client.status()

    assert status["client"]["registered"] is False
    assert status["client"]["user_agent"] == "something-else"
