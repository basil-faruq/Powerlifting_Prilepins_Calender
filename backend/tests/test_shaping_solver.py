"""Spec 09, as amended: shaping a block the template does not cover.

The load-bearing test is the first one. A soft-constrained model that cannot
reproduce its own reference is not bending the template, it is replacing it --
so `test_the_default_eleven_weeks_reproduces_the_template` and its weight
counterpart are what every other choice in the module is justified by.

Two amendments to spec 09 are tested here as behaviour rather than argued in
prose: bands and ramp peaks are per `(lift, slot)` because the author's own
squat slot 3 breaks the per-lift version, and `rep_dev` exists because
without it the solver rewrites 4x8 as 8x4.

Pure: the reference is the committed fixture, no Postgres. Seeded and
single-threaded, so a failure is reproducible.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from domains.strength import shaping_solver
from domains.strength.shaping_solver import (
    ShapingInfeasible,
    cost_of,
    problem_from_reference,
    resample_reference,
    rescale_overreach,
    solve,
    to_draft,
    validator_check,
)
from domains.strength.validator import ValidationContext, read_test_weeks, validate
from tests import golden

# Single-threaded and seeded, mirroring `test_calendar_solver.SOLVE_KW`. The
# reference is fed to CP-SAT as a hint, so even the slowest case here lands
# well inside spec 09's ten-second target.
SOLVE_KW = {"seed": 0, "num_workers": 1, "time_limit_s": 25.0}

TEMPLATE_WEEKS = 11
OVERREACH = 7

# The authored test singles, as CONTEXT.md §4 records them.
AUTHORED_TESTS = {"squat": 11, "bench": 10, "press": 9, "deadlift": 11}

# Each slot's own peak week. Squat slot 3 peaks in week 10 and then deloads to
# 50% in week 11 -- the case that makes spec 09's "ramp to the lift's test
# week" impossible.
AUTHORED_PEAKS = {
    ("squat", 1): 11,
    ("squat", 3): 10,
    ("bench", 1): 10,
    ("bench", 3): 10,
    ("deadlift", 2): 11,
    ("press", 2): 9,
    ("row", 3): 7,
}


@pytest.fixture(scope="module")
def reference(golden_entries):
    return resample_reference(golden_entries, TEMPLATE_WEEKS, template_weeks=TEMPLATE_WEEKS)


@pytest.fixture(scope="module")
def problem(reference):
    return problem_from_reference(reference, weeks=TEMPLATE_WEEKS, overreach_from_week=OVERREACH)


def context(golden_entries, *, test_week=None, overreach=OVERREACH, increment="1.0"):
    return ValidationContext(
        weekday_map=golden.MON_WED_FRI,
        overreach_from_week=overreach,
        test_week=test_week if test_week is not None else read_test_weeks(golden_entries),
        plate_increment_kg=Decimal(increment),
        today=golden.START,
    )


def gate(golden_maxes, ctx, *, increment="1.0", rounding="floor"):
    return validator_check(
        golden.athlete(increment, rounding),
        golden.template(),
        golden_maxes,
        sorted(golden_maxes),
        golden.START,
        golden.MON_WED_FRI,
        ctx,
    )


def shaped(golden_entries, golden_maxes, *, weeks, test_week=None, increment="1.0"):
    """Resample, derive, solve. The whole path a route would take."""
    reference = resample_reference(golden_entries, weeks, template_weeks=TEMPLATE_WEEKS)
    overreach = rescale_overreach(OVERREACH, weeks, TEMPLATE_WEEKS)
    prob = problem_from_reference(
        reference, weeks=weeks, overreach_from_week=overreach, test_week=test_week
    )
    ctx = context(
        golden_entries, test_week=prob.test_week, overreach=overreach, increment=increment
    )
    result = solve(prob, check=gate(golden_maxes, ctx, increment=increment), **SOLVE_KW)
    return prob, result


def template_pct(entry) -> Fraction:
    return Fraction(entry.pct_num) / Fraction(entry.pct_den)


# --- 1. Resampling ----------------------------------------------------------


def test_resampling_to_the_same_length_is_the_identity(golden_entries, reference):
    """Not by a short-circuit: `pos` lands on `w` and the fraction is zero.

    A short-circuit would let a resampler bug hide from the golden test
    below, which is the only test that would otherwise catch it.
    """
    by_key = {(e.lift_code, e.week, e.slot): e for e in golden_entries}
    assert len(reference) == len(golden_entries)
    for entry in reference:
        source = by_key[(entry.lift_code, entry.week, entry.slot)]
        assert entry.exact is True
        assert (entry.sets, entry.reps) == (source.sets, source.reps)
        assert entry.is_test == source.is_test
        # Within one tenth-point, which is spec 09 §12's own tolerance.
        assert abs(entry.p - template_pct(source) * 1000) <= 1


def test_resampling_keeps_the_first_and_last_week_verbatim(golden_entries):
    """`pos` is exactly 1 and exactly T at the endpoints, however far the
    block is stretched -- so week 1's opening and the final deload survive."""
    for weeks in (6, 8, 14):
        reference = resample_reference(golden_entries, weeks, template_weeks=TEMPLATE_WEEKS)
        by_key = {(e.lift_code, e.week, e.slot): e for e in golden_entries}
        for entry in reference:
            if entry.week in (1, weeks):
                source = by_key[
                    (entry.lift_code, 1 if entry.week == 1 else TEMPLATE_WEEKS, entry.slot)
                ]
                assert abs(entry.p - template_pct(source) * 1000) <= 1, (weeks, entry)


