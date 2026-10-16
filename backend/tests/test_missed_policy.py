"""Spec 08, as amended: what the policy does when training was missed.

The four tiers are structural, so every test here sets up a *shape* -- a
session already trained after the missed one, two weeks with nothing in them,
no room left in the week -- rather than a number of days. That is the point of
the amendment: the question is what the calendar still allows, not how long
the athlete was away.

Pure throughout. No database, no clock: `today` arrives on the `MissedEvent`,
and the block is built in memory from the committed golden fixture.

The maxes here (135/92.5/175/57.5/77.5) are not the spreadsheet's example
maxes and not the generator suite's either, so any weight in a failure message
can be traced to this file (CLAUDE.md guardrail).
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from domains.strength import generator, missed_policy, rep_max
from domains.strength.missed_policy import MissedEvent, MissedPolicyConfig, classify, propose
from domains.strength.schemas import BlockState, LoggedLift, LoggedSession
from domains.strength.validator import ValidationContext, read_test_weeks
from tests import golden

POLICY_MAXES = {
    "squat": Decimal("135"),
    "bench": Decimal("92.5"),
    "deadlift": Decimal("175"),
    "press": Decimal("57.5"),
    "row": Decimal("77.5"),
}

# A small, single-threaded, short-budget solver so the suite stays fast and
# reproducible, matching `test_calendar_solver.SOLVE_KW`.
CFG = MissedPolicyConfig(solver_time_limit_s=2.0, solver_num_workers=1)

# The block runs Mon/Wed/Fri from 2026-01-05. Week 3's Monday is seq 7.
W3_MONDAY_SEQ = 7
W3_MONDAY = dt.date(2026, 1, 19)
W3_TUESDAY = dt.date(2026, 1, 20)


@pytest.fixture(scope="module")
def draft(golden_entries):
    return generator.generate_block(
        golden.athlete("5.00", "nearest"),
        golden.template(),
        golden_entries,
        POLICY_MAXES,
        sorted(POLICY_MAXES),
        golden.START,
        golden.MON_WED_FRI,
    )


def state(draft, *, done=(), overrides=None, logs=(), status="active") -> BlockState:
    """The block part-way through, with `done` seqs trained."""
    status_of = {s.seq: "planned" for s in draft.sessions}
    status_of.update({seq: "done" for seq in done})
    status_of.update(overrides or {})
    return BlockState(
        block_id=uuid.uuid4(),
        athlete_id=draft.athlete_id,
        template_id=draft.template_id,
        status=status,
        start_date=draft.start_date,
        template_weeks=draft.template_weeks,
        lifts=draft.lifts,
        one_rm_snapshot=draft.one_rm_snapshot,
        weekday_map=golden.MON_WED_FRI,
        plan=draft.sessions,
        status_of=status_of,
        logs=tuple(logs),
    )


def ctx(golden_entries, today) -> ValidationContext:
    return ValidationContext(
        weekday_map=golden.MON_WED_FRI,
        overreach_from_week=7,
        test_week=read_test_weeks(golden_entries),
        plate_increment_kg=Decimal("5.00"),
        today=today,
    )


def event(block_state, seqs, *, frm, until=None, today, injury=False) -> MissedEvent:
    return MissedEvent(
        block_id=block_state.block_id,
        missed_seqs=tuple(seqs),
        unavailable_from=frm,
        unavailable_until=until,
        today=today,
        injury=injury,
    )


def date_of(draft, seq) -> dt.date:
    return next(s.date for s in draft.sessions if s.seq == seq)


# --- 1. Classification ------------------------------------------------------


def test_a_session_missed_with_room_left_in_the_week_is_an_in_week_move(draft):
    """Missed Monday, back Tuesday, nothing trained since -> move it."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    assert classify(missed, block_state) == "in_week_move"


def test_an_absence_that_swallows_the_rest_of_the_week_shifts_it(draft):
    """Away past Friday, so no day is left: the week is lost, not rearranged.

    Note what decides this. Not "eight days is tier two" -- the days the
    athlete was away are simply not available, so the in-week search comes
    back empty and the block stretches instead.
    """
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(
        block_state,
        [W3_MONDAY_SEQ],
        frm=W3_MONDAY,
        until=dt.date(2026, 1, 26),
        today=dt.date(2026, 1, 27),
    )
    assert classify(missed, block_state) == "week_shift"


