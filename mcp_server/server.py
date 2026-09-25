"""MCP server exposing the fantasy manager as tools.

Every tool is read-only except the three that end in ``_apply``, and those
require both the environment write switch and an explicit ``confirmed=True``
argument. See :mod:`mcp_server.safety`.

Run with::

    python -m mcp_server.server
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from . import __version__
from .config import Settings, get_settings
from .context import Snapshot, load_snapshot
from .fantasy_engine import explain
from .injuries import AvailabilityReport
from .lineup import Lineup, LineupError, compare_lineups
from .mfl_api import MFLError, MFLClient
from .models import Player, RosterPlayer
from .notify import build_notifier
from .safety import check_write, log_writes_enabled, require_write, should_submit_lineup
from .waivers import WaiverMove, claims_to_pic, recommend_claims, recommend_moves

logging.basicConfig(
    level=os.environ.get("MFL_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("mcp_server")

try:
    # MCP Python SDK 2.x. In 1.x this class was called ``FastMCP``.
    from mcp.server import MCPServer
except ModuleNotFoundError as _exc:  # pragma: no cover
    raise SystemExit(
        "The 'mcp' package is required to run the server: pip install -r requirements.txt"
    ) from _exc

mcp = MCPServer("mfl-fantasy-manager")


# ---------------------------------------------------------------- helpers


async def _snapshot(week: int | None = None) -> tuple[MFLClient, Snapshot]:
    client = MFLClient()
    try:
        return client, await load_snapshot(client, week=week)
    except Exception:
        await client.aclose()
        raise


def _roster_dict(entry: RosterPlayer) -> dict[str, Any]:
    return {
        "player_id": entry.player_id,
        "name": entry.player.name,
        "position": entry.position,
        "team": entry.player.team,
        "status": entry.status,
        "salary": entry.salary or None,
        "injury_status": entry.player.injury_status or None,
        "injury_detail": entry.player.injury_detail or None,
        "bye_week": entry.player.bye_week,
    }


def _write_state(settings: Settings) -> dict[str, Any]:
    """Report whether the *environment* permits writes.

    Deliberately evaluated with ``confirmed=True``: the per-call confirmation is
    a property of the caller's intent, not of the configuration, and reporting
    it here as a "blocker" would be misleading to a read-only client.
    """
    guard = check_write(settings, confirmed=True)
    return {
        "writes_enabled": settings.writes_allowed,
        "dry_run": settings.dry_run,
        "blocker": None if guard.allowed else guard.reason,
        "note": "Write tools also require confirmed=True on the call itself.",
    }


# ---------------------------------------------------------------- read tools


@mcp.tool()
async def league_status() -> dict[str, Any]:
    """Current MFL season state: week numbers, league name and lineup rules."""
    client = MFLClient()
    try:
        status = await client.status()
        league = await client.league_settings()
        return {
            "season": status,
            "league": {
                "name": league.name,
                "host": league.host,
                "starters": list(league.starter_slots),
                "roster_limits": league.roster_limits,
                "ir_slots": league.ir_slots,
                "taxi_slots": league.taxi_slots,
                "franchise_count": league.franchise_count,
            },
            "writes": _write_state(client.settings),
        }
    finally:
        await client.aclose()


@mcp.tool()
async def my_roster(week: int | None = None) -> dict[str, Any]:
    """My current roster with injury status, projections and weekly value."""
    client, snap = await _snapshot(week)
    try:
        entries = sorted(
            snap.roster, key=lambda r: (r.is_ir, r.is_taxi, -_value(snap, r.player_id))
        )
        return {
            "week": snap.week,
            "roster": [
                {**_roster_dict(e), **_value_dict(snap, e.player_id)} for e in entries
            ],
            "writes": _write_state(client.settings),
        }
    finally:
        await client.aclose()


@mcp.tool()
async def injury_report(week: int | None = None) -> dict[str, Any]:
    """Injury and bye-week report for my roster, with suggested IR moves."""
    _client, snap = await _snapshot(week)
    report: AvailabilityReport = snap.injuries
    return {"week": snap.week, **report.as_dict(), "summary": report.summary()}


@mcp.tool()
async def recommend_lineup(week: int | None = None) -> dict[str, Any]:
    """Optimised starting lineup versus what is currently submitted."""
    _client, snap = await _snapshot(week)
    try:
        current = snap.current_lineup()
        proposed = snap.best_lineup()
    except LineupError as exc:
        return {"week": snap.week, "error": str(exc)}
    return {
        "week": snap.week,
        "current": {"projected": current.projected_total, "starters": current.starter_names},
        "proposed": proposed.as_dict(),
        "proposed_summary": proposed.summary(),
        "comparison": compare_lineups(current, proposed),
    }


@mcp.tool()
async def recommend_waivers(week: int | None = None, limit: int = 10) -> dict[str, Any]:
    """Ranked free-agent add/drop moves and waiver claims for this week."""
    _client, snap = await _snapshot(week)
    league = snap.league
    candidates = snap.free_agent_values(limit=80)

    moves: list[WaiverMove] = recommend_moves(
        snap.roster, candidates, snap.pool, league, limit=limit
    )
    claims = recommend_claims(snap.roster, candidates, snap.pool, league, limit=5)

    return {
        "week": snap.week,
        "moves": [m.as_dict() for m in moves],
        "claims": [
            {
                "add": c.add.as_dict(),
                "drop": {"player_id": d.player_id, "name": d.player.name},
                "why": why,
            }
            for c, d, why in claims
        ],
        "claim_pic_format": claims_to_pic(claims),
        "writes": _write_state(_client_settings(snap)),
    }


@mcp.tool()
async def notify(message: str) -> dict[str, Any]:
    """Send a short notification through the configured SMS provider."""
    settings = get_settings()
    notifier = build_notifier(settings)
    result = await notifier.send(message)
    return {"provider": settings.sms_provider, "result": result}


# ---------------------------------------------------------------- write tools


@mcp.tool()
async def apply_lineup(
    week: int | None = None,
    *,
    confirmed: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Submit the optimised lineup for a week.

    Requires ``MFL_ENABLE_WRITES=1``, ``MFL_DRY_RUN=0`` and ``confirmed=True``.
    Refuses to submit when the projected gain is under the safety threshold,
    unless ``force`` is set.
    """
    settings = get_settings()
    client, snap = await _snapshot(week)
    try:
        try:
            current = snap.current_lineup()
            proposed = snap.best_lineup()
        except LineupError as exc:
            return {"ok": False, "reason": str(exc)}

        submit, why = should_submit_lineup(
            settings,
            current_ids=current.starter_ids,
            proposed_ids=proposed.starter_ids,
            current_total=current.projected_total,
            proposed_total=proposed.projected_total,
        )
        if not submit and not force:
            return {
                "ok": False,
                "reason": why,
                "hint": "Pass force=True to submit anyway.",
                "proposed": proposed.as_dict(),
            }

        require_write(settings, confirmed=confirmed)
        payload = await client.set_lineup(
            snap.week,
            proposed.starter_ids,
            comments="Set by mfl-fantasy-manager",
        )
        log.info("Submitted lineup for week %s (%s)", snap.week, proposed.starter_names)
        return {
            "ok": True,
            "week": snap.week,
            "starters": proposed.starter_names,
            "projected": proposed.projected_total,
            "mfl_response": payload,
        }
    except MFLError as exc:
        return {"ok": False, "reason": str(exc)}
    finally:
        await client.aclose()


