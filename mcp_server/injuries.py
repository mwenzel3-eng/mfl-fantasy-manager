"""Injury and availability monitoring.

Two jobs here:

* Sunday morning - flag anyone on my roster who is out, questionable or on a
  bye, so I can act before waivers clear and lineups lock.
* Any time - decide whether an injured player needs an IR move, which MFL
  handles through the ``ir`` import (activate/deactivate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .models import (
    OUT_STATUSES,
    QUESTIONABLE_STATUSES,
    RosterPlayer,
    as_str,
    player_id as norm_player_id,
)

# Designations that should trigger a proactive IR move rather than a bench spot.
IR_WORTHY = frozenset({"IR", "Out", "IR?", "Doubtful", "DNP", "Out?"})

# A player whose designation is merely "Q" usually still plays.
BENCH_INSTEAD = frozenset({"Questionable", "Q", "Probable", "Limited"})


@dataclass
class InjuryAlert:
    player_id: str
    name: str
    position: str
    status: str
    detail: str
    severity: str  # "out" | "questionable" | "monitor"
    on_roster: bool
    needs_ir: bool = False
    action: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "position": self.position,
            "status": self.status,
            "detail": self.detail,
            "severity": self.severity,
            "on_roster": self.on_roster,
            "needs_ir": self.needs_ir,
            "action": self.action,
        }


@dataclass
class AvailabilityReport:
    week: int
    report_timestamp: int
    alerts: list[InjuryAlert] = field(default_factory=list)
    byes: list[InjuryAlert] = field(default_factory=list)
    ir_suggestions: list[InjuryAlert] = field(default_factory=list)

    @property
    def roster_alerts(self) -> list[InjuryAlert]:
        """Only the alerts that concern players I actually roster."""
        return [a for a in self.alerts if a.on_roster]

    @property
    def off_roster_alerts(self) -> list[InjuryAlert]:
        """League-wide alerts for players I do not own (usually free agents)."""
        return [a for a in self.alerts if not a.on_roster]

    @property
    def has_action_items(self) -> bool:
        """True when there is something I can actually act on.

        An injury to a free agent I do not own is not actionable, so off-roster
        alerts are excluded here even though they appear in the report.
        """
        return bool(self.roster_alerts or self.byes or self.ir_suggestions)

    def summary(self, *, include_off_roster: bool = False) -> str:
        roster = self.roster_alerts
        if not roster and not self.byes:
            lines = [f"Week {self.week}: no injury or bye concerns on your roster."]
            if self.off_roster_alerts:
                out = [a for a in self.off_roster_alerts if a.severity == "out"]
                if out:
                    lines.append(
                        f"({len(out)} injured player(s) on the wire not on your roster.)"
                    )
            return "\n".join(lines)

        lines = [f"Week {self.week} availability:"]
        for alert in sorted(roster, key=_severity_order):
            lines.append(
                f"  [{alert.severity.upper()}] {alert.name} ({alert.position}) "
                f"{alert.status} {alert.detail or ''}".rstrip()
            )
        for bye in self.byes:
            lines.append(f"  [BYE] {bye.name} ({bye.position}) on bye week {self.week}")
        if self.ir_suggestions:
            names = ", ".join(a.name for a in self.ir_suggestions)
            lines.append(f"  Suggested IR moves: {names}")
        if include_off_roster and self.off_roster_alerts:
            lines.append(
                f"  {len(self.off_roster_alerts)} other injured player(s) not on your roster."
            )
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "report_timestamp": self.report_timestamp,
            "alerts": [a.as_dict() for a in self.alerts],
            "roster_alerts": [a.as_dict() for a in self.roster_alerts],
            "off_roster_alerts": [a.as_dict() for a in self.off_roster_alerts],
            "byes": [b.as_dict() for b in self.byes],
            "ir_suggestions": [a.as_dict() for a in self.ir_suggestions],
            "has_action_items": self.has_action_items,
        }


def _normalise(injuries: object) -> tuple[list[dict], int]:
    """Accept either the client's normalised shape or a raw MFL export.

    ``MFLClient.injuries()`` returns ``{"entries": [...], "timestamp": ...}``,
    but it is convenient to be able to hand this function the untouched
    ``export?TYPE=injuries`` payload too, so both are accepted.
    """
    if not isinstance(injuries, dict):
        return [], 0
    if "entries" in injuries:
        return list(injuries.get("entries") or []), int(injuries.get("timestamp") or 0)
    inner = injuries.get("injuries")
    if isinstance(inner, dict):
        entries = inner.get("injury")
        return list(entries if isinstance(entries, list) else [entries] if entries else []), int(
            inner.get("timestamp") or 0
        )
    return [], 0


def build_report(
    roster: Sequence[RosterPlayer],
    injuries: dict,
    *,
    week: int,
    bye_map: dict[str, int] | None = None,
) -> AvailabilityReport:
    """Turn the raw injury export plus my roster into an actionable report."""
    entries, timestamp = _normalise(injuries)
    index = {
        norm_player_id(entry.get("player_id") or entry.get("id")): entry
        for entry in entries
        if (entry.get("player_id") or entry.get("id"))
    }
    on_roster = {entry.player_id: entry for entry in roster}
    bye_map = {k.upper(): v for k, v in (bye_map or {}).items()}

    report = AvailabilityReport(week=week, report_timestamp=timestamp)

    for pid, entry in index.items():
        status = as_str(entry.get("status"))
        detail = as_str(entry.get("details") or entry.get("injury_detail"))
        mine = on_roster.get(pid)
        severity = _severity(status)
        if severity is None:
            continue
        alert = InjuryAlert(
            player_id=pid,
            name=mine.player.name if mine else pid,
            position=mine.position if mine else "",
            status=status,
            detail=detail,
            severity=severity,
            on_roster=mine is not None,
        )
        if mine is not None:
            if status in IR_WORTHY and not mine.is_ir:
                alert.needs_ir = True
                alert.action = f"Move {alert.name} to IR to free a roster spot"
                report.ir_suggestions.append(alert)
            elif status in BENCH_INSTEAD:
                alert.action = f"Keep on active roster; expect a reduced stat line"
        report.alerts.append(alert)

    # Bye weeks come from the team, not the injury report.
    for entry in roster:
        team = (entry.player.team or entry.player.nfl_team or "").upper()
        if not team:
            continue
        if bye_map.get(team) == week:
            report.byes.append(
                InjuryAlert(
                    player_id=entry.player_id,
                    name=entry.player.name,
                    position=entry.position,
                    status="BYE",
                    detail=f"{team} is on bye",
                    severity="monitor",
                    on_roster=True,
                )
            )

    return report


def _severity(status: str) -> str | None:
    if status in OUT_STATUSES:
        return "out"
    if status in QUESTIONABLE_STATUSES:
        return "questionable"
    return None


def _severity_order(alert: InjuryAlert) -> tuple[int, str]:
    rank = {"out": 0, "questionable": 1, "monitor": 2}.get(alert.severity, 3)
    return (0 if alert.on_roster else 1, rank, alert.name)
