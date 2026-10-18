"""The six tools, against real rows.

Each one wraps a service that has its own tests, so what is checked here is
what the *tool* adds: the arguments resolved from what a sentence gives, the
synthetic flag carried out, the branches that return instead of raising, and
the `tool_calls` entry the trace is built from.

Needs Postgres, and skips cleanly without it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import models, services, simulator
from domains.strength.agent import recorder, tools
from domains.strength.schemas import LoggedLift
from planner_core.errors import PlanInvalid
from tests.api.conftest import ALL_LIFTS, START, propose
from tests.fakes import FakeEmbedder

SIM_RUNS = 40  # enough for percentiles, fast enough for a test


def _proposal_count(db_session: Session) -> int:
    return len(list(db_session.scalars(sa.select(models.Proposal))))


@pytest.fixture
def active(api_client, db_session: Session, ready) -> models.Block:
    """An approved, running block, so its sessions exist."""
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    return services.get_block(db_session, block_id)


def _mark(db_session: Session, block_id: uuid.UUID, seq: int, status: str) -> None:
    row = next(r for r in services.sessions_of(db_session, block_id) if r.seq == seq)
    row.status = status
    db_session.add(row)
    db_session.flush()


# --- 1. get_block --------------------------------------------------------


def test_get_block_with_nothing_running_is_not_an_error(db_session: Session, ready):
    """The first thing that will ever happen: a question before any planning."""
    snapshot = tools.get_block(db_session, ready["athlete"])

    assert snapshot.block_id is None
    assert snapshot.next_sessions == ()
    assert len(snapshot.one_rms) == len(ALL_LIFTS)
    assert snapshot.weekday_map  # the athlete's days are known even with no block


def test_get_block_reports_the_week_and_the_next_three_sessions(db_session: Session, ready, active):
    snapshot = tools.get_block(db_session, ready["athlete"])

    assert snapshot.block_id == active.id
    assert snapshot.status == "active"
    assert snapshot.template_weeks == 11
    assert snapshot.week == 1  # `START` is next Monday
    assert len(snapshot.next_sessions) == tools.NEXT_SESSIONS
    assert [s.seq for s in snapshot.next_sessions] == [1, 2, 3]


def test_get_block_carries_each_maxs_provenance(db_session: Session, ready, active):
    """ADR-0004 rule 4. This suite's maxes are real, so the flag is false."""
    snapshot = tools.get_block(db_session, ready["athlete"])

    assert snapshot.has_synthetic_one_rms is False
    assert all(row.is_synthetic is False for row in snapshot.one_rms)
    assert all(row.source == "tested" for row in snapshot.one_rms)


def test_get_block_says_so_when_the_block_rests_on_a_synthetic_max(
    db_session: Session, ready, active
):
    """A chat reply is exactly where 125 kg would otherwise read as tested."""
    squat = services.current_one_rms(db_session, ready["athlete"].id)["squat"]
    squat.is_synthetic = True
    db_session.add(squat)
    db_session.flush()

    snapshot = tools.get_block(db_session, ready["athlete"])

    assert snapshot.has_synthetic_one_rms is True
    assert next(row for row in snapshot.one_rms if row.lift_code == "squat").is_synthetic


def test_misses_are_derived_from_dates_not_from_a_status(db_session: Session, ready, active):
    """ADR-0010: there is no status that means "has not happened yet".

    A session the athlete means to make up stays `planned` with a date in the
    past -- that is what lets the solver re-place it -- and `missed` means
    written off. So the count is this derivation and nothing else, which spec
    13 §5A asks for without saying how.
    """
    sessions = services.sessions_of(db_session, active.id)
    past = sessions[0]
    past.date = dt.date.today() - dt.timedelta(days=3)
    db_session.add(past)

    second = sessions[1]
    second.date = dt.date.today() - dt.timedelta(days=2)
    second.status = "done"
    db_session.add(second)

    third = sessions[2]
    third.date = dt.date.today() - dt.timedelta(days=1)
    third.status = "missed"
    db_session.add(third)
    db_session.flush()

    snapshot = tools.get_block(db_session, ready["athlete"])

    # Only the one still `planned` with a date gone by.
    assert snapshot.missed_seqs == (past.seq,)


def test_a_done_session_is_not_offered_as_next(db_session: Session, ready, active):
    _mark(db_session, active.id, 1, "done")

    snapshot = tools.get_block(db_session, ready["athlete"])

    assert 1 not in [s.seq for s in snapshot.next_sessions]


# --- 2. search_notes -----------------------------------------------------


def _log_with_note(api_client, db_session: Session, block_id: uuid.UUID, text: str) -> None:
    first = next(r for r in services.sessions_of(db_session, block_id) if r.seq == 1)
    services.log_session(
        db_session,
        first.id,
        first.date,
        [LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg=Decimal("100"))],
        note=text,
        embedder=FakeEmbedder(),
    )
    db_session.flush()


def test_search_notes_finds_the_athletes_own_note(api_client, db_session: Session, ready, active):
    _log_with_note(api_client, db_session, active.id, "left knee felt sharp on the second set")

    out = tools.search_notes(db_session, ready["athlete"], "knee", FakeEmbedder(), k=3)

    assert out.query == "knee"
    assert out.hits
    assert "knee" in out.hits[0].text