def test_training_a_later_session_anyway_forces_a_fold(draft):
    """Missed Monday, trained Wednesday. There is nowhere left to put Monday.

    `seq` order is a hard rule, so a session cannot be moved to after one that
    has already happened. This is the case spec 08's original four tiers did
    not cover and `calendar_solver._order_conflict` reports.
    """
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ), overrides={W3_MONDAY_SEQ + 1: "done"})
    missed = event(
        block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=dt.date(2026, 1, 22)
    )
    assert classify(missed, block_state) == "fold"


def test_an_injury_always_restarts(draft):
    """However short the absence. The question stops being a scheduling one."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(
        block_state,
        [W3_MONDAY_SEQ],
        frm=W3_MONDAY,
        until=W3_MONDAY,
        today=W3_TUESDAY,
        injury=True,
    )
    assert classify(missed, block_state) == "restart"


def test_two_consecutive_empty_weeks_restart_the_block(draft):
    """Weeks 1 and 2 are over and nothing was completed in either."""
    block_state = state(draft)
    missed = event(block_state, [1], frm=golden.START, today=golden.START + dt.timedelta(days=15))
    assert classify(missed, block_state) == "restart"


def test_one_empty_week_does_not_restart_the_block(draft):
    """The threshold is a starting value, and this is the side of it that heals."""
    block_state = state(draft, done=[1, 2, 3])
    missed = event(
        block_state, [4], frm=date_of(draft, 4), today=golden.START + dt.timedelta(days=15)
    )
    assert classify(missed, block_state) != "restart"


def test_a_part_elapsed_week_is_not_counted_as_empty(draft):
    """A week the athlete can still train says nothing about whether they did.

    Asked on week 2's Monday: week 1 is empty and over, week 2 has barely
    begun. One empty week, so no restart.
    """
    block_state = state(draft)
    today = golden.START + dt.timedelta(days=7)
    assert missed_policy._empty_week_run(block_state, today) == 1


@pytest.mark.parametrize("status", ["draft", "done", "abandoned"])
def test_a_block_that_is_not_running_is_refused(draft, status):
    block_state = state(draft, status=status)
    missed = event(block_state, [1], frm=golden.START, today=W3_TUESDAY)
    with pytest.raises(ValueError, match=f"is {status}"):
        classify(missed, block_state)


def test_a_paused_block_is_still_running(draft):
    """ADR-0007 treats `paused` as running, and so does this."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ), status="paused")
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    assert classify(missed, block_state) == "in_week_move"


def test_a_session_already_done_cannot_be_missed(draft):
    block_state = state(draft, done=[1])
    missed = event(block_state, [1], frm=golden.START, today=W3_TUESDAY)
    with pytest.raises(ValueError, match="already done"):
        classify(missed, block_state)


def test_a_seq_the_block_does_not_have_is_refused(draft):
    block_state = state(draft)
    missed = event(block_state, [999], frm=golden.START, today=W3_TUESDAY)
    with pytest.raises(ValueError, match="not in the block"):
        classify(missed, block_state)


# --- 2. in_week_move --------------------------------------------------------


def test_the_in_week_move_puts_monday_on_tuesday_and_changes_nothing_else(draft, golden_entries):
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    result = propose(missed, block_state, ctx(golden_entries, W3_TUESDAY), CFG)

    assert result.ok, result.failure
    assert result.tier == "in_week_move"
    best = result.proposals[0]
    assert [(c.seq, c.action, c.to_date) for c in best.payload.changes] == [
        (W3_MONDAY_SEQ, "move", W3_TUESDAY)
    ]
    # Every other session keeps its date.
    moved = {c.seq for c in best.payload.changes}
    assert all(
        planned.date == date_of(draft, planned.seq)
        for planned in best.payload.plan
        if planned.seq not in moved
    )


def test_the_in_week_move_reason_names_the_days_and_the_lifts(draft, golden_entries):
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    result = propose(missed, block_state, ctx(golden_entries, W3_TUESDAY), CFG)
    reason = result.proposals[0].reasons[0]
    assert reason == (
        "Moves Monday 2026-01-19's squat/bench to Tuesday 2026-01-20. Nothing else changes."
    )


