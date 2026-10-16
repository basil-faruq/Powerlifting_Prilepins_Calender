"""Spec 07: the calendar solver places sessions on days, and nothing else.

The five behaviours spec 07 §12 names are the backbone here, and each one is
asserted on exact dates rather than "something moved": the module is pure and
seeded, so a vague assertion would be a choice to not notice a regression.

The block under test is the authored template generated with **5 kg / nearest**
and maxes of its own (squat 135, bench 92.5, deadlift 175, press 57.5, row
77.5). Those are not the workbook's example maxes -- those reach a test only
through `tests/golden.py`, so a reader can always tell which block a weight
came from (CLAUDE.md guardrail).

Layout worth keeping in mind, because every expected date follows from it:
slot 1 is Monday (squat + bench), slot 2 Wednesday (deadlift + press), slot 3
Friday (squat + bench + row). 11 weeks x 3 slots = 33 sessions, seq 1..33 in
date order from Monday 2026-01-05. So seq 7 is week 3 slot 1, Monday
2026-01-19 -- the session that gets missed below.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from domains.strength import calendar_solver
from domains.strength.calendar_solver import (
    CalendarProblem,
    CostWeights,
    Infeasible,
    ScheduleOption,
    SessionToPlace,
    cost_of,
    problem_from_plan,
    solve,
    to_changes,
    to_plan,
    validator_check,
)
from domains.strength.validator import ValidationContext, read_test_weeks, validate
from planner_core.proposal import ValidationResult, Violation
from tests import golden
from tests.golden import MON_WED_FRI, START

# This suite's own maxes. Deliberately unlike the workbook's 160/140/105/70/105.
SOLVER_MAXES = {
    "squat": Decimal("135"),
    "bench": Decimal("92.5"),
    "deadlift": Decimal("175"),
    "press": Decimal("57.5"),
    "row": Decimal("77.5"),
}

PLATE = Decimal("5.00")

# The missed-session scenario, shared by most tests below.
MISSED_SEQ = 7
MISSED_DATE = dt.date(2026, 1, 19)  # Monday, week 3 slot 1
DONE_SEQS = frozenset(range(1, MISSED_SEQ))  # weeks 1-2, all trained
TUESDAY = dt.date(2026, 1, 20)  # the day after, and `today` for those tests

# One solve per test, single-threaded: spec 07 §10's test settings. Eight
# workers would still be correct but would not be reproducible.
SOLVE_KW = {"time_limit_s": 2.0, "num_workers": 1}


@pytest.fixture(scope="module")
def draft(golden_entries):
    """The authored block at 5 kg / nearest, which is now the athlete default."""
    from domains.strength import generator

    return generator.generate_block(
        golden.athlete("5.00", "nearest"),
        golden.template(),
        golden_entries,
        SOLVER_MAXES,
        sorted(SOLVER_MAXES),
        START,
        MON_WED_FRI,
    )


@pytest.fixture(scope="module")
def plan(draft):
    return draft.sessions


def ctx(golden_entries, today: dt.date, **overrides) -> ValidationContext:
    fields = {
        "weekday_map": dict(MON_WED_FRI),
        "overreach_from_week": 7,
        "test_week": read_test_weeks(golden_entries),
        "plate_increment_kg": PLATE,
        "today": today,
    }
    fields.update(overrides)
    return ValidationContext(**fields)


def missed_problem(plan, **overrides) -> CalendarProblem:
    """Week 3's Monday never happened, and it is now Tuesday."""
    fields = {
        "today": TUESDAY,
        "weekday_map": dict(MON_WED_FRI),
        "history_seqs": DONE_SEQS,
        "allow_flex_days": True,
    }
    fields.update(overrides)
    return problem_from_plan(plan, **fields)


def date_of(plan, seq: int) -> dt.date:
    return next(session.date for session in plan if session.seq == seq)


# --- 1. The block itself ----------------------------------------------------


def test_the_five_kilo_block_is_valid_to_begin_with(plan, golden_entries):
    """Everything below rests on this: the input plan has nothing wrong with it.

    If a 5 kg / nearest block broke a rule, every "option was dropped" below
    would be measuring the fixture rather than the solver.
    """
    result = validate(plan, ctx(golden_entries, START))
    assert result.ok is True, [v.message for v in result.violations]
    assert date_of(plan, MISSED_SEQ) == MISSED_DATE
    assert len(plan) == 33


