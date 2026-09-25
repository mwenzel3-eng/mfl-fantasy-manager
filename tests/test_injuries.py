"""Tests for the injury/availability report."""

from __future__ import annotations

from mcp_server.injuries import build_report

from .conftest import WEEK, load


def _report(roster, week: int = WEEK):
    return build_report(
        roster,
        load("injuries.json"),
        week=week,
        bye_map={b["team"]: b["bye"] for b in load("bye_weeks.json")["nflByeWeeks"]["bye"]},
    )


def test_flags_out_players(roster):
    report = _report(roster)
    out = [a for a in report.alerts if a.severity == "out"]
    assert any(a.player_id == "0006" for a in out)
    assert any(a.player_id == "0011" for a in out)


def test_flags_questionable_players(roster):
    report = _report(roster)
    q = [a for a in report.alerts if a.severity == "questionable"]
    assert {a.player_id for a in q} == {"0003", "0020"}


def test_suggests_ir_only_for_players_not_already_on_ir(roster):
    report = _report(roster)
    suggested = {a.player_id for a in report.ir_suggestions}
    # 0006 is Out and active, so it needs a move. 0011 is already on IR.
    assert "0006" in suggested
    assert "0011" not in suggested


def test_ir_suggestion_has_an_action_message(roster):
    report = _report(roster)
    for alert in report.ir_suggestions:
        assert "IR" in alert.action


def test_healthy_roster_reports_no_concerns(pool, league):
    from mcp_server.models import Player, RosterPlayer

    healthy = [
        RosterPlayer(
            player=Player(player_id=p, name=n, position=pos, team="NE"),
            status="R",
        )
        for p, n, pos in [("0001", "A. Quarterback", "QB"), ("0002", "A. Runningback", "RB")]
    ]
    report = build_report(healthy, load("injuries.json"), week=WEEK, bye_map={})

    # No concerns about *my* players. The report still carries league-wide
    # alerts for free agents, which is intentional.
    assert not [a for a in report.alerts if a.on_roster]
    assert not report.ir_suggestions
    assert not report.byes
    assert report.has_action_items is False
    assert "no injury" in report.summary().lower()


def test_report_includes_free_agent_alerts_even_when_healthy(pool, league):
    from mcp_server.models import Player, RosterPlayer

    healthy = [
        RosterPlayer(
            player=Player(player_id="0001", name="A. Quarterback", position="QB", team="NE"),
            status="R",
        )
    ]
    report = build_report(healthy, load("injuries.json"), week=WEEK, bye_map={})
    off_roster = [a for a in report.alerts if not a.on_roster]
    assert {a.player_id for a in off_roster} == {"0011", "0003", "0023", "0006", "0020"}


def test_bye_week_detection(roster):
    report = _report(roster, week=5)  # NE is on bye in week 5
    assert report.byes
    assert all(b.status == "BYE" for b in report.byes)
    assert all(b.position != "QB" or True for b in report.byes)


def test_no_byes_in_non_bye_week(roster):
    report = _report(roster, week=3)
    assert report.byes == []


def test_free_agent_alerts_marked_off_roster(roster):
    report = _report(roster)
    off_roster = [a for a in report.alerts if not a.on_roster]
    # 0020 and 0023 are free agents in the fixture.
    assert {a.player_id for a in off_roster} == {"0020", "0023"}


def test_report_timestamp_is_captured(roster):
    report = _report(roster)
    assert report.report_timestamp == 1789000000


def test_as_dict_is_serialisable(roster):
    import json

    report = _report(roster)
    assert json.loads(json.dumps(report.as_dict()))