def test_fast_mode_and_cp_sat_choose_the_same_day(draft, golden_entries):
    """Spec 08 §14's `fast` requirement, as the strongest claim available.

    With one session free and every other pinned, the greedy placement
    minimises the same cost over the same feasible set CP-SAT searches, and
    both settle ties on the earliest date. They must agree -- otherwise spec
    10's simulator is quietly simulating a different planner than the one the
    athlete is offered.
    """
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    exact = propose(missed, block_state, ctx(golden_entries, W3_TUESDAY), CFG)
    quick = propose(missed, block_state, ctx(golden_entries, W3_TUESDAY), CFG, fast=True)

    assert quick.tier == exact.tier
    assert quick.ok and exact.ok
    assert quick.proposals[0].payload.changes[0].to_date == (
        exact.proposals[0].payload.changes[0].to_date
    )


def test_every_in_week_option_passes_the_validator(draft, golden_entries):
    """Spec 08 §14: a returned proposal is one approval will not refuse."""
    from domains.strength.validator import validate

    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    result = propose(missed, block_state, ctx(golden_entries, W3_TUESDAY), CFG)
    for option in result.proposals:
        base = ctx(golden_entries, W3_TUESDAY)
        waived = base.model_copy(
            update={"approved_off_weekday": {c.seq for c in option.payload.changes}}
        )
        verdict = validate(option.payload.plan, waived, changes=option.payload.changes)
        assert verdict.ok, [v.message for v in verdict.violations]


# --- 3. week_shift ----------------------------------------------------------


def shifted(draft, golden_entries):
    block_state = state(draft, done=range(1, 13))
    away_from = date_of(draft, 13)
    missed = event(
        block_state,
        [13],
        frm=away_from,
        until=away_from + dt.timedelta(days=7),
        today=away_from + dt.timedelta(days=8),
    )
    return block_state, missed, propose(missed, block_state, ctx(golden_entries, missed.today), CFG)


def test_a_week_shift_moves_whole_weeks_so_nothing_leaves_its_weekday(draft, golden_entries):
    """The property that makes this safe without a solver.

    Adding a multiple of seven preserves the weekday, so a shift never needs
    the off-weekday waiver -- which is why it can be computed directly instead
    of searched for.
    """
    _, _, result = shifted(draft, golden_entries)
    assert result.ok, result.failure
    assert result.tier == "week_shift"
    plan = result.proposals[0].payload.plan
    assert all(planned.date.weekday() == golden.MON_WED_FRI[planned.slot] for planned in plan)
    assert result.proposals[0].cost["off_weekday_count"] == 0


def test_a_week_shift_moves_everything_from_the_missed_session_onward(draft, golden_entries):
    _, _, result = shifted(draft, golden_entries)
    changes = result.proposals[0].payload.changes
    assert {c.seq for c in changes} == {s.seq for s in draft.sessions if s.seq >= 13}
    assert all(c.action == "move" for c in changes)


def test_a_week_shift_leaves_the_sessions_before_it_alone(draft, golden_entries):
    _, _, result = shifted(draft, golden_entries)
    for planned in result.proposals[0].payload.plan:
        if planned.seq < 13:
            assert planned.date == date_of(draft, planned.seq)


def test_a_week_shift_keeps_the_missed_session_s_own_numbers(draft, golden_entries):
    """ "Same tonnage": the session moves, its prescription does not."""
    _, _, result = shifted(draft, golden_entries)
    before = next(s for s in draft.sessions if s.seq == 13)
    after = next(s for s in result.proposals[0].payload.plan if s.seq == 13)
    assert after.lifts == before.lifts
    assert after.week == before.week


def test_a_week_shift_pushes_the_block_end_back(draft, golden_entries):
    _, missed, result = shifted(draft, golden_entries)
    plan = result.proposals[0].payload.plan
    # Two weeks, not one: the athlete was still away on the first candidate day.
    assert max(p.date for p in plan) == draft.sessions[-1].date + dt.timedelta(days=14)
    assert result.proposals[0].cost["end_slip_days"] == 14


def test_a_week_shift_never_lands_on_a_day_the_athlete_is_away(draft, golden_entries):
    _, missed, result = shifted(draft, golden_entries)
    moved_to = next(c.to_date for c in result.proposals[0].payload.changes if c.seq == 13)
    assert moved_to not in missed.unavailable_days
    assert moved_to >= missed.today