def test_every_weight_is_loadable_on_a_paired_plate_bar(plan):
    """5 kg steps, because 2.5 kg plates go on in pairs."""
    weights = [lift.weight_kg for session in plan for lift in session.lifts]
    assert weights
    assert all(weight % PLATE == 0 for weight in weights)


# --- 2. Nothing missed ------------------------------------------------------


def test_nothing_missed_gives_one_option_at_zero_cost(plan, golden_entries):
    """Spec 07 §12: the default block, nothing missed -> one option, cost 0.

    One and not three: a second option would have to move something, every
    move costs, and nothing within 3x of zero exists.
    """
    problem = problem_from_plan(plan, today=START, weekday_map=MON_WED_FRI)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, START)), **SOLVE_KW)

    assert not isinstance(options, Infeasible), getattr(options, "reason", None)
    assert len(options) == 1
    assert options[0].cost.total == 0
    assert options[0].assignments == {session.seq: session.date for session in plan}
    assert to_changes(plan, options[0]) == ()
    assert to_plan(plan, options[0]) == tuple(plan)


# --- 3. One missed session, flex allowed ------------------------------------


def test_a_missed_monday_moves_to_tuesday_when_flex_is_allowed(plan, golden_entries):
    """Spec 07 §12: the cheapest answer is the next day, if rest still holds.

    Friday's squat was three days ago, so Tuesday is two days clear -- the gap
    holds, and one day late plus one day off-weekday (1 + 3) beats shifting the
    remaining 27 sessions by a week.
    """
    problem = missed_problem(plan)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible), getattr(options, "reason", None)
    best = options[0]
    assert best.assignments[MISSED_SEQ] == TUESDAY
    assert best.cost.delay_days == 1
    assert best.cost.off_weekday_count == 1
    assert best.cost.end_slip_days == 0
    assert best.cost.total == 4

    changes = to_changes(plan, best)
    assert len(changes) == 1
    assert changes[0].seq == MISSED_SEQ
    assert changes[0].action == "move"
    assert changes[0].from_date == MISSED_DATE
    assert changes[0].to_date == TUESDAY


def test_a_rest_override_pushes_the_session_further_out(plan, golden_entries):
    """A tighter rule on one pair changes the answer, in solver and validator alike.

    Bench and deadlift two days apart rules out Tuesday-then-Wednesday, so the
    solver has to find something else. Both sides are told about the override;
    the point is that they agree.
    """
    override = {("bench", "deadlift"): 2}
    problem = missed_problem(plan, rest_min_days=override)
    checker = validator_check(plan, ctx(golden_entries, TUESDAY, rest_min_days=override))
    options = solve(problem, check=checker, **SOLVE_KW)

    assert not isinstance(options, Infeasible), getattr(options, "reason", None)
    best = options[0]
    placed = best.assignments[MISSED_SEQ]
    next_session = best.assignments[MISSED_SEQ + 1]
    assert (next_session - placed).days >= 2
    assert (
        validate(
            to_plan(plan, best),
            ctx(
                golden_entries,
                TUESDAY,
                rest_min_days=override,
                approved_off_weekday={c.seq for c in to_changes(plan, best)},
            ),
            changes=to_changes(plan, best),
        ).ok
        is True
    )


# --- 4. Flex not allowed ----------------------------------------------------


def test_without_flex_the_whole_remainder_slides_a_week(plan, golden_entries):
    """Spec 07 §12: keep every session on its home weekday and the block slips.

    The missed Monday can only be another Monday, and the earliest free one is
    a week out -- so everything behind it follows and the max test moves with
    it. That is the trade-off the two options exist to show.
    """
    problem = missed_problem(plan, allow_flex_days=False)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible), getattr(options, "reason", None)
    best = options[0]
    assert best.cost.off_weekday_count == 0
    assert best.cost.end_slip_days == 7
    assert best.assignments[MISSED_SEQ] == MISSED_DATE + dt.timedelta(days=7)

    for seq in range(MISSED_SEQ, 34):
        assert best.assignments[seq] == date_of(plan, seq) + dt.timedelta(days=7)
    for seq in DONE_SEQS:
        assert best.assignments[seq] == date_of(plan, seq)

    moved = {change.seq for change in to_changes(plan, best)}
    assert moved == set(range(MISSED_SEQ, 34))


# --- 5. Unavailable days ----------------------------------------------------