def test_search_notes_returns_nothing_rather_than_failing_when_there_are_none(
    db_session: Session, ready
):
    out = tools.search_notes(db_session, ready["athlete"], "knee", FakeEmbedder())

    assert out.hits == ()


# --- 3. plan_block -------------------------------------------------------


def test_plan_block_writes_one_pending_proposal_with_33_sessions(db_session: Session, ready):
    out = tools.plan_block(
        db_session,
        ready["athlete"],
        template_id=ready["template"].id,
        lifts=ALL_LIFTS,
        start_date=START,
    )

    assert out.proposal_id is not None
    assert out.session_count == 33
    assert out.preview_only is False
    assert out.violations == ()
    row = services.get_proposal(db_session, out.proposal_id)
    assert row.status == "pending"
    assert row.kind == "new_block"


def test_plan_block_with_weeks_is_a_preview_and_never_a_proposal(db_session: Session, ready):
    """ADR-0011 leaves proposing a *shaped* block deliberately undone.

    Silently planning the template-length block instead would be worse: the
    athlete asked for nine weeks and would get eleven without being told.
    """
    out = tools.plan_block(
        db_session,
        ready["athlete"],
        template_id=ready["template"].id,
        lifts=ALL_LIFTS,
        start_date=START,
        weeks=9,
    )

    assert out.preview_only is True
    assert out.proposal_id is None
    assert out.session_count > 0
    assert _proposal_count(db_session) == 0


def test_plan_block_returns_the_violations_rather_than_raising(
    db_session: Session, athlete, template
):
    """Spec 13 §13: a tool's refusal becomes a reply, not a 422.

    No weekday map here, so the generator cannot place a session -- and the
    athlete should be told which setup step is missing, in a sentence.
    """
    out = tools.plan_block(
        db_session,
        athlete,
        template_id=template.id,
        lifts=ALL_LIFTS,
        start_date=START,
    )

    assert out.proposal_id is None
    assert out.violations
    assert _proposal_count(db_session) == 0


def test_a_refused_plan_is_recorded_with_its_rules(db_session: Session, athlete, template):
    with recorder.recording() as rec:
        tools.plan_block(
            db_session,
            athlete,
            template_id=template.id,
            lifts=ALL_LIFTS,
            start_date=START,
        )

    (call,) = rec.calls
    assert call.name == "plan_block"
    assert call.validation is not None
    assert call.validation["ok"] is False
    assert call.validation["violation_rules"]


# --- 4. reschedule -------------------------------------------------------


def test_reschedule_with_no_block_is_a_failure_not_a_crash(db_session: Session, ready):
    out = tools.reschedule(db_session, ready["athlete"], unavailable_from=dt.date.today())

    assert out.failure is not None
    assert "no running block" in out.failure.reason
    assert out.proposal_ids == ()


def test_reschedule_writes_approvable_rows(db_session: Session, ready, active):
    """The gap spec 08 left: the policy is pure, so without `save_proposals`
    the athlete is shown options with nothing behind them."""
    for seq in (1, 2):
        _mark(db_session, active.id, seq, "done")
    third = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 3)

    out = tools.reschedule(
        db_session,
        ready["athlete"],
        unavailable_from=third.date,
        unavailable_until=third.date,
        missed_dates=[third.date],
    )

    assert out.failure is None, out.failure
    assert 1 <= len(out.proposal_ids) <= 3
    for proposal_id in out.proposal_ids:
        row = services.get_proposal(db_session, proposal_id)
        assert row.status == "pending"
    assert out.reasons


def test_the_number_of_options_follows_the_tier(db_session: Session, ready, active):
    """ADR-0010, not spec 13 §5A's flat "1-3": only `in_week_move` offers
    alternatives, and `week_shift` and `fold` give exactly one."""
    for seq in (1, 2):
        _mark(db_session, active.id, seq, "done")
    third = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 3)

    out = tools.reschedule(
        db_session,
        ready["athlete"],
        unavailable_from=third.date,
        unavailable_until=third.date,
        missed_dates=[third.date],
    )

    if out.tier == "in_week_move":
        assert 1 <= len(out.proposal_ids) <= 3
    else:
        assert len(out.proposal_ids) == 1


def test_a_date_window_resolves_without_the_athlete_listing_sessions(
    db_session: Session, ready, active
):
    """ "I can't train tomorrow" gives a date, and `MissedEvent` wants seqs."""
    first = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 1)

    out = tools.reschedule(db_session, ready["athlete"], unavailable_from=first.date)

    assert out.failure is None or "no planned session" not in out.failure.reason


def test_a_window_with_no_sessions_in_it_says_so(db_session: Session, ready, active):
    far_future = dt.date.today() + dt.timedelta(days=900)

    out = tools.reschedule(
        db_session, ready["athlete"], unavailable_from=far_future, missed_dates=[far_future]
    )

    assert out.failure is not None
    assert "no planned session" in out.failure.reason


