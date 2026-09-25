"""Lineup construction and optimisation.

MFL describes lineup requirements as a slot string, e.g.::

    starters = "QB,1,RB,2,WR,3,FLX,1"

which means one QB, two RB, three WR, and one flex. We read the league's own
string rather than assuming a format, so this works for 4-slot leagues,
2-RB leagues, dynasty and IDP variants without changes.

Algorithm: greedy fill of the concrete position requirements in order of
engine value, then flex filled from the best remaining flex-eligible player,
then a bounded pairwise-swap local search to clean up greedy mistakes. The
search is exhaustive over swaps of a single player, which is cheap for roster
sizes in the tens and is guaranteed to be a local optimum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .fantasy_engine import PlayerPool, PlayerValue
from .models import SKILL_POSITIONS, LeagueSettings, Player, RosterPlayer


class LineupError(ValueError):
    """The requested lineup cannot be constructed from the available roster."""


@dataclass
class Starter:
    slot: str
    position: str
    value: PlayerValue

    @property
    def player_id(self) -> str:
        return self.value.player_id

    @property
    def name(self) -> str:
        return self.value.player.name


@dataclass
class Lineup:
    week: int
    starters: list[Starter] = field(default_factory=list)
    bench: list[Starter] = field(default_factory=list)
    projected_total: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def starter_ids(self) -> list[str]:
        return [s.player_id for s in self.starters]

    @property
    def starter_names(self) -> list[str]:
        return [s.name for s in self.starters]

    def summary(self) -> str:
        lines = [f"Week {self.week} projected lineup ({self.projected_total:.1f} pts):"]
        for starter in sorted(self.starters, key=lambda s: s.slot):
            lines.append(
                f"  {starter.slot:<6} {starter.name} "
                f"({starter.value.player.position}) "
                f"{starter.value.projected:.1f} [{starter.value.availability_note}]"
            )
        if self.bench:
            lines.append("Bench: " + ", ".join(s.name for s in self.bench))
        for note in self.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "projected_total": self.projected_total,
            "starters": [
                {
                    "slot": s.slot,
                    "player_id": s.player_id,
                    "name": s.name,
                    "position": s.position,
                    "projected": s.value.projected,
                    "wsis": s.value.wsis,
                    "adjusted": s.value.adjusted,
                    "availability": s.value.availability_note,
                }
                for s in self.starters
            ],
            "bench": [{"player_id": s.player_id, "name": s.name, "position": s.position}
                      for s in self.bench],
            "notes": self.notes,
        }


def slot_plan(league: LeagueSettings) -> list[tuple[str, str]]:
    """The ordered list of ``(slot_label, required_position)`` to fill.

    Flex slots sort last so the greedy pass never spends a flex-eligible
    player that a concrete slot could have used.
    """
    slots = list(league.starter_slots)
    if not slots:
        raise LineupError(
            "League did not report a starter configuration (starters field empty)."
        )
    return sorted(slots, key=lambda pair: (pair[1].upper().startswith("FLX"),))


def optimize_lineup(
    roster: Sequence[RosterPlayer],
    pool: PlayerPool,
    league: LeagueSettings,
    *,
    week: int | None = None,
    respect_injuries: bool = True,
    improve: bool = True,
) -> Lineup:
    """Build the best legal lineup from ``roster``."""
    week = week if week is not None else pool.week
    plan = slot_plan(league)
    flex_positions = _flex_positions(league)

    available: dict[str, RosterPlayer] = {}
    unusable: list[RosterPlayer] = []
    for entry in roster:
        if not entry.is_startable:
            unusable.append(entry)
            continue
        if respect_injuries and entry.player.is_out:
            unusable.append(entry)
            continue
        available[entry.player_id] = entry

    if not available:
        raise LineupError("No startable players on the roster")

    values = {
        pid: (pool.value(pid) or _fallback_value(entry))
        for pid, entry in available.items()
    }

    used: set[str] = set()
    starters: list[Starter] = []

    for slot, position in plan:
        pid = _pick_for_slot(position, values, used, available, flex_positions)
        if pid is None:
            if position.upper().startswith("FLX"):
                notes_hint = "flex"
            else:
                notes_hint = position
            raise LineupError(
                f"No eligible player available for the {notes_hint} slot "
                f"(roster has {_positions(available, used)})"
            )
        used.add(pid)
        starters.append(Starter(slot=slot, position=position, value=values[pid]))

    if improve:
        starters = _improve(starters, values, available, used, flex_positions, league)

    projected = sum(s.value.projected for s in starters)
    bench_ids = [pid for pid in available if pid not in used]
    bench = sorted(
        (
            Starter(slot="BN", position=available[pid].position, value=values[pid])
            for pid in bench_ids
        ),
        key=lambda s: -s.value.projected,
    )

    notes = _notes(roster, starters, unusable, pool)
    return Lineup(
        week=week,
        starters=starters,
        bench=bench,
        projected_total=round(projected, 2),
        notes=notes,
    )


def _flex_positions(league: LeagueSettings) -> frozenset[str]:
    return league.flex_eligible_positions or frozenset(SKILL_POSITIONS)


def _pick_for_slot(
    position: str,
    values: dict[str, PlayerValue],
    used: set[str],
    available: dict[str, RosterPlayer],
    flex_positions: frozenset[str],
) -> str | None:
    """Highest-value unused player who can legally fill ``position``."""
    if position.upper().startswith("FLX"):
        candidates = [
            pid for pid, entry in available.items()
            if pid not in used and entry.position in flex_positions
        ]
    else:
        candidates = [
            pid for pid, entry in available.items()
            if pid not in used and entry.position == position.upper()
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda pid: (values[pid].adjusted, values[pid].projected))


def _improve(
    starters: list[Starter],
    values: dict[str, PlayerValue],
    available: dict[str, RosterPlayer],
    used: set[str],
    flex_positions: frozenset[str],
    league: LeagueSettings,
) -> list[Starter]:
    """Single-swap local search over the greedy solution.

    Only swaps that keep the lineup legal are considered, and we keep the best
    strictly-improving swap each round. This terminates because the objective is
    strictly increasing over a finite set of lineups.
    """
    current = list(starters)
    for _ in range(len(current) * 2):
        best_gain = 0.0
        best_swap: tuple[int, str] | None = None

        for slot_index, starter in enumerate(current):
            for pid, entry in available.items():
                if pid in used:
                    continue
                if not _can_fill(entry.position, starter.slot, flex_positions):
                    continue
                gain = values[pid].adjusted - starter.value.adjusted
                if gain > best_gain + 1e-9:
                    best_gain = gain
                    best_swap = (slot_index, pid)

        if best_swap is None:
            break

        slot_index, pid = best_swap
        outgoing = current[slot_index]
        current[slot_index] = Starter(
            slot=outgoing.slot,
            position=outgoing.position,
            value=values[pid],
        )
        used.discard(outgoing.player_id)
        used.add(pid)
    return current


def _can_fill(position: str, slot: str, flex_positions: frozenset[str]) -> bool:
    if slot.upper().startswith("FLX"):
        return position in flex_positions
    return position == slot.upper()


def _positions(available: dict[str, RosterPlayer], used: set[str]) -> str:
    counts: dict[str, int] = {}
    for pid, entry in available.items():
        if pid not in used:
            counts[entry.position] = counts.get(entry.position, 0) + 1
    return ", ".join(f"{pos}:{n}" for pos, n in sorted(counts.items())) or "an empty bench"


def _fallback_value(entry: RosterPlayer) -> PlayerValue:
    return PlayerValue(
        player=entry.player,
        projected=0.0,
        wsis=50.0,
        available=not entry.player.is_out,
    )


def _notes(
    roster: Sequence[RosterPlayer],
    starters: list[Starter],
    unusable: Sequence[RosterPlayer],
    pool: PlayerPool,
) -> list[str]:
    notes: list[str] = []
    out_starts = [s for s in starters if s.value.player.is_out]
    if out_starts:
        notes.append(
            "No healthy alternative available for: "
            + ", ".join(s.name for s in out_starts)
        )
    questionable = [s for s in starters if s.value.player.is_questionable]
    if questionable:
        notes.append(
            "Questionable but still projected to start: "
            + ", ".join(s.name for s in questionable)
        )
    byes = [s for s in starters if s.value.player.bye_week == pool.week]
    if byes:
        notes.append(
            "Projected on bye week "
            + ", ".join(s.name for s in byes)
            + "; check the bye schedule"
        )
    return notes


def compare_lineups(
    a: Lineup,
    b: Lineup,
) -> dict[str, object]:
    """Diff two lineups, used to decide whether a change is worth submitting."""
    a_ids = set(a.starter_ids)
    b_ids = set(b.starter_ids)
    added = [s for s in b.starters if s.player_id not in a_ids]
    removed = [s for s in a.starters if s.player_id not in b_ids]
    return {
        "delta_points": round(b.projected_total - a.projected_total, 2),
        "added": [s.name for s in added],
        "removed": [s.name for s in removed],
        "changed": bool(added or removed),
    }
