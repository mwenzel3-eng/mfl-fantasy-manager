"""Waiver recommendations.

Produces ranked ``(add, drop)`` proposals and, separately, ranked
``waiverRequest`` claims. The two are different mechanisms in MFL:

* ``fcfsWaiver``  - an *immediate* add/drop. Correct when the target is an
  available free agent and you want them right now.
* ``waiverRequest`` - a ranked list of claims for a waiver round. Correct for
  players on waivers, or when you prefer to wait for the weekly processing.

The engine proposes; :mod:`mcp_server.safety` and the MCP tools decide whether
anything is actually submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .fantasy_engine import (
    PlayerPool,
    PlayerValue,
    depth_adjusted_value,
    drop_candidates,
    explain,
)
from .models import SKILL_POSITIONS, LeagueSettings, RosterPlayer

# A free agent has to clear this bar to be worth burning a roster spot on.
MIN_VALUE_GAIN = 0.5


@dataclass
class WaiverMove:
    add: PlayerValue
    drop: RosterPlayer
    add_value: float
    drop_value: float
    rationale: str
    drop_projected: float = 0.0

    @property
    def net_gain(self) -> float:
        return round(self.add_value - self.drop_value, 3)

    def as_dict(self) -> dict[str, object]:
        return {
            "add": {
                "player_id": self.add.player_id,
                "name": self.add.player.name,
                "position": self.add.player.position,
                "projected": self.add.projected,
                "wsis": self.add.wsis,
            },
            "drop": {
                "player_id": self.drop.player_id,
                "name": self.drop.player.name,
                "position": self.drop.position,
                "projected": self.drop_projected,
            },
            "net_gain": self.net_gain,
            "rationale": self.rationale,
        }


def recommend_moves(
    roster: Sequence[RosterPlayer],
    free_agents: Sequence[PlayerValue],
    pool: PlayerPool,
    league: LeagueSettings,
    *,
    limit: int = 10,
    protect_ids: Sequence[str] = (),
) -> list[WaiverMove]:
    """Rank immediate fcfs add/drop moves by projected improvement.

    Drop selection prefers players at the same position as the incoming player
    so the move does not wreck positional balance, then falls back to the
    globally weakest rostered player.
    """
    if not free_agents:
        return []

    droppables = drop_candidates(roster, pool, league=league, protect_ids=protect_ids)
    if not droppables:
        return []

    roster_counts: dict[str, int] = {}
    for entry in roster:
        roster_counts[entry.position] = roster_counts.get(entry.position, 0) + 1

    by_position: dict[str, list[tuple[RosterPlayer, float]]] = {}
    for pair in droppables:
        by_position.setdefault(pair[0].position, []).append(pair)

    moves: list[WaiverMove] = []
    for candidate in sorted(free_agents, key=lambda v: -v.adjusted):
        # Pick the drop before valuing the add, because which drop is chosen
        # determines whether the depth penalty applies at all.
        preferred = by_position.get(candidate.player.position, [])
        if preferred:
            drop_entry, drop_value = preferred[0]
            # The drop removes a player at this exact position, which is the
            # surplus that depth_adjusted_value() would otherwise charge for.
            # Applying both counted the same overstaffing twice and rejected
            # almost every same-position swap: at three WRs in three slots the
            # penalty alone is 2 * 0.45 = 0.9, nearly double the threshold.
            add_value = candidate.adjusted
        else:
            drop_entry, drop_value = droppables[0]
            # Nothing at this position is being dropped, so the surplus really
            # does stay and the depth penalty is genuine.
            add_value = depth_adjusted_value(candidate, roster, league=league)

        if add_value < MIN_VALUE_GAIN:
            continue

        if add_value - drop_value < MIN_VALUE_GAIN:
            continue

        surplus = roster_counts.get(candidate.player.position, 0) - _required(
            league, candidate.player.position
        )
        drop_pool_value = pool.value(drop_entry.player_id)
        drop_projected = drop_pool_value.projected if drop_pool_value else 0.0

        rationale = (
            f"{candidate.player.name} projects {candidate.projected:.1f} "
            f"({explain(candidate)}), replacing {drop_entry.player.name}"
        )
        # Say *why* the drop target is expendable; injury status is usually the
        # clearest reason and it is the bit most likely to be missed.
        if drop_entry.player.is_out:
            rationale += f", who is {drop_entry.player.injury_status}"
            if drop_entry.player.injury_detail:
                rationale += f" ({drop_entry.player.injury_detail})"
            rationale += " and worth little while injured"
        elif drop_entry.player.is_questionable:
            rationale += ", who is questionable"
        if surplus <= 0:
            rationale += "; fills a needed position"
        elif surplus > 0:
            rationale += f"; {surplus} surplus already at {candidate.player.position}"

        moves.append(
            WaiverMove(
                add=candidate,
                drop=drop_entry,
                add_value=add_value,
                drop_value=drop_value,
                rationale=rationale,
                drop_projected=drop_projected,
            )
        )
        if len(moves) >= limit:
            break

    return sorted(moves, key=lambda m: -m.net_gain)


def nearest_miss(
    roster: Sequence[RosterPlayer],
    free_agents: Sequence[PlayerValue],
    pool: PlayerPool,
    league: LeagueSettings,
    *,
    protect_ids: Sequence[str] = (),
) -> str | None:
    """Describe the closest rejected swap, or None if there was no candidate.

    "No move cleared the threshold" is unactionable on its own: it reads the
    same whether the market is empty, the projections are missing, or the
    threshold is simply strict. This reports the actual numbers so the next run
    says which it was.
    """
    if not free_agents:
        return "No free agents were available to evaluate."
    droppables = drop_candidates(roster, pool, league=league, protect_ids=protect_ids)
    if not droppables:
        return "No rostered player was droppable, so there was nothing to compare against."

    by_position: dict[str, list[tuple[RosterPlayer, float]]] = {}
    for pair in droppables:
        by_position.setdefault(pair[0].position, []).append(pair)

    best: tuple[float, PlayerValue, RosterPlayer] | None = None
    for candidate in sorted(free_agents, key=lambda v: -v.adjusted):
        preferred = by_position.get(candidate.player.position, [])
        if preferred:
            drop_entry, drop_value = preferred[0]
            add_value = candidate.adjusted
        else:
            drop_entry, drop_value = droppables[0]
            add_value = depth_adjusted_value(candidate, roster, league=league)
        gain = add_value - drop_value
        if best is None or gain > best[0]:
            best = (gain, candidate, drop_entry)

    if best is None:
        return None
    gain, candidate, drop_entry = best
    return (
        f"Closest call: +{candidate.player.name} ({candidate.player.position}) "
        f"projects {candidate.projected:.1f} against {drop_entry.player.name} "
        f"({drop_entry.position}), a net {gain:+.1f} - below the "
        f"{MIN_VALUE_GAIN:.1f} threshold."
    )


def recommend_claims(
    roster: Sequence[RosterPlayer],
    candidates: Sequence[PlayerValue],
    pool: PlayerPool,
    league: LeagueSettings,
    *,
    limit: int = 5,
    protect_ids: Sequence[str] = (),
) -> list[tuple[PlayerValue, RosterPlayer, str]]:
    """Rank ``(claim, drop_if_awarded)`` pairs for a waiver round.

    Ordered by desirability, most wanted first, because MFL processes the
    ``PICKS`` list in priority order.
    """
    droppables = drop_candidates(roster, pool, league=league, protect_ids=protect_ids)
    if not droppables:
        return []

    out: list[tuple[PlayerValue, RosterPlayer, str]] = []
    for candidate in sorted(candidates, key=lambda v: -v.adjusted):
        add_value = depth_adjusted_value(candidate, roster, league=league)
        if add_value < MIN_VALUE_GAIN:
            continue
        same_position = [pair for pair in droppables if pair[0].position == candidate.player.position]
        drop_entry, drop_value = same_position[0] if same_position else droppables[0]
        if add_value - drop_value < MIN_VALUE_GAIN:
            continue
        out.append(
            (
                candidate,
                drop_entry,
                f"{explain(candidate)}; drop {drop_entry.player.name} if awarded",
            )
        )
        if len(out) >= limit:
            break
    return out


def claims_to_pic(
    claims: Sequence[tuple[PlayerValue, RosterPlayer, str]],
) -> list[tuple[str, str]]:
    """Format claims for the ``waiverRequest`` ``PICKS`` parameter."""
    return [(claim.player_id, drop.player_id) for claim, drop, _ in claims]


def _required(league: LeagueSettings, position: str) -> int:
    return league.starter_counts().get(position.upper(), 1)


def stale_waivers(
    pending: Sequence[dict],
    *,
    current_players: Sequence[str] = (),
) -> list[dict]:
    """Flag pending claims that are no longer worth making.

    Useful before the Thursday job, when a Wednesday claim may have been
    beaten or rendered pointless by later news.
    """
    available = set(current_players)
    out: list[dict] = []
    for entry in pending:
        pid = str(entry.get("player_id") or entry.get("id") or "")
        if pid and available and pid not in available:
            out.append(entry)
    return out