@pytest.mark.parametrize("flex", [True, False])
def test_no_session_lands_in_an_unavailable_window(plan, golden_entries, flex):
    """Spec 07 §12: ten days away, and nothing is scheduled inside them."""
    away = {dt.date(2026, 1, 12) + dt.timedelta(days=offset) for offset in range(10)}
    problem = problem_from_plan(
        plan,
        today=START,
        weekday_map=MON_WED_FRI,
        unavailable=away,
        allow_flex_days=flex,
    )
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, START)), **SOLVE_KW)

    assert not isinstance(options, Infeasible), getattr(options, "reason", None)
    for option in options:
        assert not (set(option.assignments.values()) & away)
    # Five sessions sat inside that window, so this cannot have been a no-op.
    assert options[0].cost.total > 0


# --- 6. Impossible hard end date -------------------------------------------


def test_an_impossible_meet_date_reports_the_relaxation_that_works(plan, golden_entries):
    """Spec 07 §12: `Infeasible`, naming what would have to give.

    Flex is off and the block may not finish later than planned -- which cannot
    be done, since the missed Monday can only become a later Monday. Allowing
    the session off its weekday is enough, so that is what gets reported.
    """
    problem = missed_problem(plan, allow_flex_days=False, hard_end_date=date_of(plan, 33))
    result = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert isinstance(result, Infeasible)
    assert result.relaxation == "allow_flex_days"
    assert "allow_flex_days" in result.reason
    assert result.stats is not None


def test_a_meet_date_nothing_can_meet_falls_through_to_the_next_rung(plan, golden_entries):
    """Flex is already on, so the ladder skips it and drops the date instead.

    Twenty-seven sessions cannot be fitted into ten days at any weekday, so the
    only thing left to give is the deadline itself.
    """
    problem = missed_problem(plan, hard_end_date=TUESDAY + dt.timedelta(days=10))
    result = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert isinstance(result, Infeasible)
    assert result.relaxation == "drop hard_end_date"
    assert "allow_flex_days" not in result.relaxations_tried


# --- 7. Options are distinct, sorted, and all valid -------------------------


def test_options_are_distinct_and_sorted_by_cost(plan, golden_entries):
    problem = missed_problem(plan)
    options = solve(
        problem, n_options=3, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW
    )

    assert not isinstance(options, Infeasible)
    assert len(options) >= 2, "a missed session has more than one answer"
    layouts = [tuple(sorted(option.assignments.items())) for option in options]
    assert len(set(layouts)) == len(layouts)

    totals = [option.cost.total for option in options]
    assert totals == sorted(totals)
    assert max(totals) <= calendar_solver.COST_RATIO_LIMIT * min(totals)


def test_every_returned_option_passes_the_validator(plan, golden_entries):
    """Spec 07 DoD. Checked here independently of the gate inside `solve`."""
    problem = missed_problem(plan)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    for option in options:
        changes = to_changes(plan, option)
        result = validate(
            to_plan(plan, option),
            ctx(
                golden_entries,
                TUESDAY,
                approved_off_weekday={change.seq for change in changes},
            ),
            changes=changes,
        )
        assert result.ok is True, [v.message for v in result.violations]


def test_an_option_only_ever_changes_dates(plan, golden_entries):
    """Weights, sets and reps are spec 09's business, never this module's."""
    problem = missed_problem(plan)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    before = {session.seq: session.lifts for session in plan}
    for option in options:
        for session in to_plan(plan, option):
            assert session.lifts == before[session.seq]


# --- 8. History is not rewritten -------------------------------------------


def test_a_completed_session_is_never_re_dated(plan, golden_entries):
    problem = missed_problem(plan)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    for option in options:
        for seq in DONE_SEQS:
            assert option.assignments[seq] == date_of(plan, seq)
        assert not ({change.seq for change in to_changes(plan, option)} & DONE_SEQS)


def test_a_missed_session_trapped_behind_a_trained_one_is_reported_as_order(plan, golden_entries):
    """Monday was missed, Wednesday was trained anyway, and it is now Thursday.

    No relaxation helps: Monday's session cannot be moved to before a day that
    has already happened. The block has to be reshaped (spec 08), and saying
    "infeasible, try dropping your meet date" would be a lie.
    """
    thursday = dt.date(2026, 1, 22)
    problem = problem_from_plan(
        plan,
        today=thursday,
        weekday_map=MON_WED_FRI,
        history_seqs=DONE_SEQS | {MISSED_SEQ + 1},
        allow_flex_days=True,
    )
    result = solve(problem, check=validator_check(plan, ctx(golden_entries, thursday)), **SOLVE_KW)

    assert isinstance(result, Infeasible)
    assert result.reason.startswith("order:")
    assert f"seq {MISSED_SEQ}" in result.reason


