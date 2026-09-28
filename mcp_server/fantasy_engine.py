"""Scoring and ranking: turn raw MFL feeds into a single decision signal.

The engine is deliberately simple and explainable. For each player we build a
``PlayerValue`` that combines:

1. **Projected points** from MFL's ``projectedScores`` export, already scored
   with *your* league's scoring system. This is the dominant term.
2. **Who Should I Start** win percentage, as a modest tie-breaker among
   players with similar projections.
3. **Availability adjustments** for injury designations and bye weeks.
4. **Positional depth** for waiver moves: the same RB is worth less to a team
   that already has three starting-quality RBs.

Nothing here touches the network, so it is straightforward to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .models import (
    OUT_STATUSES,
    QUESTIONABLE_STATUSES,
    SKILL_POSITIONS,
    LeagueSettings,
    Player,
    RosterPlayer,
    as_float,
)

# Weights. Projection dominates; the rest break ties and encode availability.
PROJECTION_WEIGHT = 1.0
WSIS_WEIGHT = 0.06
QUESTIONABLE_PENALTY = 0.35
OUT_PENALTY = 0.85
DEPTH_PENALTY = 0.10
DEEP_STASH_DISCOUNT = 0.45
IR_DISCOUNT = 0.60


@dataclass(frozen=True, slots=True)
class PlayerValue:
    """The engine's view of one player for one week."""

    player: Player
    projected: float
    wsis: float
    available: bool
    note: str = ""
    projection_rank: int = 0
    # False when ``wsis`` is the 50.0 neutral default rather than a figure MFL
    # supplied, so reports can say "no consensus data" instead of printing a
    # confident-looking 50.
    has_consensus: bool = False

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def adjusted(self) -> float:
        """Projection, discounted by availability and nudged by consensus."""
        base = self.projected * PROJECTION_WEIGHT
        base += (self.wsis - 50.0) * WSIS_WEIGHT
        if self.player.injury_status in QUESTIONABLE_STATUSES:
            base *= 1.0 - QUESTIONABLE_PENALTY
        elif self.player.injury_status in OUT_STATUSES:
            base *= 1.0 - OUT_PENALTY
        return round(base, 3)

    @property
    def availability_note(self) -> str:
        if self.player.injury_status in OUT_STATUSES:
            return f"{self.player.injury_status} ({self.player.injury_detail or 'no detail'})"
        if self.player.injury_status in QUESTIONABLE_STATUSES:
            return f"Questionable: {self.player.injury_status}"
        if self.player.bye_week is not None:
            return f"Bye week {self.player.bye_week}"
        return "Available"

    def as_dict(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "name": self.player.name,
            "position": self.player.position,
            "team": self.player.team,
            "projected": self.projected,
            "wsis": self.wsis,
            "adjusted": self.adjusted,
            "available": self.available,
            "availability": self.availability_note,
            "note": self.note,
        }


@dataclass
class PlayerPool:
    """Everything the engine knows about a set of players for a given week."""

    week: int
    players: dict[str, Player] = field(default_factory=dict)
    projected: dict[str, float] = field(default_factory=dict)
    wsis: dict[str, float] = field(default_factory=dict)
    values: dict[str, PlayerValue] = field(default_factory=dict)

    def value(self, player_id: str) -> PlayerValue | None:
        return self.values.get(player_id)

    def ranked(self, *, startable_only: bool = False) -> list[PlayerValue]:
        items = list(self.values.values())
        if startable_only:
            items = [v for v in items if v.available]
        return sorted(items, key=lambda v: (-v.adjusted, v.player.name))

    def by_position(self, position: str) -> list[PlayerValue]:
        return [v for v in self.ranked() if v.player.position == position.upper()]

    def rank_of(self, player_id: str) -> int:
        """Rank by raw projection (1 = most projected), independent of health."""
        value = self.values.get(player_id)
        return value.projection_rank if value else 0

    def top_by_position(self, position: str, limit: int = 25) -> list[PlayerValue]:
        return self.by_position(position)[:limit]