def test_squat_slot_three_keeps_its_final_deload(golden_entries):
    """The entry spec 09's monotonic ramp would have forbidden: 104.2% in
    week 10 down to 50.0% in week 11."""
    reference = resample_reference(golden_entries, TEMPLATE_WEEKS, template_weeks=TEMPLATE_WEEKS)
    slot3 = sorted(
        (e for e in reference if e.lift_code == "squat" and e.slot == 3),
        key=lambda e: e.week,
    )
    assert slot3[-2].p == 1042
    assert slot3[-1].p == 500


@pytest.mark.parametrize("weeks", [6, 8, 9, 11, 14])
def test_every_lift_keeps_its_test_at_any_length(golden_entries, weeks):
    """A shortened block that loses its tests ends with nothing measured.

    At eight weeks no new week lands on the template's week 10, so bench's
    test has to be carried onto the nearest one rather than dropped.
    """
    reference = resample_reference(golden_entries, weeks, template_weeks=TEMPLATE_WEEKS)
    tests = {}
    for entry in reference:
        if entry.is_test:
            tests.setdefault(entry.lift_code, []).append(entry.week)
    assert sorted(tests) == sorted(AUTHORED_TESTS)
    for lift, weeks_found in tests.items():
        assert len(weeks_found) == 1, (lift, weeks_found)
    assert "row" not in tests


# --- 2. Derivation ----------------------------------------------------------


def test_the_derived_test_weeks_match_the_authored_ones(problem):
    assert problem.test_week == AUTHORED_TESTS


def test_each_slot_peaks_on_its_own_week_not_its_lift_s_test_week(problem):
    """The amendment, stated as data. Squat tests in week 11 on slot 1 while
    slot 3 peaks in week 10; row has no test at all and peaks in week 7."""
    assert problem.ramp_end == AUTHORED_PEAKS


def test_row_gets_a_peak_but_never_a_test(problem):
    assert ("row", 3) in problem.ramp_end
    assert "row" not in problem.test_week


