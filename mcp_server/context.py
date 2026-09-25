"""One call to fetch everything the decision logic needs.

Building a pool requires the player database, projections, consensus and injury
data, and those requests are not free. This module fetches them once, in the
right order, and hands the rest of the codebase a ready-to-use snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from .config import Settings
from .fantasy_engine import PlayerPool, build_pool
from .injuries import AvailabilityReport, build_report
from .lineup import Lineup, optimize_lineup
from .mfl_api import MFLError, MFLClient
from .models import LeagueSettings, RosterPlayer, player_id as norm_player_id

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """A complete read of the league, cached for the duration of a run."""

    settings: Settings
    week: int
    league: LeagueSettings
    roster: list[RosterPlayer]
    pool: PlayerPool
    injuries: AvailabilityReport
    free_agent_ids: list[str] = field(default_factory=list)
    status: dict = field(default_factory=dict)
    generated_at: datetime | None = None

    def now_local(self) -> datetime:
        tz = ZoneInfo(self.settings.league_timezone)
        return datetime.now(tz)

    def current_lineup(self) -> Lineup:
        """The lineup MFL has recorded for me right now."""
        starters = [r for r in self.roster if r.status == "S"]
        if not starters:
            # No lineup submitted yet; fall back to the best possible one.
            return optimize_lineup(self.roster, self.pool, self.league, week=self.week)
        return _lineup_from_status(starters, self)

    def best_lineup(self) -> Lineup:
        return optimize_lineup(self.roster, self.pool, self.league, week=self.week)

    def free_agent_values(self, limit: int = 60) -> list:
        """Top available free agents by projected value, for waiver review."""
        from .fantasy_engine import PlayerValue

        out: list[PlayerValue] = []
        for pid in self.free_agent_ids[:limit]:
            value = self.pool.value(pid)
            if value is not None and value.available:
                out.append(value)
        return sorted(out, key=lambda v: -v.adjusted)


def _lineup_from_status(starters: Sequence[RosterPlayer], snap: Snapshot) -> Lineup:
    from .lineup import Starter

    plan = {pos.upper(): i + 1 for i, (_l, pos) in enumerate(snap.league.starter_slots)}
    entries: list[Starter] = []
    for entry in starters:
        value = snap.pool.value(entry.player_id)
        if value is None:
            continue
        slot_index = plan.get(entry.position.upper(), len(entries) + 1)
        entries.append(
            Starter(slot=f"SLOT{slot_index}", position=entry.position.upper(), value=value)
        )
    used = {e.player_id for e in entries}
    bench = [
        Starter(
            slot="BN",
            position=r.position,
            value=snap.pool.value(r.player_id),
        )
        for r in snap.roster
        if r.player_id not in used and snap.pool.value(r.player_id) is not None
    ]
    return Lineup(
        week=snap.week,
        starters=entries,
        bench=bench,
        projected_total=round(sum(e.value.projected for e in entries), 2),
    )


async def load_snapshot(
    client: MFLClient,
    *,
    week: int | None = None,
    free_agent_limit: int = 200,
) -> Snapshot:
    """Fetch league config, roster, projections, consensus, injuries and FAs."""
    settings = client.settings
    status = await client.status()
    target_week = week or status.get("lineup_week") or status.get("current_week") or 1
    log.info("Building snapshot for week %s of season %s", target_week, status.get("year"))

    league = await client.league_settings()
    roster = await client.my_roster()
    roster_ids = [r.player_id for r in roster]

    players = await client.players()
    projected = await _safe(client.projected_scores(week=target_week, players=roster_ids))
    wsis = await _safe(client.who_should_i_start(week=target_week))
    injuries_raw = await _safe(client.injuries(target_week))
    bye_map = await _safe(client.bye_weeks(target_week))
    free_agents = await _safe(client.free_agents(), default=[])

    pool = build_pool(players, week=target_week, projected=projected, wsis=wsis)
    report = build_report(roster, injuries_raw, week=target_week, bye_map=bye_map)

    return Snapshot(
        settings=settings,
        week=target_week,
        league=league,
        roster=roster,
        pool=pool,
        injuries=report,
        free_agent_ids=list(free_agents)[:free_agent_limit],
        status=status,
        generated_at=snap_now(settings),
    )


def snap_now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.league_timezone))


async def _safe(coro, default=None):
    """Run a feed, degrading to a default rather than failing the whole snapshot.

    Injuries, consensus and bye data are enrichments. Losing one should
    degrade the recommendation, not break the run.
    """
    try:
        return await coro
    except MFLError as exc:
        log.warning("Optional feed unavailable: %s", exc)
        return {} if default is None else default