def build_pool(
    players: Iterable[Player],
    *,
    week: int,
    projected: Mapping[str, float] | None = None,
    wsis: Mapping[str, float] | None = None,
) -> PlayerPool:
    """Combine player metadata, projections and consensus into a ranked pool."""
    projected = {k: as_float(v) for k, v in (projected or {}).items()}
    wsis = {k: as_float(v, 50.0) for k, v in (wsis or {}).items()}

    pool = PlayerPool(week=week, players={p.player_id: p for p in players})
    pool.projected = projected
    pool.wsis = wsis

    provisional = {
        pid: PlayerValue(
            player=p,
            projected=projected.get(pid, 0.0),
            wsis=wsis.get(pid, 50.0),
            available=p.injury_status not in OUT_STATUSES,
            has_consensus=pid in wsis,
        )
        for pid, p in pool.players.items()
    }

    order = sorted(provisional.values(), key=lambda v: (-v.projected, v.player.name))
    ranks = {v.player_id: i for i, v in enumerate(order, start=1)}

    pool.values = {
        pid: PlayerValue(
            player=v.player,
            projected=v.projected,
            wsis=v.wsis,
            available=v.available,
            projection_rank=ranks.get(pid, 0),
            has_consensus=v.has_consensus,
        )
        for pid, v in provisional.items()
    }
    return pool


def depth_adjusted_value(
    candidate: PlayerValue,
    roster: Sequence[RosterPlayer],
    *,
    league: LeagueSettings | None = None,
) -> float:
    """Value a free agent *to this specific roster*, accounting for depth.

    A borderline WR is worth more when you have one starter-quality WR and
    less when you already have three.
    """
    base = candidate.adjusted
    position = candidate.player.position
    incumbents = [r for r in roster if r.position == position and r.is_startable]

    surplus = len(incumbents) - 1
    if surplus <= 0:
        return round(base, 3)

    counts = league.starter_counts() if league else {}
    needed = counts.get(position, 1)
    # Already at or above the number of starting slots at this position.
    if len(incumbents) >= max(needed, 1):
        base -= surplus * DEEP_STASH_DISCOUNT
    else:
        base -= max(surplus - 1, 0) * DEPTH_PENALTY
    return round(max(base, 0.0), 3)


def drop_candidates(
    roster: Sequence[RosterPlayer],
    pool: PlayerPool,
    *,
    league: LeagueSettings | None = None,
    protect_ids: Iterable[str] = (),
    min_value: float = 0.0,
) -> list[tuple[RosterPlayer, float]]:
    """Rank rostered players by how safe they are to drop.

    Players on IR or the taxi squad are never droppable. Stash players are
    heavily discounted so they are only released as a last resort.
    """
    protected = set(protect_ids)
    out: list[tuple[RosterPlayer, float]] = []
    for entry in roster:
        if entry.is_ir or entry.is_taxi or entry.player_id in protected:
            continue
        value = pool.value(entry.player_id)
        score = value.adjusted if value else 0.0
        if entry.status in {"R", "NS"}:
            # A generic roster slot rather than a recognised starter.
            score *= 0.9
        if entry.position not in SKILL_POSITIONS and entry.position != "QB":
            score *= 0.5
        score = round(score, 3)
        if score < min_value:
            continue
        out.append((entry, score))
    return sorted(out, key=lambda pair: (pair[1], pair[0].player.name))


def explain(value: PlayerValue) -> str:
    """A one-line, human-readable justification for a decision."""
    parts = [
        f"proj {value.projected:.1f}",
    ]
    # Only claim a consensus number when MFL actually supplied one. A defaulted
    # 50 contributes nothing to ``adjusted`` so ranking is unaffected, but
    # printing "wsis 50" reads as a real 50% win probability from MFL, and in
    # leagues with no consensus feed that is every single player.
    if value.has_consensus:
        parts.append(f"wsis {value.wsis:.0f}")
    avail = value.availability_note
    if avail != "Available":
        parts.append(avail.lower())
    if value.projection_rank:
        parts.append(f"rank {value.projection_rank}")
    if value.note:
        parts.append(value.note)
    return ", ".join(parts)