def test_the_week_one_band_is_per_slot_because_squat_breaks_the_per_lift_one(problem):
    """Spec 09's 0.55-0.60 band excludes squat slot 3, which opens at 0.625."""
    low_1, high_1 = problem.week1_band[("squat", 1)]
    low_3, high_3 = problem.week1_band[("squat", 3)]
    assert low_1 <= 583 <= high_1
    assert low_3 <= 625 <= high_3
    assert not low_1 <= 625 <= high_1


@pytest.mark.parametrize(("weeks", "expected"), [(11, 7), (8, 6), (14, 9), (6, 4)])
def test_the_overreach_week_is_rescaled_with_the_block(weeks, expected):
    """Identity at eleven weeks; an eight-week block overreaches from six."""
    assert rescale_overreach(OVERREACH, weeks, TEMPLATE_WEEKS) == expected


# --- 3. The golden reproduction --------------------------------------------


@pytest.fixture(scope="module")
def default_block(golden_entries, golden_maxes, problem):
    ctx = context(golden_entries)
    result = solve(problem, check=gate(golden_maxes, ctx), **SOLVE_KW)
    assert not isinstance(result, ShapingInfeasible), getattr(result, "reason", None)
    return result


def test_the_default_eleven_weeks_reproduces_the_template(default_block, problem):
    """Spec 09 §12's golden test, and the justification for every weight in
    the objective: with the template as reference, the template is optimal."""
    assert default_block.status == "OPTIMAL"
    assert default_block.cost.pct_dev == 0
    assert default_block.cost.vol_dev == 0
    assert default_block.cost.rep_dev == 0

    by_key = {(e.lift_code, e.week, e.slot): e for e in problem.reference}
    for entry in default_block.entries:
        source = by_key[(entry.lift_code, entry.week, entry.slot)]
        assert (entry.p, entry.sets, entry.reps) == (source.p, source.sets, source.reps)


def test_the_default_block_reproduces_all_seventy_seven_weights(
    default_block, golden_rows, golden_maxes
):
    """The test the authored-fraction passthrough exists for.

    `583/1000` and `70/120` are the same intensity to four decimals and
    different kilograms once floored, so a shaped block that re-expressed
    every intensity in tenths would drift off the oracle. An entry the
    solver did not move keeps the pair the athlete wrote.
    """
    draft = to_draft(
        default_block,
        golden.athlete("1.0", "floor"),
        golden.template(),
        golden_maxes,
        sorted(golden_maxes),
        golden.START,
        golden.MON_WED_FRI,
    )
    produced = {
        (lift.lift_code, session.week, session.slot): lift.weight_kg
        for session in draft.sessions
        for lift in session.lifts
    }
    expected = {
        (row["lift"], row["week"], row["slot"]): Decimal(row["weight_kg"]) for row in golden_rows
    }
    assert produced == expected


def test_the_default_block_keeps_the_authored_fractions(default_block, golden_entries):
    by_key = {(e.lift_code, e.week, e.slot): e for e in golden_entries}
    for entry in default_block.entries:
        source = by_key[(entry.lift_code, entry.week, entry.slot)]
        assert (entry.pct_num, entry.pct_den) == (source.pct_num, source.pct_den)


def test_sets_and_reps_are_not_refactored(default_block):
    """The regression guard for the defect `rep_dev` was added to fix.

    Without that term the solver reproduced every intensity and every volume
    while rewriting bench week 1 from 4x8 to 8x4 -- same 32 reps, different
    session, and spec 09's "every sets/reps equal" quietly unmet.
    """
    bench = next(
        e for e in default_block.entries if e.lift_code == "bench" and e.week == 1 and e.slot == 1
    )
    assert (bench.sets, bench.reps) == (4, 8)

    deadlift = next(e for e in default_block.entries if e.lift_code == "deadlift" and e.week == 1)
    assert (deadlift.sets, deadlift.reps) == (6, 6)


