"""Tests for the write guard and the lineup submission threshold."""

from __future__ import annotations

from dataclasses import replace

from mcp_server.config import Settings
from mcp_server.safety import (
    MIN_LINEUP_GAIN,
    check_write,
    log_writes_enabled,
    require_write,
    should_submit_lineup,
)
from mcp_server.mfl_api import WritesDisabledError


def test_default_settings_block_writes(settings: Settings):
    guard = check_write(settings, confirmed=True)
    assert guard.allowed is False
    assert "ENABLE_WRITES" in guard.reason


def test_dry_run_alone_blocks_writes(writable_settings: Settings):
    settings = replace(writable_settings, dry_run=True)
    guard = check_write(settings, confirmed=True)
    assert guard.allowed is False
    assert "DRY_RUN" in guard.reason


def test_confirmation_required(writable_settings: Settings):
    assert check_write(writable_settings, confirmed=False).allowed is False
    assert check_write(writable_settings, confirmed=True).allowed is True


def test_apikey_alone_cannot_write(writable_settings: Settings):
    """MFL documents that APIKEY is export-only, so writes need a password."""
    settings = replace(writable_settings, password=None)
    assert settings.has_apikey_auth is True
    guard = check_write(settings, confirmed=True)
    assert guard.allowed is False
    assert "MFL_USERNAME" in guard.reason


def test_require_write_raises(settings: Settings):
    try:
        require_write(settings, confirmed=True)
    except WritesDisabledError as exc:
        assert "ENABLE_WRITES" in str(exc)
    else:
        raise AssertionError("expected WritesDisabledError")


def test_require_write_passes_when_fully_enabled(writable_settings: Settings):
    require_write(writable_settings, confirmed=True)  # must not raise


def test_writes_allowed_property(writable_settings: Settings):
    assert writable_settings.writes_allowed is True
    assert replace(writable_settings, dry_run=True).writes_allowed is False


# -- lineup submission threshold ---------------------------------------


def test_identical_lineups_are_not_submitted(writable_settings: Settings):
    ids = ["0001", "0002", "0003"]
    submit, why = should_submit_lineup(
        writable_settings,
        current_ids=ids,
        proposed_ids=list(reversed(ids)),
        current_total=10.0,
        proposed_total=10.0,
    )
    assert submit is False
    assert "already matches" in why


def test_tiny_gain_is_rejected(writable_settings: Settings):
    submit, why = should_submit_lineup(
        writable_settings,
        current_ids=["0001"],
        proposed_ids=["0002"],
        current_total=10.0,
        proposed_total=10.0 + MIN_LINEUP_GAIN / 2,
    )
    assert submit is False
    assert "below" in why


def test_real_gain_is_accepted(writable_settings: Settings):
    submit, why = should_submit_lineup(
        writable_settings,
        current_ids=["0001"],
        proposed_ids=["0002"],
        current_total=10.0,
        proposed_total=18.0,
    )
    assert submit is True
    assert "8.00" in why


def test_log_writes_enabled_does_not_raise(writable_settings: Settings, caplog):
    log_writes_enabled(writable_settings)
    log_writes_enabled(replace(writable_settings, dry_run=True))