def test_an_already_done_session_is_skipped_rather_than_raising(db_session: Session, ready, active):
    """`missed_policy` raises for a seq that is already done, and "I couldn't
    train Monday" about a Monday that was logged is a mistake to absorb."""
    first = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 1)
    _mark(db_session, active.id, 1, "done")

    out = tools.reschedule(
        db_session,
        ready["athlete"],
        unavailable_from=first.date,
        missed_dates=[first.date],
    )

    assert out.failure is not None
    assert "no planned session" in out.failure.reason


def test_the_solver_stats_reach_the_tool_call_entry(db_session: Session, ready, active):
    """Spec 14 wants `solver.calendar`'s numbers, and they come from the
    policy's own stats rather than from a clock (ADR-0016)."""
    for seq in (1, 2):
        _mark(db_session, active.id, seq, "done")
    third = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 3)

    with recorder.recording() as rec:
        out = tools.reschedule(
            db_session,
            ready["athlete"],
            unavailable_from=third.date,
            unavailable_until=third.date,
            missed_dates=[third.date],
        )

    (call,) = rec.calls
    assert call.output_summary["tier"] == out.tier
    assert call.output_summary["proposal_ids"] == [str(pid) for pid in out.proposal_ids]
    if out.stats is not None:
        assert call.solver_stats is not None
        assert call.solver_stats["wall_time_s"] >= 0


# --- 5. simulate_block ---------------------------------------------------


def test_simulate_block_returns_percentiles_and_assumptions(db_session: Session, ready, active):
    """CLAUDE.md non-negotiable 8: ranges and assumptions, never one number."""
    out = tools.simulate_block(db_session, ready["athlete"], runs=SIM_RUNS)

    assert out.block_id == active.id
    assert out.result.stats.runs == SIM_RUNS
    assert out.result.assumptions
    assert out.result.missed_sessions.p10 <= out.result.missed_sessions.p90
    assert out.has_synthetic_one_rms is False


def test_simulate_block_with_a_scenario(db_session: Session, ready, active):
    event = simulator.ForcedEvent(kind="illness", start_week=6, days=10)

    out = tools.simulate_block(db_session, ready["athlete"], scenario=[event], runs=SIM_RUNS)

    assert any("illness" in line for line in out.result.assumptions)


def test_simulate_block_with_no_block_refuses(db_session: Session, ready):
    with pytest.raises(PlanInvalid):
        tools.simulate_block(db_session, ready["athlete"], runs=SIM_RUNS)


def test_a_scenario_outside_the_block_is_a_plan_invalid(db_session: Session, ready, active):
    event = simulator.ForcedEvent(kind="travel", start_week=99, days=3)

    with pytest.raises(PlanInvalid):
        tools.simulate_block(db_session, ready["athlete"], scenario=[event], runs=SIM_RUNS)


# --- 6. log_session ------------------------------------------------------


def test_log_session_records_a_session_by_date(db_session: Session, ready, active):
    """Built and tested, though no route kind dispatches to it in v1 (D2)."""
    first = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 1)

    out = tools.log_session(
        db_session,
        ready["athlete"],
        performed_on=first.date,
        entries=[
            LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg=Decimal("100")),
        ],
        note="felt good",
        embedder=FakeEmbedder(),
    )

    assert out.seq == 1
    assert out.lifts_logged == 1
    db_session.refresh(first)
    assert first.status == "done"


def test_log_session_refuses_a_date_with_no_session(db_session: Session, ready, active):
    with pytest.raises(PlanInvalid, match="no session is planned"):
        tools.log_session(
            db_session,
            ready["athlete"],
            performed_on=dt.date.today() + dt.timedelta(days=900),
            entries=[],
            embedder=FakeEmbedder(),
        )


# --- Every tool records itself -------------------------------------------


def test_every_tool_appends_one_entry(db_session: Session, ready, active):
    """Spec 13 §5A: each tool records into `Decision.tool_calls`.

    One test over all of them, because the risk is a tool added later without
    the decorator -- which no per-tool test would catch.
    """
    first = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 1)

    with recorder.recording() as rec:
        tools.get_block(db_session, ready["athlete"])
        tools.search_notes(db_session, ready["athlete"], "knee", FakeEmbedder())
        tools.simulate_block(db_session, ready["athlete"], runs=SIM_RUNS)
        tools.log_session(
            db_session,
            ready["athlete"],
            performed_on=first.date,
            entries=[
                LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg=Decimal("100"))
            ],
            embedder=FakeEmbedder(),
        )

    assert [call.name for call in rec.calls] == [
        "get_block",
        "search_notes",
        "simulate_block",
        "log_session",
    ]
    assert all(call.duration_ms >= 0 for call in rec.calls)
    assert all(call.error is None for call in rec.calls)
    # It has to survive the JSONB column.
    import json

    json.dumps(rec.dump())


def test_a_tool_entry_never_carries_the_session_or_the_athlete(db_session: Session, ready, active):
    """Spec 14 §10: summaries only, never full rows."""
    with recorder.recording() as rec:
        tools.get_block(db_session, ready["athlete"])

    (call,) = rec.calls
    assert "session" not in call.input
    assert "athlete" not in call.input
    assert "embedder" not in call.input