def test_the_template_wins_despite_breaking_prilepin(default_block, problem):
    """Spec 09 §10: deadlift week 1 is 6x6 = 36 reps at 56%, well outside
    Prilepin's 18-30. The solver keeps it, because moving it would cost ten
    times what it saves."""
    assert default_block.cost.prilepin > 0
    assert default_block.cost.pct_dev == 0 and default_block.cost.vol_dev == 0


def test_the_objective_matches_the_cost_computed_in_python(default_block, problem):
    """Stated twice so a mis-written objective cannot hide behind a solver
    that optimises it faithfully."""
    assert cost_of(problem, default_block.entries).total == default_block.cost.total
    assert default_block.stats.objective == default_block.cost.total


def test_the_default_block_passes_the_validator(default_block, golden_entries, golden_maxes):
    draft = to_draft(
        default_block,
        golden.athlete("1.0", "floor"),
        golden.template(),
        golden_maxes,
        sorted(golden_maxes),
        golden.START,
        golden.MON_WED_FRI,
    )
    verdict = validate(draft.sessions, context(golden_entries))
    assert verdict.ok, [v.message for v in verdict.violations]


# --- 4. Shaping to a different length ---------------------------------------


@pytest.fixture(scope="module")
def eight_weeks(golden_entries, golden_maxes):
    prob, result = shaped(golden_entries, golden_maxes, weeks=8)
    assert not isinstance(result, ShapingInfeasible), getattr(result, "reason", None)
    return prob, result


def test_an_eight_week_block_is_whole_and_valid(eight_weeks, golden_entries, golden_maxes):
    prob, block = eight_weeks
    assert block.weeks == 8
    assert {e.week for e in block.entries} == set(range(1, 9))
    draft = to_draft(
        block,
        golden.athlete("1.0", "floor"),
        golden.template(),
        golden_maxes,
        sorted(golden_maxes),
        golden.START,
        golden.MON_WED_FRI,
    )
    verdict = validate(
        draft.sessions,
        context(golden_entries, test_week=prob.test_week, overreach=prob.overreach_from_week),
    )
    assert verdict.ok, [v.message for v in verdict.violations]


def test_an_eight_week_block_still_tests_every_tested_lift(eight_weeks):
    _, block = eight_weeks
    tested = {e.lift_code for e in block.entries if e.is_test}
    assert tested == set(AUTHORED_TESTS)


def test_an_eight_week_block_says_it_is_eight_weeks_long(eight_weeks, golden_maxes):
    """`template_weeks` means weeks in *this* block: `next_block_one_rms`
    reads it as a look-back cutoff, so eleven would narrow the window to one
    week."""
    _, block = eight_weeks
    draft = to_draft(
        block,
        golden.athlete("1.0", "floor"),
        golden.template(),
        golden_maxes,
        sorted(golden_maxes),
        golden.START,
        golden.MON_WED_FRI,
    )
    assert draft.template_weeks == 8


def test_a_moved_test_week_moves_the_test(golden_entries, golden_maxes):
    """Spec 09 §12: press peaks in week 11 instead of week 9."""
    prob, block = shaped(golden_entries, golden_maxes, weeks=11, test_week={"press": 11})
    assert not isinstance(block, ShapingInfeasible), getattr(block, "reason", None)

    press_tests = [(e.week, e.slot) for e in block.entries if e.lift_code == "press" and e.is_test]
    assert press_tests == [(11, 2)]
    assert prob.test_week["press"] == 11
    assert prob.ramp_end[("press", 2)] == 11
    # The other lifts are untouched.
    assert prob.test_week["squat"] == 11 and prob.test_week["bench"] == 10


def test_a_moved_test_week_pulls_the_old_peak_back_under_a_hundred_percent(
    golden_entries, golden_maxes
):
    """Week 9 is no longer the week before a test, so `pct_early` applies to
    it -- and the solver respects that rather than leaving the validator to
    refuse the block."""
    _, block = shaped(golden_entries, golden_maxes, weeks=11, test_week={"press": 11})
    week9 = next(e for e in block.entries if e.lift_code == "press" and e.week == 9)
    assert week9.is_test is False
    assert week9.p <= 1000