def test_a_week_shift_only_widens_the_gaps_that_straddle_it(draft, golden_entries):
    """The rest-gap half of the proof, checked rather than asserted in prose."""
    _, _, result = shifted(draft, golden_entries)
    plan = {p.seq: p.date for p in result.proposals[0].payload.plan}
    before = {s.seq: s.date for s in draft.sessions}
    for earlier, later in zip(sorted(plan), sorted(plan)[1:], strict=False):
        was = (before[later] - before[earlier]).days
        now = (plan[later] - plan[earlier]).days
        assert now >= was


# --- 4. fold ----------------------------------------------------------------


def folded(draft, golden_entries):
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ), overrides={W3_MONDAY_SEQ + 1: "done"})
    today = dt.date(2026, 1, 22)
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=today)
    return block_state, propose(missed, block_state, ctx(golden_entries, today), CFG)


def test_a_fold_moves_the_lifts_into_the_next_session_that_trains_them(draft, golden_entries):
    _, result = folded(draft, golden_entries)
    assert result.ok, result.failure
    assert result.tier == "fold"
    change = result.proposals[0].payload.changes[0]
    assert (change.seq, change.action, change.into_seq) == (W3_MONDAY_SEQ, "fold", 9)


def test_a_fold_drops_the_missed_session_from_the_plan(draft, golden_entries):
    _, result = folded(draft, golden_entries)
    plan = result.proposals[0].payload.plan
    assert not any(p.seq == W3_MONDAY_SEQ for p in plan)


def test_a_fold_keeps_both_sessions_worth_of_work(draft, golden_entries):
    """The receiving session keeps its own numbers *and* gains the missed ones.

    Squat appears twice afterwards, at different sets and intensities.
    Collapsing them would quietly delete a session.
    """
    _, result = folded(draft, golden_entries)
    missed_lifts = next(s for s in draft.sessions if s.seq == W3_MONDAY_SEQ).lifts
    target_lifts = next(s for s in draft.sessions if s.seq == 9).lifts
    merged = next(s for s in result.proposals[0].payload.plan if s.seq == 9).lifts

    assert len(merged) == len(missed_lifts) + len(target_lifts)
    assert sorted(lift.lift_code for lift in merged) == sorted(
        lift.lift_code for lift in missed_lifts + target_lifts
    )
    # Training order, not "mine then yours".
    codes = ("squat", "bench", "deadlift", "press", "row")
    order = {code: index for index, code in enumerate(codes)}
    assert [order[lift.lift_code] for lift in merged] == sorted(
        order[lift.lift_code] for lift in merged
    )


def test_a_fold_moves_no_dates(draft, golden_entries):
    _, result = folded(draft, golden_entries)
    for planned in result.proposals[0].payload.plan:
        assert planned.date == date_of(draft, planned.seq)


def test_a_fold_with_nowhere_to_go_fails_with_a_reason(draft, golden_entries):
    """The last session of the block has no successor to fold into."""
    last = draft.sessions[-1].seq
    block_state = state(draft, done=[s.seq for s in draft.sessions if s.seq < last - 1])
    block_state = block_state.model_copy(
        update={"status_of": {**block_state.status_of, last: "done"}}
    )
    today = draft.sessions[-1].date + dt.timedelta(days=1)
    missed = event(block_state, [last - 1], frm=date_of(draft, last - 1), today=today)
    result = propose(missed, block_state, ctx(golden_entries, today), CFG)
    assert not result.ok
    assert "nothing to fold into" in result.failure.reason


# --- 5. restart -------------------------------------------------------------


def test_a_restart_reduces_every_max_including_the_ones_with_no_logs(draft):
    """The factor is not conditional on evidence.

    A lift with no recent set is not a lift that held its strength; it is a
    lift with nothing to say either way, and the restart's assumption applies
    to it like any other. Treating logged and unlogged lifts differently
    would make the new block's balance depend on what the athlete happened to
    write down.
    """
    reduced = missed_policy.restart_one_rms(state(draft), CFG)
    for lift, before in draft.one_rm_snapshot.items():
        assert reduced[lift] == rep_max.quantize_kg(Decimal(before) * CFG.restart_one_rm_factor)
        assert reduced[lift] < before


