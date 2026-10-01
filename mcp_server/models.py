"""Domain models and parsing helpers for MFL responses.

MFL is liberal with its JSON: single elements come back as a dict, multiple
elements as a list, and absent elements simply do not appear. These helpers
normalise all of that so the rest of the codebase can assume lists.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Status values used by the `rosters` export for a rostered player.
STATUS_ROSTER = "R"
STATUS_STARTER = "S"
STATUS_NONSTARTER = "NS"
STATUS_IR = "IR"
STATUS_TAXI = "TS"

# Positions that MFL treats as offense skill positions for lineup construction.
SKILL_POSITIONS = frozenset({"RB", "WR", "TE"})

# Injury statuses that should keep a player out of a lineup outright.
OUT_STATUSES = frozenset({"Out", "IR", "IR?", "Doubtful", "Out?", "PUP", "NIR", "Suspended", "COVID-19"})

# The rosters export spells roster status out in full ("ROSTER",
# "INJURED_RESERVE"), while the rest of the code compares the short codes above.
# Comparing the long form against the short code is silently false, which made
# is_active false for every player and left injured players looking droppable.
# Accept both spellings.
_ROSTER_STATUS_ALIASES = {
    "ROSTER": STATUS_ROSTER,
    "RESERVE": STATUS_ROSTER,
    "STARTER": STATUS_STARTER,
    "NONSTARTER": STATUS_NONSTARTER,
    "NON-STARTER": STATUS_NONSTARTER,
    "INJURED_RESERVE": STATUS_IR,
    "INJURED RESERVE": STATUS_IR,
    "IR": STATUS_IR,
    "TAXI": STATUS_TAXI,
    "TS": STATUS_TAXI,
}


def norm_roster_status(raw: object, default: str = STATUS_ROSTER) -> str:
    """Map a roster-status value to one of the STATUS_* codes."""
    text = as_str(raw).upper()
    return _ROSTER_STATUS_ALIASES.get(text, text or default)
QUESTIONABLE_STATUSES = frozenset({"Questionable", "Q", "Out?", "IR?", "Doubtful*"})


def as_list(value: Any) -> list[Any]:
    """Coerce MFL's dict-or-list-or-missing shape into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def player_id(value: Any) -> str:
    """MFL player ids are 4-5 digit strings and must keep their leading zeros."""
    text = as_str(value)
    if text.isdigit() and len(text) < 4:
        return text.zfill(4)
    return text


def franchise_id(value: Any) -> str:
    """Franchise ids are always 4-digit zero-padded strings ('0000' = commissioner)."""
    text = as_str(value)
    if text.isdigit():
        return text.zfill(4)
    return text


@dataclass(frozen=True, slots=True)
class Player:
    player_id: str
    name: str
    position: str
    team: str = ""
    nfl_team: str = ""
    status: str = ""
    injury_status: str = ""
    injury_detail: str = ""
    bye_week: int | None = None
    drafted: str = ""

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Player":
        pid = player_id(raw.get("id"))
        return cls(
            player_id=pid,
            name=as_str(raw.get("name")),
            # The live players export returns "position"; the documented
            # field name is "pos". Reading only "pos" gave every player in the
            # database an empty position, which quietly disabled all
            # position-based logic: drop filtering, depth, lineup validation.
            position=as_str(raw.get("position") or raw.get("pos")).upper(),
            team=as_str(raw.get("team")).upper(),
            nfl_team=as_str(raw.get("nfl_team") or raw.get("team")).upper(),
            status=as_str(raw.get("status")).upper(),
            injury_status=as_str(raw.get("injury_status")),
            injury_detail=as_str(raw.get("injury_detail")),
            bye_week=as_int(raw["bye_week"]) if raw.get("bye_week") not in (None, "") else None,
            drafted=as_str(raw.get("draft_pick")),
        )

    @property
    def is_injury(self) -> bool:
        return bool(self.injury_status)

    @property
    def is_out(self) -> bool:
        return self.injury_status in OUT_STATUSES

    @property
    def is_questionable(self) -> bool:
        return self.injury_status in QUESTIONABLE_STATUSES

    @property
    def is_flex_eligible(self) -> bool:
        return self.position in SKILL_POSITIONS

    def __str__(self) -> str:  # pragma: no cover - display only
        bits = [self.name, f"({self.position})"]
        if self.team:
            bits.append(self.team)
        if self.injury_status:
            bits.append(f"[{self.injury_status}]")
        return " ".join(bits)


@dataclass(frozen=True, slots=True)
class RosterEntry:
    player_id: str
    franchise_id: str
    status: str
    salary: str = ""
    contract: str = ""

    @property
    def is_ir(self) -> bool:
        return self.status == STATUS_IR

    @property
    def is_taxi(self) -> bool:
        return self.status == STATUS_TAXI

    @property
    def is_active(self) -> bool:
        return self.status in {STATUS_ROSTER, STATUS_STARTER, STATUS_NONSTARTER}