# --- 5. Hard constraints ----------------------------------------------------


def test_intensity_never_dips_while_a_slot_is_still_climbing(eight_weeks):
    prob, block = eight_weeks
    by_slot: dict[tuple[str, int], list] = {}
    for entry in block.entries:
        by_slot.setdefault((entry.lift_code, entry.slot), []).append(entry)
    for key, rows in by_slot.items():
        rows.sort(key=lambda entry: entry.week)
        end = prob.ramp_end[key]
        for earlier, later in zip(rows, rows[1:], strict=False):
            if later.week <= end:
                assert earlier.p <= later.p, (key, earlier.week, later.week)


def test_a_test_entry_is_one_rep_at_a_maximum(default_block):
    for entry in default_block.entries:
        if entry.is_test:
            assert (entry.sets, entry.reps) == (1, 1)
            assert entry.p >= 1000


def test_nothing_exceeds_the_hundred_and_ten_percent_cap(default_block, eight_weeks):
    _, eight = eight_weeks
    for block in (default_block, eight):
        assert all(entry.p <= 1100 for entry in block.entries)


def test_week_one_opens_inside_its_band(default_block, problem):
    for entry in default_block.entries:
        if entry.week == 1:
            low, high = problem.week1_band[(entry.lift_code, entry.slot)]
            assert low <= entry.p <= high


def test_an_untested_lift_never_goes_above_a_hundred_percent(default_block, eight_weeks):
    """Row has no test, so `pct_early` forbids it from ever exceeding 100%.

    Spec 09 omits this rule; without it the solver can hand back a block the
    validator then refuses.
    """
    _, eight = eight_weeks
    for block in (default_block, eight):
        assert all(e.p <= 1000 for e in block.entries if e.lift_code == "row")


def test_nothing_passes_a_hundred_percent_before_the_week_of_its_test(eight_weeks):
    prob, block = eight_weeks
    for entry in block.entries:
        if entry.p > 1000:
            test = prob.test_week.get(entry.lift_code)
            assert test is not None, entry
            assert entry.week >= test - 1, entry


def test_relative_intensity_of_exactly_one_is_allowed_before_the_overreach_week(
    default_block, problem
):
    """The inclusivity that the author's own block depends on.

    Press week 5 is 63/70 for three reps -- a relative intensity of exactly
    1.0000. A strict `< 1.0` bound would reject the source template, which
    CLAUDE.md non-negotiable #5 forbids.
    """
    from domains.strength import rep_max

    press = next(e for e in default_block.entries if e.lift_code == "press" and e.week == 5)
    assert press.week < problem.overreach_from_week
    assert rep_max.rel_intensity(Fraction(press.p, 1000), press.reps) == Decimal("1.0000")


def test_the_jump_rule_is_soft_because_the_template_breaks_it(default_block):
    """Six week-on-week rises in the author's own block exceed 7.5 points.
    A hard rule here would reject the source material."""
    assert default_block.cost.jump > 0


# --- 6. Refusals ------------------------------------------------------------


def test_an_impossible_week_one_band_is_reported_not_raised(golden_entries, golden_maxes):
    """Spec 09's own example (six weeks with a steep ramp) is not actually
    infeasible -- the ramp is `>=` and `jump` is soft -- so the refusal is
    tested with a genuine contradiction instead: a week-1 band above 100%
    against `pct_early`, which caps week 1 at 100% for a lift whose test is
    ten weeks away.
    """
    reference = resample_reference(golden_entries, TEMPLATE_WEEKS, template_weeks=TEMPLATE_WEEKS)
    prob = problem_from_reference(reference, weeks=TEMPLATE_WEEKS, overreach_from_week=OVERREACH)
    impossible = prob.model_copy(
        update={"week1_band": {**prob.week1_band, ("squat", 1): (1050, 1100)}}
    )
    ctx = context(golden_entries)
    result = solve(impossible, check=gate(golden_maxes, ctx), **SOLVE_KW)

    assert isinstance(result, ShapingInfeasible)
    assert "infeasible" in result.reason
    assert result.stats is not None