def test_a_restart_never_raises_a_max(draft):
    """ADR-0005 rule 6: only a tested single raises a max, never an estimate.

    A heavy triple logged in the last weeks implies a bigger 1RM than the
    snapshot. A restart still does not take it.
    """
    heavy = LoggedSession(
        seq=draft.sessions[-1].seq,
        performed_on=draft.sessions[-1].date,
        entries=(
            LoggedLift(lift_code="squat", sets_done=1, reps_done=3, weight_kg=Decimal("200")),
        ),
    )
    reduced = missed_policy.restart_one_rms(state(draft, logs=[heavy]), CFG)
    assert reduced["squat"] < draft.one_rm_snapshot["squat"]


def test_a_restart_takes_a_recent_log_when_it_is_lower_than_the_snapshot(draft):
    """A weak recent set is evidence, and the restart uses it."""
    weak_seq = draft.sessions[-1].seq
    weak = LoggedSession(
        seq=weak_seq,
        performed_on=draft.sessions[-1].date,
        entries=(LoggedLift(lift_code="squat", sets_done=1, reps_done=5, weight_kg=Decimal("80")),),
    )
    reduced = missed_policy.restart_one_rms(state(draft, logs=[weak]), CFG)
    implied = rep_max.estimate_1rm(Decimal("80"), 5)
    assert reduced["squat"] == rep_max.quantize_kg(implied * CFG.restart_one_rm_factor)


def test_a_restart_ignores_a_log_from_before_the_window(draft):
    """`restart_estimate_window_weeks` is counted in template weeks, no clock."""
    early = LoggedSession(
        seq=1,
        performed_on=golden.START,
        entries=(LoggedLift(lift_code="squat", sets_done=1, reps_done=5, weight_kg=Decimal("80")),),
    )
    reduced = missed_policy.restart_one_rms(state(draft, logs=[early]), CFG)
    assert reduced["squat"] == rep_max.quantize_kg(
        draft.one_rm_snapshot["squat"] * CFG.restart_one_rm_factor
    )


def test_a_restart_skips_a_set_too_long_to_estimate_from(draft):
    """`estimate_1rm` refuses past 12 reps; a log of 20 is not an error."""
    marathon = LoggedSession(
        seq=draft.sessions[-1].seq,
        performed_on=draft.sessions[-1].date,
        entries=(
            LoggedLift(lift_code="squat", sets_done=1, reps_done=20, weight_kg=Decimal("60")),
        ),
    )
    reduced = missed_policy.restart_one_rms(state(draft, logs=[marathon]), CFG)
    assert reduced["squat"] == rep_max.quantize_kg(
        draft.one_rm_snapshot["squat"] * CFG.restart_one_rm_factor
    )


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (dt.date(2026, 1, 20), dt.date(2026, 1, 26)),  # Tuesday -> next Monday
        (dt.date(2026, 1, 26), dt.date(2026, 1, 26)),  # already a Monday
    ],
)
def test_a_restart_begins_on_a_monday(draft, today, expected):
    """`generate_block` requires one, and a mid-week start desynchronises
    every weekday in the block."""
    block_state = state(draft)
    missed = event(block_state, [1], frm=golden.START, today=today)
    assert missed_policy.restart_start_date(missed) == expected


def test_a_restart_waits_until_the_athlete_is_back(draft):
    block_state = state(draft)
    missed = event(
        block_state,
        [1],
        frm=golden.START,
        until=dt.date(2026, 2, 3),
        today=dt.date(2026, 1, 20),
    )
    assert missed_policy.restart_start_date(missed) == dt.date(2026, 2, 9)


def test_a_restart_without_a_block_to_offer_fails_rather_than_guessing(draft, golden_entries):
    """The pure module cannot build a block; it says so instead of pretending."""
    block_state = state(draft)
    missed = event(
        block_state,
        [1],
        frm=golden.START,
        today=golden.START + dt.timedelta(days=15),
    )
    result = propose(missed, block_state, ctx(golden_entries, missed.today), CFG)
    assert result.tier == "restart"
    assert not result.ok
    assert "needs a new block" in result.failure.reason