# --- 9. The validator gate --------------------------------------------------


def _rejecting(*rules: str):
    def check(option: ScheduleOption) -> ValidationResult:
        return ValidationResult.of(
            [Violation(rule=rule, message="no", refs=()) for rule in rules], []
        )

    return check


def test_an_option_the_validator_rejects_is_dropped_and_logged(plan, golden_entries, caplog):
    problem = missed_problem(plan)
    with caplog.at_level(logging.WARNING):
        result = solve(problem, check=_rejecting("rest_gap"), **SOLVE_KW)

    assert isinstance(result, Infeasible)
    assert "validator rejected every option" in result.reason
    assert "rest_gap" in result.reason
    assert "dropped by the validator" in caplog.text


def test_the_gate_keeps_the_options_it_does_not_reject(plan, golden_entries):
    """One bad option does not sink the rest."""
    seen: list[int] = []

    def check(option: ScheduleOption) -> ValidationResult:
        seen.append(option.cost.total)
        if len(seen) == 1:
            return ValidationResult.of([Violation(rule="order", message="no", refs=())], [])
        return ValidationResult.of([], [])

    options = solve(missed_problem(plan), check=check, **SOLVE_KW)
    assert not isinstance(options, Infeasible)
    assert len(options) >= 1
    assert len(seen) > 1


# --- 10. Cost and stats -----------------------------------------------------


def test_the_solver_objective_matches_the_cost_computed_in_python(plan, golden_entries):
    """The objective is stated twice -- as constraints and as `cost_of` -- so a
    mis-stated one is caught rather than silently optimised for."""
    problem = missed_problem(plan)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    for option in options:
        assert cost_of(problem, option.assignments) == option.cost
        assert option.stats.objective == option.cost.total


def test_solver_stats_are_attached_to_every_result(plan, golden_entries):
    """Spec 07 DoD, and spec 14's trace reads these."""
    problem = missed_problem(plan)
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    for option in options:
        assert option.stats.status in {"OPTIMAL", "FEASIBLE"}
        assert option.status == option.stats.status
        assert option.stats.wall_time_s >= 0