def test_a_timed_out_solve_is_reported_as_such(monkeypatch, golden_entries, golden_maxes, problem):
    """`_solve_once` is a seam, as the calendar solver's is, so the time-out
    path can be exercised without waiting for one."""
    from ortools.sat.python import cp_model

    class _TimedOut:
        wall_time = 0.0
        status_name = staticmethod(lambda status: "UNKNOWN")
        objective_value = 0.0

        class response_proto:  # noqa: N801 - mirrors the CP-SAT attribute
            num_booleans = 0

    monkeypatch.setattr(
        shaping_solver, "_solve_once", lambda *a, **k: (cp_model.UNKNOWN, _TimedOut())
    )
    ctx = context(golden_entries)
    result = solve(problem, check=gate(golden_maxes, ctx), **SOLVE_KW)
    assert isinstance(result, ShapingInfeasible)
    assert result.reason == "time limit"


def test_a_block_the_validator_refuses_is_not_returned(golden_entries, golden_maxes, problem):
    """Spec 09 §11: every result goes through the gate, and a refused block
    is not handed back.

    The weights are rounded to 1 kg but judged against a 2.5 kg increment --
    the same mismatch `test_validator` uses to make the authored block fail
    `plate_multiple`. Giving the athlete the hostile increment instead would
    prove nothing, because `round_weight` would simply satisfy it.
    """
    ctx = context(golden_entries, increment="2.5")
    result = solve(problem, check=gate(golden_maxes, ctx, increment="1.0"), **SOLVE_KW)
    assert isinstance(result, ShapingInfeasible)
    assert "plate_multiple" in result.reason


@pytest.mark.parametrize("weeks", [5, 15])
def test_a_week_count_outside_the_allowed_range_is_refused(golden_entries, weeks):
    reference = resample_reference(golden_entries, weeks, template_weeks=TEMPLATE_WEEKS)
    with pytest.raises(ValueError):
        problem_from_reference(reference, weeks=weeks, overreach_from_week=OVERREACH)


def test_a_test_week_naming_an_absent_lift_is_refused(reference):
    with pytest.raises(ValueError, match="no slot"):
        problem_from_reference(
            reference,
            weeks=TEMPLATE_WEEKS,
            overreach_from_week=OVERREACH,
            test_week={"bicep_curl": 4},
        )


def test_a_reference_that_disagrees_with_the_week_count_is_refused(reference):
    with pytest.raises(ValueError, match="runs to week"):
        problem_from_reference(reference, weeks=9, overreach_from_week=OVERREACH)


def test_the_gate_cannot_be_omitted(problem):
    with pytest.raises(TypeError):
        solve(problem)  # type: ignore[call-arg]


# --- 7. Determinism and purity ----------------------------------------------


def test_two_identical_runs_give_identical_blocks(golden_entries, golden_maxes, problem):
    ctx = context(golden_entries)
    first = solve(problem, check=gate(golden_maxes, ctx), **SOLVE_KW)
    second = solve(problem, check=gate(golden_maxes, ctx), **SOLVE_KW)
    assert first.entries == second.entries
    assert first.cost == second.cost


def test_solver_stats_are_returned_for_tracing(default_block):
    """Spec 09 §14's last item: spec 14 traces this call."""
    stats = default_block.stats
    assert stats.status == "OPTIMAL"
    assert stats.objective == default_block.cost.total
    assert stats.wall_time_s > 0
    assert stats.num_booleans > 0


def test_the_shaping_module_has_no_clock_database_or_randomness():
    source = Path(shaping_solver.__file__).read_text(encoding="utf-8")
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
