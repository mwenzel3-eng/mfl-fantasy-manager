"""Tests for the MCP server surface.

These do not talk to MFL. They pin the tool surface and, more usefully, check
that a write tool refuses to write when the safety switches are off - the
property that matters most and is easiest to regress.
"""

from __future__ import annotations

import importlib
from dataclasses import replace

import httpx
import pytest

from mcp_server.config import Settings
from mcp_server.errors import WritesDisabledError

from .conftest import apply_to_env

READ_TOOLS = {
    "league_status",
    "my_roster",
    "injury_report",
    "recommend_lineup",
    "recommend_waivers",
    "notify",
}
WRITE_TOOLS = {
    "apply_lineup",
    "apply_waiver_move",
    "submit_waiver_claims",
}


@pytest.fixture(scope="module")
def server_module():
    return importlib.import_module("mcp_server.server")


def _tool_names(module) -> set[str]:
    return {tool.name for tool in module.mcp._tool_manager.list_tools()}


def test_all_expected_tools_are_registered(server_module):
    assert _tool_names(server_module) == READ_TOOLS | WRITE_TOOLS


def test_every_tool_has_a_description(server_module):
    for tool in server_module.mcp._tool_manager.list_tools():
        assert tool.description, f"{tool.name} has no description"


def test_read_and_write_tools_are_disjoint(server_module):
    assert not (READ_TOOLS & WRITE_TOOLS)


def test_write_state_is_reported_for_read_tools(server_module, settings: Settings):
    """Read tools surface whether writes are on, so a client cannot guess."""
    state = server_module._write_state(settings)
    assert state["writes_enabled"] is False
    assert state["dry_run"] is True
    assert "ENABLE_WRITES" in state["blocker"]


def test_write_state_clear_when_fully_enabled(server_module, writable_settings: Settings):
    state = server_module._write_state(writable_settings)
    assert state["writes_enabled"] is True
    assert state["blocker"] is None
    # Confirmation is a per-call concern, reported separately.
    assert "confirmed=True" in state["note"]


# -- write tools refuse without the switches -----------------------------


async def test_apply_waiver_move_refuses_when_disabled(
    server_module, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    apply_to_env(monkeypatch, settings)
    with pytest.raises(WritesDisabledError):
        await server_module.apply_waiver_move("0020", "0031", confirmed=True)


async def test_apply_waiver_move_refuses_without_confirmation(
    server_module, writable_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    apply_to_env(monkeypatch, writable_settings)
    with pytest.raises(WritesDisabledError, match="confirmed"):
        await server_module.apply_waiver_move("0020", "0031", confirmed=False)


async def test_submit_waiver_claims_refuses_when_disabled(
    server_module, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    apply_to_env(monkeypatch, settings)
    with pytest.raises(WritesDisabledError):
        await server_module.submit_waiver_claims(confirmed=True)


async def test_apply_waiver_move_never_reaches_the_network_when_refused(
    server_module, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    """The guard must run before any HTTP call is attempted."""
    apply_to_env(monkeypatch, settings)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    original = importlib.import_module("mcp_server.server").MFLClient

    def counting_client(_settings, **kwargs):
        kwargs["client"] = http
        return original(_settings, **kwargs)

    server_module.MFLClient = counting_client
    try:
        with pytest.raises(WritesDisabledError):
            await server_module.apply_waiver_move("0020", "0031", confirmed=True)
    finally:
        server_module.MFLClient = original
        await http.aclose()
    assert calls == 0


def test_server_module_exposes_main_entrypoint(server_module):
    assert callable(server_module.main)