def test_an_injury_restart_says_to_see_a_physio(draft, golden_entries):
    """Spec 08 §11, and the one reason in this module that is not about dates."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(
        block_state,
        [W3_MONDAY_SEQ],
        frm=W3_MONDAY,
        until=W3_MONDAY,
        today=W3_TUESDAY,
        injury=True,
    )
    reduced = missed_policy.restart_one_rms(block_state, CFG)
    replacement = generator.generate_block(
        golden.athlete("5.00", "nearest"),
        golden.template(),
        golden_entries,
        reduced,
        sorted(reduced),
        dt.date(2026, 1, 26),
        golden.MON_WED_FRI,
    )
    result = propose(
        missed,
        block_state,
        ctx(golden_entries, W3_TUESDAY),
        CFG,
        restart_draft=replacement,
        restart_block_id=uuid.uuid4(),
    )
    assert result.ok, result.failure
    assert any("physio" in reason for reason in result.proposals[0].reasons)


def test_a_restart_proposal_is_a_new_block_that_names_what_it_replaces(draft, golden_entries):
    """ADR-0007 keeps approval to dates and statuses, so a restart cannot be
    a reschedule: it is a different block, and the old one is retired."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(
        block_state,
        [W3_MONDAY_SEQ],
        frm=W3_MONDAY,
        until=W3_MONDAY,
        today=W3_TUESDAY,
        injury=True,
    )
    reduced = missed_policy.restart_one_rms(block_state, CFG)
    replacement = generator.generate_block(
        golden.athlete("5.00", "nearest"),
        golden.template(),
        golden_entries,
        reduced,
        sorted(reduced),
        dt.date(2026, 1, 26),
        golden.MON_WED_FRI,
    )
    new_id = uuid.uuid4()
    result = propose(
        missed,
        block_state,
        ctx(golden_entries, W3_TUESDAY),
        CFG,
        restart_draft=replacement,
        restart_block_id=new_id,
    )
    proposal = result.proposals[0]
    assert proposal.kind == "new_block"
    assert proposal.block_id == new_id
    assert proposal.payload.supersedes_block_id == block_state.block_id
    # The reduction is an assumption about this block, not a recorded max.
    assert proposal.payload.new_one_rms is None


# --- 6. Failure reporting ---------------------------------------------------


def hostile_ctx(golden_entries):
    """A context whose plate increment no weight in the block is a multiple of.

    Every option therefore fails `plate_multiple`, which is the cheapest way
    to exercise the rejection paths without constructing an impossible
    calendar.
    """
    return ctx(golden_entries, W3_TUESDAY).model_copy(
        update={"plate_increment_kg": Decimal("3.00")}
    )


def test_the_solver_s_own_gate_rejects_before_the_policy_sees_an_option(
    draft, golden_entries, caplog
):
    """`calendar_solver.solve` takes the validator as a required argument, so
    a bad option never reaches this module -- the refusal arrives as an
    `Infeasible` whose reason already names the rule."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    with caplog.at_level(logging.WARNING):
        result = propose(missed, block_state, hostile_ctx(golden_entries), CFG)

    assert not result.ok
    assert "plate_multiple" in result.failure.reason
    assert result.failure.stats is not None
    assert "dropped by the validator" in caplog.text


def test_a_dropped_fast_option_is_logged_with_the_rules_it_broke(draft, golden_entries, caplog):
    """The greedy path has no solver gate in front of it, so the drop happens
    here. A silent one would be indistinguishable from finding nothing."""
    block_state = state(draft, done=range(1, W3_MONDAY_SEQ))
    missed = event(block_state, [W3_MONDAY_SEQ], frm=W3_MONDAY, until=W3_MONDAY, today=W3_TUESDAY)
    with caplog.at_level(logging.WARNING):
        result = propose(missed, block_state, hostile_ctx(golden_entries), CFG, fast=True)

    assert not result.ok
    assert "plate_multiple" in " ".join(result.failure.dropped)
    assert "missed-policy option dropped by the validator" in caplog.text


def test_a_result_carries_proposals_or_a_failure_but_never_both():
    from domains.strength.missed_policy import PolicyResult

    with pytest.raises(ValueError, match="never both or neither"):
        PolicyResult(tier="week_shift")
    with pytest.raises(ValueError, match="never both or neither"):
        PolicyResult(tier="week_shift", proposals=(), failure=None)


# --- 7. Purity --------------------------------------------------------------


def test_the_policy_module_has_no_clock_database_or_randomness():
    """Mirrors the calendar solver's own guard.

    `today` arrives on the event. A module that reads a clock cannot be
    replayed by spec 10's simulator or called from a Temporal workflow
    (CLAUDE.md non-negotiable #6).
    """
    source = Path(missed_policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    banned = {"sqlalchemy", "sqlmodel", "random", "planner_core.db"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {alias.name for alias in node.names} & banned
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in banned

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "today", "utcnow"}