def test_the_cost_weights_order_the_options(plan, golden_entries):
    """Make an off-weekday day free and the no-flex answer stops being chosen.

    Guards against the weights being decorative: with `off_weekday` at zero the
    solver should take the next day and pay nothing for it.
    """
    problem = missed_problem(plan, weights=CostWeights(delay=1, off_weekday=0, end_slip=10))
    options = solve(problem, check=validator_check(plan, ctx(golden_entries, TUESDAY)), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    assert options[0].assignments[MISSED_SEQ] == TUESDAY
    assert options[0].cost.total == 1


def test_two_identical_runs_give_identical_schedules(plan, golden_entries):
    problem = missed_problem(plan)
    checker = validator_check(plan, ctx(golden_entries, TUESDAY))
    first = solve(problem, check=checker, **SOLVE_KW)
    second = solve(problem, check=checker, **SOLVE_KW)

    assert not isinstance(first, Infeasible)
    assert not isinstance(second, Infeasible)
    assert [o.assignments for o in first] == [o.assignments for o in second]
    assert [o.cost for o in first] == [o.cost for o in second]


# --- 11. Errors and edges ---------------------------------------------------


def test_an_empty_session_list_is_one_option_at_zero_cost():
    """Spec 07 §13."""
    problem = CalendarProblem(sessions=(), today=START, weekday_map=MON_WED_FRI)
    options = solve(problem, check=lambda option: ValidationResult.of([], []), **SOLVE_KW)

    assert not isinstance(options, Infeasible)
    assert len(options) == 1
    assert options[0].cost.total == 0
    assert options[0].assignments == {}


def test_today_after_the_horizon_is_a_value_error(plan):
    """Spec 07 §13. A setup error, not an infeasible calendar."""
    with pytest.raises(ValueError, match="after horizon_end"):
        problem_from_plan(
            plan,
            today=START,
            weekday_map=MON_WED_FRI,
            horizon_end=START - dt.timedelta(days=1),
        )


def test_a_hard_end_date_already_past_is_a_value_error(plan):
    with pytest.raises(ValueError, match="hard_end_date"):
        problem_from_plan(
            plan,
            today=TUESDAY,
            weekday_map=MON_WED_FRI,
            hard_end_date=TUESDAY - dt.timedelta(days=1),
        )


def test_a_duplicate_seq_is_a_value_error():
    session = SessionToPlace(seq=1, week=1, slot=1, lifts=frozenset({"squat"}), original_date=START)
    with pytest.raises(ValueError, match="duplicate seq"):
        CalendarProblem(
            sessions=(session, session.model_copy()),
            today=START,
            weekday_map=MON_WED_FRI,
        )


def test_a_slot_with_no_home_weekday_is_a_value_error(plan):
    with pytest.raises(ValueError, match="no home weekday"):
        problem_from_plan(plan, today=START, weekday_map={1: 0, 2: 2})


def test_a_session_with_nowhere_left_to_go_is_infeasible(plan, golden_entries):
    """Every allowed day blocked out is an answer, not a crash.

    An empty variable domain would make CP-SAT report MODEL_INVALID, which
    reads as a bug in the caller rather than "you are away for the rest of the
    block".
    """
    horizon = dt.date(2026, 2, 2)
    away = {START + dt.timedelta(days=offset) for offset in range((horizon - START).days + 1)}
    problem = problem_from_plan(
        plan,
        today=START,
        weekday_map=MON_WED_FRI,
        unavailable=away,
        horizon_end=horizon,
    )
    result = solve(problem, check=validator_check(plan, ctx(golden_entries, START)), **SOLVE_KW)

    assert isinstance(result, Infeasible)
    assert "no allowed day" in result.reason or "infeasible" in result.reason


def test_a_time_out_with_no_solution_says_so(plan, golden_entries, monkeypatch):
    """Spec 07 §13. "We ran out of time" is not "it is impossible"."""
    from ortools.sat.python import cp_model

    class _TimedOut:
        wall_time = 2.0

        class response_proto:  # noqa: N801 - mirrors the CpSolver attribute
            num_booleans = 0

        def status_name(self, status):
            return "UNKNOWN"

    monkeypatch.setattr(
        calendar_solver,
        "_solve_once",
        lambda *a, **k: (cp_model.UNKNOWN, _TimedOut()),
    )
    result = solve(
        missed_problem(plan),
        check=validator_check(plan, ctx(golden_entries, TUESDAY)),
        **SOLVE_KW,
    )

    assert isinstance(result, Infeasible)
    assert result.reason == "time limit"


# --- 12. Applying an option -------------------------------------------------


def test_to_plan_changes_only_the_dates_it_was_given(plan):
    problem = problem_from_plan(
        plan, today=TUESDAY, weekday_map=MON_WED_FRI, history_seqs=DONE_SEQS
    )
    placed = {
        session.seq: (TUESDAY if session.seq == MISSED_SEQ else session.date) for session in plan
    }
    option = ScheduleOption(
        assignments={MISSED_SEQ: TUESDAY},
        cost=cost_of(problem, placed),
        status="OPTIMAL",
        stats=calendar_solver.SolverStats(status="OPTIMAL", wall_time_s=0.0, num_booleans=0),
    )
    updated = to_plan(plan, option)

    assert date_of(updated, MISSED_SEQ) == TUESDAY
    assert len(updated) == len(plan)
    for before, after in zip(plan, updated, strict=True):
        assert after.seq == before.seq
        assert after.week == before.week
        assert after.lifts == before.lifts
        if before.seq != MISSED_SEQ:
            assert after.date == before.date


def test_to_plan_refuses_a_seq_the_plan_does_not_have(plan):
    option = ScheduleOption(
        assignments={999: TUESDAY},
        cost=calendar_solver.CostBreakdown(
            total=0, delay_days=0, off_weekday_count=0, end_slip_days=0
        ),
        status="OPTIMAL",
        stats=calendar_solver.SolverStats(status="OPTIMAL", wall_time_s=0.0, num_booleans=0),
    )
    with pytest.raises(ValueError, match="not in the plan"):
        to_plan(plan, option)


# --- 13. Purity (spec 07 §11) -----------------------------------------------


def test_the_solver_module_has_no_clock_database_or_randomness():
    source = Path(calendar_solver.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"sqlalchemy", "sqlmodel", "random", "time", "planner_core.db"}

    clock_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "today", "utcnow"}
    ]
    assert clock_calls == []
