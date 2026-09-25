"""Command line interface.

One entry point for everything, used by the GitHub Actions workflows and handy
for running things by hand::

    python -m mcp_server.cli status
    python -m mcp_server.cli waivers
    python -m mcp_server.cli lineup --week 4
    python -m mcp_server.cli injuries
    python -m mcp_server.cli roster

Safety: every subcommand is read-only unless the environment already permits
writes (``MFL_ENABLE_WRITES=1`` and ``MFL_DRY_RUN=0``). ``--execute`` does not
override that; it only exists so the flag combination is stated explicitly at
the call site.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import replace

from .config import ConfigError, get_settings
from .context import load_snapshot
from .mfl_api import MFLError, MFLClient
from .safety import log_writes_enabled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mfl",
        description="MyFantasyLeague fantasy manager (read-only by default)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="season week numbers and league configuration")

    roster = sub.add_parser("roster", help="your roster with projections and injuries")
    roster.add_argument("--week", type=int, default=None)

    waivers = sub.add_parser("waivers", help="ranked free-agent add/drop recommendations")
    waivers.add_argument("--week", type=int, default=None)
    waivers.add_argument("--limit", type=int, default=10)

    lineup = sub.add_parser("lineup", help="optimised lineup, and optionally submit it")
    lineup.add_argument("--week", type=int, default=None)
    lineup.add_argument(
        "--force",
        action="store_true",
        help="submit even if the projected gain is below the safety threshold",
    )

    injuries = sub.add_parser("injuries", help="injury and bye-week report")
    injuries.add_argument("--week", type=int, default=None)

    for name in ("roster", "waivers", "lineup", "injuries"):
        sub.choices[name].add_argument(
            "--execute",
            action="store_true",
            help="allow writes for this run (still requires MFL_ENABLE_WRITES=1 "
                 "and MFL_DRY_RUN=0 in the environment)",
        )

    return parser


async def cmd_status(args, settings) -> tuple[dict, int]:
    """Season status plus league config.

    The season half of this is public, so a missing or wrong credential yields
    a partial answer rather than a hard failure. That makes ``status`` a useful
    first command when you are still setting things up.
    """
    payload: dict = {"writes": {"enabled": settings.writes_allowed, "dry_run": settings.dry_run}}
    async with MFLClient(settings) as client:
        payload["season"] = await client.status()
        try:
            league = await client.league_settings()
        except (MFLError, ConfigError) as exc:
            payload["league"] = None
            payload["league_error"] = str(exc)
        else:
            payload["league"] = {
                "name": league.name,
                "host": league.host,
                "starters": [list(s) for s in league.starter_slots],
                "roster_limits": league.roster_limits,
                "ir_slots": league.ir_slots,
                "taxi_slots": league.taxi_slots,
            }
    return payload, 0


async def cmd_roster(args, settings) -> tuple[dict, int]:
    async with MFLClient(settings) as client:
        snap = await load_snapshot(client, week=args.week)
    rows = []
    for entry in snap.roster:
        value = snap.pool.value(entry.player_id)
        rows.append(
            {
                "player_id": entry.player_id,
                "name": entry.player.name,
                "position": entry.position,
                "status": entry.status,
                "injury_status": entry.player.injury_status or None,
                "projected": value.projected if value else None,
                "adjusted": value.adjusted if value else None,
                "bye_week": entry.player.bye_week,
            }
        )
    rows.sort(key=lambda r: (r["status"] in {"IR", "TS"}, -(r["adjusted"] or 0.0)))
    return {"week": snap.week, "rows": rows}, 0


async def cmd_waivers(args, settings) -> tuple[dict, int]:
    from jobs.wednesday_waivers import run

    result = await run(week=args.week)
    return {"lines": result.lines, "changed": result.changed}, result.exit_code()


async def cmd_lineup(args, settings) -> tuple[dict, int]:
    from jobs.thursday_lineup import run

    result = await run(week=args.week, force=args.force)
    return {"lines": result.lines, "changed": result.changed}, result.exit_code()


async def cmd_injuries(args, settings) -> tuple[dict, int]:
    from jobs.sunday_injury_check import run

    result = await run(week=args.week)
    return {"lines": result.lines, "changed": result.changed}, result.exit_code()


COMMANDS = {
    "status": cmd_status,
    "roster": cmd_roster,
    "waivers": cmd_waivers,
    "lineup": cmd_lineup,
    "injuries": cmd_injuries,
}


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.log_level:
        logging.getLogger().setLevel(args.log_level.upper())

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # --execute is a statement of intent, never a bypass. It cannot enable
    # writes that the environment has not already permitted.
    if getattr(args, "execute", False) and not settings.writes_allowed:
        print(
            "--execute requested but writes are not enabled. Set MFL_ENABLE_WRITES=1 "
            "and MFL_DRY_RUN=0, and supply MFL_USERNAME/MFL_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    log_writes_enabled(settings)
    handler = COMMANDS[args.command]
    try:
        payload, code = await handler(args, settings)
    except (MFLError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        payload = dict(payload)
        lines = payload.pop("lines", None)
        if lines is not None:
            for line in lines:
                print(line)
        else:
            print(json.dumps(payload, indent=2, default=str))
    return code


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