@mcp.tool()
async def apply_waiver_move(
    add_player_id: str,
    drop_player_id: str,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Execute an immediate free-agent add and drop.

    Uses MFL's ``fcfsWaiver`` import. Requires the same write switches as
    :func:`apply_lineup` plus ``confirmed=True``.
    """
    settings = get_settings()
    require_write(settings, confirmed=confirmed)
    client = MFLClient()
    try:
        payload = await client.fcfs_move(add=add_player_id, drop=[drop_player_id])
        log.info("Waiver move applied: +%s -%s", add_player_id, drop_player_id)
        return {
            "ok": True,
            "added": add_player_id,
            "dropped": drop_player_id,
            "mfl_response": payload,
        }
    except MFLError as exc:
        return {"ok": False, "reason": str(exc)}
    finally:
        await client.aclose()


@mcp.tool()
async def submit_waiver_claims(
    round_number: int | None = None,
    limit: int = 5,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Submit a ranked round of waiver claims via MFL's ``waiverRequest`` import."""
    settings = get_settings()
    require_write(settings, confirmed=confirmed)
    client, snap = await _snapshot(None)
    try:
        claims = recommend_claims(
            snap.roster,
            snap.free_agent_values(limit=80),
            snap.pool,
            snap.league,
            limit=limit,
        )
        picks = claims_to_pic(claims)
        if not picks:
            return {"ok": False, "reason": "No waiver claims cleared the value threshold."}
        payload = await client.submit_waiver_round(picks, round_number=round_number)
        return {
            "ok": True,
            "claims": [f"{add}_{drop}" for add, drop in picks],
            "mfl_response": payload,
        }
    except MFLError as exc:
        return {"ok": False, "reason": str(exc)}
    finally:
        await client.aclose()


# ---------------------------------------------------------------- internals


def _value(snap: Snapshot, player_id: str) -> float:
    value = snap.pool.value(player_id)
    return value.adjusted if value else 0.0


def _value_dict(snap: Snapshot, player_id: str) -> dict[str, Any]:
    value = snap.pool.value(player_id)
    if value is None:
        return {"projected": None, "wsis": None, "adjusted": None, "rationale": "no data"}
    return {
        "projected": value.projected,
        "wsis": value.wsis,
        "adjusted": value.adjusted,
        "rationale": explain(value),
    }


def _client_settings(snap: Snapshot) -> Settings:
    return snap.settings


def main() -> None:
    settings = get_settings()
    log_writes_enabled(settings)
    log.info("Starting mfl-fantasy-manager MCP server v%s", __version__)
    mcp.run()


if __name__ == "__main__":
    main()