@dataclass(frozen=True, slots=True)
class RosterPlayer:
    """A player as seen on one franchise's roster, joined with player metadata."""

    player: Player
    status: str
    salary: str = ""

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def position(self) -> str:
        return self.player.position

    @property
    def is_ir(self) -> bool:
        return self.status == STATUS_IR

    @property
    def is_taxi(self) -> bool:
        return self.status == STATUS_TAXI

    @property
    def is_startable(self) -> bool:
        return not self.is_ir and not self.is_taxi


@dataclass(frozen=True, slots=True)
class Franchise:
    franchise_id: str
    name: str
    owner_name: str = ""
    is_mine: bool = False


@dataclass(frozen=True, slots=True)
class LeagueSettings:
    """The subset of the `league` export we actually need."""

    name: str
    season: str
    host: str
    roster_positions: dict[str, int] = field(default_factory=dict)
    starter_slots: tuple[tuple[str, str], ...] = ()  # ((slot_label, position), ...)
    roster_limits: dict[str, int] = field(default_factory=dict)
    ir_slots: int = 0
    taxi_slots: int = 0
    divisions: bool = False
    current_week: int = 0
    last_week: int = 0
    final_week: int = 0
    franchise_count: int = 0

    @property
    def flex_slots(self) -> int:
        return sum(1 for _label, pos in self.starter_slots if pos.upper().startswith("FLX"))

    @property
    def flex_eligible_positions(self) -> frozenset[str]:
        """Positions a FLX slot may hold.

        MFL's flex slot accepts skill players (RB/WR/TE) regardless of which of
        those positions the league starts, so a TE on a 1-QB/2-RB/3-WR league can
        still flex. We default to the skill positions and intersect with
        whatever the league actually starts, so an exotic league cannot smuggle
        a QB into a flex slot.
        """
        eligible = {pos for _label, pos in self.starter_slots if pos in SKILL_POSITIONS}
        eligible |= set(SKILL_POSITIONS)
        return frozenset(eligible)

    def starter_counts(self) -> dict[str, int]:
        """Required starts per concrete position, flex excluded."""
        counts: dict[str, int] = {}
        for _label, pos in self.starter_slots:
            key = pos.upper()
            if key.startswith("FLX"):
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts


def parse_slot_spec(spec: str) -> tuple[tuple[str, str], ...]:
    """Parse MFL slot strings like ``"QB,1,RB,2,WR,3,FLX,1"``."""
    parts = [p.strip().upper() for p in spec.split(",") if p.strip()]
    out: list[tuple[str, str]] = []
    for pos, count in zip(parts[0::2], parts[1::2]):
        try:
            n = int(count)
        except ValueError:
            continue
        out.extend((pos, pos) for _ in range(n))
    return tuple(out)


def parse_mfl_starters(structured: Any) -> tuple[tuple[str, str], ...]:
    """Parse MFL's structured ``starters`` object into ((label, position), ...).

    MFL does not send starters as a flat ``"QB,1,RB,2"`` string. It sends::

        {"position": [{"name": "QB", "limit": "1"},
                      {"name": "RB", "limit": "2-3"}], "count": "10"}

    ``limit`` is a pick *range*, not a count, so "2-3" means two slots and "1"
    means one. A name containing "+" is one slot several positions may fill, so
    its range is dealt round-robin across the listed positions.
    """
    if not isinstance(structured, Mapping):
        return parse_slot_spec(as_str(structured))
    out: list[tuple[str, str]] = []
    for entry in as_list(structured.get("position")):
        if not isinstance(entry, Mapping):
            continue
        raw_name = as_str(entry.get("name")).upper()
        if not raw_name:
            continue
        positions = [p for p in raw_name.split("+") if p]
        if not positions:
            continue
        limit = as_str(entry.get("limit")).strip()
        if "-" in limit:
            low, _, high = limit.partition("-")
            # "2-3" spans picks 2 and 3, so two slots. "0-0" is MFL's
            # "unlimited/none" sentinel rather than pick zero, so a range
            # ending at 0 is empty; inclusive counting would wrongly give one.
            high_n = as_int(high)
            slots = max(0, high_n - as_int(low) + 1) if high_n > 0 else 0
        else:
            slots = max(0, as_int(limit))
        for i in range(slots):
            pos = positions[i % len(positions)]
            out.append((pos if len(positions) == 1 else raw_name, pos))
    return tuple(out)


def mfl_timestamp(value: Any) -> float:
    """MFL timestamps are Unix seconds; tolerate floats and empty strings."""
    return as_float(value, 0.0)


def format_mfl_timestamp(value: float) -> str:  # pragma: no cover - display only
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def players_index(players: Iterable[Player]) -> dict[str, Player]:
    return {p.player_id: p for p in players}


def names_of(index: dict[str, Player], ids: Sequence[str]) -> list[str]:
    return [index[pid].name if pid in index else pid for pid in ids]
