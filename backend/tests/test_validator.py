"""Validator tests -- written before the validator, and meant to be read as its spec.

The headline is CLAUDE.md non-negotiable #5: **the author's own block must
validate with zero violations.** A rule that rejects the source template is
miscalibrated, not strict. Everything else here is one passing and one failing
case per rule, plus the edges where a plausible implementation goes wrong.

Hand-built plans use plain percentages over 100 (`70/100`) and weights that are
not the spreadsheet's example maxes, so a reader can always tell which block a
number came from (CLAUDE.md guardrail). The golden block reaches these tests
only through `tests/golden.py`.

Every test here is pure: no database, no workbook, no clock.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from domains.strength import rep_max, validator
from domains.strength.schemas import DraftLift, DraftSession, ParsedEntry, SessionChange
from domains.strength.validator import (
    StrengthValidator,
    ValidationContext,
    ValidatorConfig,
    read_test_weeks,
    validate,
)
from planner_core.proposal import ValidationResult, Violation
from tests.golden import MON_WED_FRI, START

# The authored block's test weeks (CONTEXT.md §4). Row is never tested.
GOLDEN_TEST_WEEKS = {"squat": 11, "deadlift": 11, "bench": 10, "press": 9}

# The day before the block starts: nothing in it is in the past yet.
BEFORE_START = START - dt.timedelta(days=1)


# --- Builders -------------------------------------------------------------


def _on(week: int, slot: int, weekday_map: dict[int, int] = MON_WED_FRI) -> dt.date:
    """The date the generator would give this week and slot."""
    return START + dt.timedelta(days=7 * (week - 1) + weekday_map[slot])


def _lift(
    lift_code: str = "squat",
    *,
    sets: int = 3,
    reps: int = 5,
    num: str = "70",
    den: str = "100",
    weight: str = "91",
    is_test: bool = False,
    rel: Decimal | None = None,
    variation: str | None = None,
) -> DraftLift:
    pct = Fraction(Decimal(num)) / Fraction(Decimal(den))
    return DraftLift(
        lift_code=lift_code,
        variation=variation,
        sets=sets,
        reps=reps,
        pct_num=Decimal(num),
        pct_den=Decimal(den),
        weight_kg=Decimal(weight),
        rel_intensity=rel if rel is not None else rep_max.rel_intensity(pct, reps),
        is_test=is_test,
    )


def _session(seq: int, week: int, slot: int, *lifts: DraftLift, on: dt.date | None = None):
    return DraftSession(
        week=week,
        slot=slot,
        seq=seq,
        date=on if on is not None else _on(week, slot),
        lifts=lifts or (_lift(),),
    )


def _ctx(**overrides) -> ValidationContext:
    fields = {
        "weekday_map": MON_WED_FRI,
        "overreach_from_week": 7,
        "test_week": GOLDEN_TEST_WEEKS,
        "plate_increment_kg": Decimal("1.0"),
        "today": BEFORE_START,
    }
    fields.update(overrides)
    return ValidationContext(**fields)


def _rules(findings: tuple[Violation, ...]) -> set[str]:
    return {finding.rule for finding in findings}


def _named(findings: tuple[Violation, ...], rule: str) -> list[Violation]:
    return [finding for finding in findings if finding.rule == rule]


def _swap_lift(
    sessions: tuple[DraftSession, ...],
    week: int,
    slot: int,
    lift_code: str,
    replacement: DraftLift,
) -> tuple[DraftSession, ...]:
    """The plan with one lift in one session replaced."""
    out = []
    for session in sessions:
        if (session.week, session.slot) == (week, slot):
            lifts = tuple(
                replacement if lift.lift_code == lift_code else lift for lift in session.lifts
            )
            session = session.model_copy(update={"lifts": lifts})
        out.append(session)
    return tuple(out)


def _move(sessions: tuple[DraftSession, ...], week: int, slot: int, days: int):
    """The plan with one session's date shifted by `days`."""
    return tuple(
        s.model_copy(update={"date": s.date + dt.timedelta(days=days)})
        if (s.week, s.slot) == (week, slot)
        else s
        for s in sessions
    )


def _golden_lift(golden_draft, lift_code: str, week: int, slot: int) -> DraftLift:
    session = next(s for s in golden_draft.sessions if (s.week, s.slot) == (week, slot))
    return next(lift for lift in session.lifts if lift.lift_code == lift_code)


def _seq_of(golden_draft, week: int, slot: int) -> int:
    return next(s.seq for s in golden_draft.sessions if (s.week, s.slot) == (week, slot))


# --- The golden block (CLAUDE.md #5) --------------------------------------


def test_the_test_weeks_are_read_from_the_template(golden_entries):
    assert read_test_weeks(golden_entries) == GOLDEN_TEST_WEEKS


def test_a_lift_with_two_test_weeks_is_a_malformed_template():
    def entry(week: int) -> ParsedEntry:
        return ParsedEntry(
            lift_code="squat",
            week=week,
            slot=1,
            sets=1,
            reps=1,
            pct_num=Decimal("105"),
            pct_den=Decimal("100"),
            is_test=True,
            source_sheet="B1 Squat",
            source_row=5,
        )

    with pytest.raises(ValueError, match="squat"):
        read_test_weeks([entry(9), entry(11)])


def test_the_golden_block_passes_with_zero_violations(golden_draft, golden_entries):
    result = validate(golden_draft.sessions, _ctx(test_week=read_test_weeks(golden_entries)))

    assert result.violations == ()
    assert result.ok is True


def test_the_golden_block_is_accepted_with_warnings_only(golden_draft):
    """The calibration, pinned. A change here is a decision, not a refactor.

    The author's template breaks Prilepin on purpose and lets volume and
    intensity rise together four times; all of it must stay a warning.
    """
    result = validate(golden_draft.sessions, _ctx())

    assert _rules(result.warnings) == {
        "prilepin_reps_per_set",
        "prilepin_total_reps",
        "volume_trend",
    }
    assert len(_named(result.warnings, "prilepin_reps_per_set")) == 33
    assert len(_named(result.warnings, "prilepin_total_reps")) == 34
    assert len(_named(result.warnings, "volume_trend")) == 4


def test_the_golden_blocks_volume_trend_warnings_are_the_four_known_ones(golden_draft):
    result = validate(golden_draft.sessions, _ctx())

    heads = sorted(w.message.split(":")[0] for w in _named(result.warnings, "volume_trend"))
    assert heads == [
        "press W3 to W4",
        "squat W10 to W11",
        "squat W2 to W3",
        "squat W8 to W9",
    ]


def test_the_golden_block_still_passes_halfway_through(golden_draft):
    """With today mid-block, history is still part of the plan and still fine."""
    result = validate(golden_draft.sessions, _ctx(today=_on(6, 2)))

    assert result.ok is True


def test_the_golden_weights_are_not_multiples_of_a_two_and_a_half_kilo_increment(golden_draft):
    """Weights floored to 1 kg are mostly not multiples of 2.5 -- so the rule bites."""
    result = validate(golden_draft.sessions, _ctx(plate_increment_kg=Decimal("2.5")))

    assert _rules(result.violations) == {"plate_multiple"}


# --- off_weekday ----------------------------------------------------------


def test_a_session_moved_off_its_weekday_is_rejected(golden_draft):
    plan = _move(golden_draft.sessions, 3, 1, days=1)  # Monday -> Tuesday

    result = validate(plan, _ctx())

    assert _rules(result.violations) == {"off_weekday"}
    (violation,) = result.violations
    assert violation.refs == (f"seq:{_seq_of(golden_draft, 3, 1)}",)
    assert violation.message.startswith("squat+bench W3 S1: ")
    assert "Tue" in violation.message and "Mon" in violation.message


def test_an_approved_off_weekday_session_is_accepted(golden_draft):
    plan = _move(golden_draft.sessions, 3, 1, days=1)

    result = validate(plan, _ctx(approved_off_weekday={_seq_of(golden_draft, 3, 1)}))

    assert result.ok is True


def test_a_session_in_a_slot_with_no_weekday_is_rejected():
    plan = (_session(1, 1, 1), _session(2, 1, 3, _lift("row")))

    result = validate(plan, _ctx(weekday_map={1: 0}))

    (violation,) = _named(result.violations, "off_weekday")
    assert violation.refs == ("seq:2",)
    assert "Day 3" in violation.message


def test_an_off_weekday_session_already_in_the_past_is_not_judged(golden_draft):
    plan = _move(golden_draft.sessions, 3, 1, days=1)

    result = validate(plan, _ctx(today=_on(5, 1)))

    assert "off_weekday" not in _rules(result.violations)


# --- one_per_day ----------------------------------------------------------


def test_two_sessions_on_one_date_are_rejected():
    monday = _on(1, 1)
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 2, _lift("deadlift"), on=monday),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}))

    (violation,) = _named(result.violations, "one_per_day")
    assert violation.refs == ("seq:1", "seq:2")
    assert str(monday) in violation.message


def test_sessions_on_different_dates_pass_one_per_day():
    plan = (_session(1, 1, 1, _lift("squat")), _session(2, 1, 2, _lift("deadlift")))

    assert validate(plan, _ctx()).ok is True


def test_two_sessions_on_one_past_date_are_not_judged():
    monday = _on(1, 1)
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 2, _lift("deadlift"), on=monday),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}, today=_on(2, 1)))

    assert "one_per_day" not in _rules(result.violations)


# --- order ----------------------------------------------------------------


def test_dates_out_of_seq_order_are_rejected():
    plan = (
        _session(1, 1, 2, _lift("deadlift")),  # Wednesday
        _session(2, 1, 1, _lift("squat")),  # Monday: earlier, with a later seq
    )

    result = validate(plan, _ctx())

    (violation,) = _named(result.violations, "order")
    assert violation.refs == ("seq:1", "seq:2")


def test_a_repeated_week_with_new_seqs_stays_in_order():
    """Spec §5C: a repeated week gets new seqs, so it stays valid."""
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 3, _lift("squat")),
        _session(3, 1, 1, _lift("squat"), on=_on(2, 1)),
        _session(4, 1, 3, _lift("squat"), on=_on(2, 3)),
    )

    result = validate(plan, _ctx())

    assert result.ok is True


def test_an_inversion_entirely_in_the_past_is_not_judged():
    plan = (
        _session(1, 1, 2, _lift("deadlift")),
        _session(2, 1, 1, _lift("squat")),
        _session(3, 2, 1, _lift("squat")),
    )

    result = validate(plan, _ctx(today=_on(2, 1)))

    assert "order" not in _rules(result.violations)


# --- rest_gap -------------------------------------------------------------


def test_two_squat_sessions_one_day_apart_are_rejected():
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 3, _lift("squat"), on=_on(1, 1) + dt.timedelta(days=1)),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}))

    (violation,) = _named(result.violations, "rest_gap")
    assert violation.refs == ("seq:1", "seq:2")
    assert violation.message.startswith("squat W1 S1 ")
    assert "1 day apart" in violation.message and "2 are needed" in violation.message


def test_two_squat_sessions_two_days_apart_pass():
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 3, _lift("squat"), on=_on(1, 1) + dt.timedelta(days=2)),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}))

    assert "rest_gap" not in _rules(result.violations)


def test_the_rest_gap_holds_across_the_week_boundary():
    plan = (
        _session(1, 1, 3, _lift("squat"), on=_on(1, 1) + dt.timedelta(days=6)),  # Sunday
        _session(2, 2, 1, _lift("squat")),  # Monday
    )

    result = validate(plan, _ctx(approved_off_weekday={1}))

    assert "rest_gap" in _rules(result.violations)


def test_different_lifts_one_day_apart_pass_by_default():
    """Cross-lift pairs default to 1 day -- a starting value (CONTEXT.md §9)."""
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 2, _lift("deadlift"), on=_on(1, 1) + dt.timedelta(days=1)),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}))

    assert result.ok is True


def test_a_cross_lift_rest_rule_applies_in_either_order():
    rule = {("squat", "deadlift"): 2}
    squat_first = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 2, _lift("deadlift"), on=_on(1, 1) + dt.timedelta(days=1)),
    )
    deadlift_first = (
        _session(1, 1, 1, _lift("deadlift")),
        _session(2, 1, 2, _lift("squat"), on=_on(1, 1) + dt.timedelta(days=1)),
    )

    for plan in (squat_first, deadlift_first):
        result = validate(plan, _ctx(approved_off_weekday={1, 2}, rest_min_days=rule))
        assert _rules(result.violations) == {"rest_gap"}


def test_a_back_to_back_pair_in_the_past_is_not_judged():
    """History cannot be changed by a proposal, so it cannot veto one."""
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 3, _lift("squat"), on=_on(1, 1) + dt.timedelta(days=1)),
        _session(3, 2, 1, _lift("squat")),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}, today=_on(2, 1)))

    assert result.ok is True


def test_a_back_to_back_pair_straddling_today_is_judged():
    plan = (
        _session(1, 1, 1, _lift("squat")),
        _session(2, 1, 3, _lift("squat"), on=_on(1, 1) + dt.timedelta(days=1)),
    )

    result = validate(plan, _ctx(approved_off_weekday={2}, today=_on(1, 1) + dt.timedelta(days=1)))

    assert "rest_gap" in _rules(result.violations)


def test_two_lifts_in_one_session_need_no_rest_between_them():
    plan = (_session(1, 1, 1, _lift("squat"), _lift("bench")),)

    assert validate(plan, _ctx()).ok is True


# --- pct_early ------------------------------------------------------------


def test_squat_above_one_hundred_percent_in_week_ten_passes(golden_draft):
    """Spec §12: test week 11, so week 10 is allowed."""
    w10 = _golden_lift(golden_draft, "squat", 10, 3)
    assert w10.pct == Fraction(125, 120)

    result = validate(golden_draft.sessions, _ctx())

    assert "pct_early" not in _rules(result.violations)


def test_the_same_set_moved_into_week_eight_fails_pct_early(golden_draft):
    w10 = _golden_lift(golden_draft, "squat", 10, 3)
    plan = _swap_lift(golden_draft.sessions, 8, 3, "squat", w10)

    result = validate(plan, _ctx())

    assert _rules(result.violations) == {"pct_early"}
    (violation,) = result.violations
    assert violation.message == "squat W8 S3: 104.2% is above 100% before week 10"
    assert violation.refs == (f"seq:{_seq_of(golden_draft, 8, 3)}",)


def test_a_row_set_above_one_hundred_percent_fails_pct_early():
    """Spec §12: row has no test week, so it may never exceed 100%."""
    plan = (_session(1, 8, 3, _lift("row", sets=1, reps=1, num="101", den="100")),)

    result = validate(plan, _ctx())

    (violation,) = _named(result.violations, "pct_early")
    assert violation.message.startswith("row W8 S3: 101.0% is above 100%")
    assert "no test" in violation.message


def test_exactly_one_hundred_percent_is_not_early():
    plan = (_session(1, 1, 1, _lift("squat", sets=1, reps=1, num="100", den="100")),)

    assert "pct_early" not in _rules(validate(plan, _ctx()).violations)


# --- pct_cap --------------------------------------------------------------


def test_the_squat_test_at_one_hundred_and_eight_percent_passes_the_cap(golden_draft):
    """Spec §12, and the reason the cap is 110% rather than 108% (CONTEXT.md §5)."""
    test = _golden_lift(golden_draft, "squat", 11, 1)
    assert test.is_test and test.pct == Fraction(130, 120)

    result = validate(golden_draft.sessions, _ctx())

    assert "pct_cap" not in _rules(result.violations)


def test_a_set_above_one_hundred_and_ten_percent_fails_the_cap():
    plan = (_session(1, 11, 1, _lift("squat", sets=1, reps=1, num="112.5", den="100")),)

    result = validate(plan, _ctx())

    (violation,) = _named(result.violations, "pct_cap")
    assert violation.message.startswith("squat W11 S1: 112.5% is above the 110.0% cap")


def test_exactly_one_hundred_and_ten_percent_is_within_the_cap():
    plan = (_session(1, 11, 1, _lift("squat", sets=1, reps=1, num="110", den="100")),)

    assert validate(plan, _ctx()).ok is True


# --- rel_intensity --------------------------------------------------------


def test_overreaching_before_the_overreach_week_is_rejected():
    # 88% for 5 reps: 0.88 / 0.86 = 1.0233.
    plan = (_session(1, 3, 1, _lift("squat", reps=5, num="88")),)

    result = validate(plan, _ctx())

    (violation,) = _named(result.violations, "rel_intensity")
    assert violation.message.startswith("squat W3 S1: relative intensity 1.0233")
    assert "before week 7" in violation.message


def test_the_same_overreach_from_the_overreach_week_passes():
    plan = (_session(1, 7, 1, _lift("squat", reps=5, num="88")),)

    assert validate(plan, _ctx()).ok is True


def test_relative_intensity_above_the_cap_is_rejected_in_any_week():
    # 96% for 5 reps: 0.96 / 0.86 = 1.1163.
    plan = (_session(1, 9, 1, _lift("squat", reps=5, num="96")),)

    result = validate(plan, _ctx())

    (violation,) = _named(result.violations, "rel_intensity")
    assert "above the 1.10 cap" in violation.message


def test_exactly_at_the_rep_max_is_not_overreaching():
    # 86% for 5 reps is exactly P1RM(5).
    plan = (_session(1, 1, 1, _lift("squat", reps=5, num="86")),)

    assert validate(plan, _ctx()).ok is True


def test_a_stale_cached_relative_intensity_is_not_trusted():
    """The rule recomputes from pct and reps; a solver that changed reps
    without recomputing must not slip through."""
    plan = (_session(1, 3, 1, _lift("squat", reps=5, num="88", rel=Decimal("0.5000"))),)

    result = validate(plan, _ctx())

    assert "rel_intensity" in _rules(result.violations)


# --- plate_multiple -------------------------------------------------------


def test_a_weight_off_the_plate_increment_is_rejected():
    """Spec §12: 81.5 kg with a 1.0 kg increment."""
    plan = (_session(1, 1, 1, _lift("squat", weight="81.5")),)

    result = validate(plan, _ctx())

    (violation,) = _named(result.violations, "plate_multiple")
    assert violation.message.startswith("squat W1 S1: 81.5 kg is not a multiple of")


def test_a_weight_on_the_plate_increment_passes():
    plan = (_session(1, 1, 1, _lift("squat", weight="81.5")),)

    assert validate(plan, _ctx(plate_increment_kg=Decimal("0.5"))).ok is True


# --- pain_progression -----------------------------------------------------


def _pain_plan() -> tuple[DraftSession, ...]:
    return (
        _session(1, 4, 1, _lift("squat", num="70")),
        _session(2, 4, 3, _lift("squat", num="75")),
    )


def test_progression_on_a_lift_with_a_pain_note_is_rejected():
    """Spec §12: squat in pain_lifts, next session above the last % done."""
    ctx = _ctx(pain_lifts={"squat"}, last_done_pct={"squat": Fraction(70, 100)})

    result = validate(_pain_plan(), ctx)

    (violation,) = _named(result.violations, "pain_progression")
    assert violation.refs == ("seq:2",)
    assert violation.message.startswith("squat W4 S3: 75.0% is above 70.0%")


def test_holding_the_load_on_a_lift_with_a_pain_note_passes():
    ctx = _ctx(pain_lifts={"squat"}, last_done_pct={"squat": Fraction(75, 100)})

    assert validate(_pain_plan(), ctx).ok is True


def test_progression_without_a_pain_note_passes():
    ctx = _ctx(last_done_pct={"squat": Fraction(50, 100)})

    assert validate(_pain_plan(), ctx).ok is True


def test_a_pain_note_with_nothing_done_yet_rejects_every_session_of_that_lift():
    """No baseline means any planned load is progression from nothing."""
    result = validate(_pain_plan(), _ctx(pain_lifts={"squat"}))

    violations = _named(result.violations, "pain_progression")
    assert [v.refs for v in violations] == [("seq:1",), ("seq:2",)]
    assert "no completed" in violations[0].message


def test_sessions_already_done_do_not_count_as_pain_progression():
    ctx = _ctx(
        pain_lifts={"squat"},
        last_done_pct={"squat": Fraction(70, 100)},
        today=_on(4, 3) + dt.timedelta(days=1),
    )

    assert validate(_pain_plan(), ctx).ok is True


# --- past_date ------------------------------------------------------------


def _change(action: str, to_date: dt.date | None, from_date: dt.date | None = None):
    return SessionChange(seq=1, week=1, slot=1, action=action, from_date=from_date, to_date=to_date)


@pytest.mark.parametrize("action", ["create", "move"])
def test_a_change_into_the_past_is_rejected(action):
    today = _on(1, 1)
    change = _change(action, today - dt.timedelta(days=1), from_date=today)
    plan = (_session(1, 1, 1, on=today - dt.timedelta(days=1)),)

    result = validate(plan, _ctx(today=today, approved_off_weekday={1}), changes=(change,))

    (violation,) = _named(result.violations, "past_date")
    assert violation.refs == ("seq:1",)
    assert violation.message.startswith("squat W1 S1: ")


def test_a_move_to_today_passes():
    today = _on(1, 1)
    change = _change("move", today, from_date=today - dt.timedelta(days=2))
    plan = (_session(1, 1, 1, on=today),)

    assert validate(plan, _ctx(today=today), changes=(change,)).ok is True


@pytest.mark.parametrize("action", ["skip", "repeat_week"])
def test_past_date_ignores_changes_that_do_not_place_a_session(action):
    today = _on(2, 1)
    change = _change(action, today - dt.timedelta(days=3), from_date=today - dt.timedelta(days=7))
    plan = (_session(1, 2, 1),)

    assert validate(plan, _ctx(today=today), changes=(change,)).ok is True


# --- Prilepin (warnings) --------------------------------------------------


def test_deadlift_week_one_warns_on_total_reps_and_is_not_a_violation(golden_draft):
    """Spec §12: 6x6 = 36 reps at 56%, above Prilepin's 18-30."""
    result = validate(golden_draft.sessions, _ctx())
    seq = f"seq:{_seq_of(golden_draft, 1, 2)}"

    deadlift_w1 = [w for w in result.warnings if w.message.startswith("deadlift W1 S2")]
    assert _rules(tuple(deadlift_w1)) == {"prilepin_total_reps"}
    assert deadlift_w1[0].refs == (seq,)
    assert "36" in deadlift_w1[0].message and "18-30" in deadlift_w1[0].message
    assert result.ok is True


def test_reps_per_set_outside_the_zone_warn():
    plan = (_session(1, 1, 1, _lift("squat", sets=3, reps=8, num="60")),)

    result = validate(plan, _ctx())

    assert result.ok is True
    (warning,) = _named(result.warnings, "prilepin_reps_per_set")
    assert warning.message.startswith("squat W1 S1: 8 reps per set at 60.0%")


def test_sets_and_reps_inside_the_zone_do_not_warn():
    plan = (_session(1, 1, 1, _lift("squat", sets=4, reps=5, num="60")),)

    assert validate(plan, _ctx()).warnings == ()


def test_a_test_single_is_exempt_from_prilepin():
    plan = (_session(1, 11, 1, _lift("squat", sets=1, reps=1, num="105", is_test=True)),)

    assert validate(plan, _ctx()).warnings == ()


def test_a_non_test_single_in_the_top_zone_warns_on_total_reps():
    plan = (_session(1, 7, 1, _lift("squat", sets=1, reps=1, num="95")),)

    assert _rules(validate(plan, _ctx()).warnings) == {"prilepin_total_reps"}


def test_below_fifty_five_percent_is_not_checked():
    plan = (_session(1, 1, 1, _lift("squat", sets=5, reps=10, num="50")),)

    assert validate(plan, _ctx()).warnings == ()


@pytest.mark.parametrize(
    ("sets", "reps", "num", "expected"),
    [
        # 55% sits in 55-70 (3-6 per set), not in the unchecked band below it.
        (1, 10, "55", {"prilepin_reps_per_set", "prilepin_total_reps"}),
        # 70% sits in 70-80 (total 12-24), where 15 is fine; 55-70 would warn.
        (3, 5, "70", set()),
        # 80% sits in 80-90 (2-4 per set), where 5 warns; 70-80 would accept it.
        (3, 5, "80", {"prilepin_reps_per_set"}),
        # 90% sits in 90+ (total 4-10), where 2x2 is fine; 80-90 would warn.
        (2, 2, "90", set()),
    ],
)
def test_each_zone_includes_its_lower_bound(sets, reps, num, expected):
    plan = (_session(1, 7, 1, _lift("squat", sets=sets, reps=reps, num=num)),)

    assert _rules(validate(plan, _ctx()).warnings) == expected


# --- volume_trend (warning) -----------------------------------------------


def test_volume_and_intensity_rising_together_warn():
    plan = (
        _session(1, 1, 1, _lift("squat", sets=3, reps=5, num="70")),
        _session(2, 2, 1, _lift("squat", sets=4, reps=5, num="75")),
    )

    result = validate(plan, _ctx())

    assert result.ok is True
    (warning,) = _named(result.warnings, "volume_trend")
    assert warning.message.startswith("squat W1 to W2: ")
    assert warning.refs == ("seq:2",)


def test_volume_rising_at_the_same_intensity_does_not_warn():
    plan = (
        _session(1, 1, 1, _lift("squat", sets=3, reps=5, num="70")),
        _session(2, 2, 1, _lift("squat", sets=4, reps=5, num="70")),
    )

    assert "volume_trend" not in _rules(validate(plan, _ctx()).warnings)


def test_intensity_rising_as_volume_falls_does_not_warn():
    plan = (
        _session(1, 1, 1, _lift("squat", sets=4, reps=5, num="70")),
        _session(2, 2, 1, _lift("squat", sets=3, reps=5, num="75")),
    )

    assert "volume_trend" not in _rules(validate(plan, _ctx()).warnings)


def test_a_repeated_week_is_its_own_training_week_for_volume():
    """Grouping by week number would double W2 and warn; a repeat is not a rise."""
    plan = (
        _session(1, 1, 1, _lift("squat", sets=3, reps=5, num="70")),
        _session(2, 2, 1, _lift("squat", sets=3, reps=5, num="75")),
        _session(3, 2, 1, _lift("squat", sets=3, reps=5, num="75"), on=_on(3, 1)),
    )

    assert "volume_trend" not in _rules(validate(plan, _ctx()).warnings)


# --- Messages -------------------------------------------------------------


def test_every_violation_names_the_lift_week_and_slot(golden_draft):
    w10 = _golden_lift(golden_draft, "squat", 10, 3)
    plan = _swap_lift(golden_draft.sessions, 8, 3, "squat", w10)
    plan = _move(plan, 3, 1, days=1)
    plan = _swap_lift(plan, 2, 2, "press", _lift("press", reps=5, num="88", weight="61.5"))

    result = validate(
        plan,
        _ctx(pain_lifts={"row"}),
        changes=(_change("move", BEFORE_START - dt.timedelta(days=1), from_date=START),),
    )

    assert len(_rules(result.violations)) >= 6
    for violation in result.violations:
        assert re.match(r"^[a-z]+(\+[a-z]+)* W\d+ S\d+", violation.message), violation.message
        assert violation.refs and all(ref.startswith("seq:") for ref in violation.refs)


# --- Error handling (spec §13) --------------------------------------------


def test_an_empty_plan_is_valid():
    result = validate((), _ctx())

    assert result == ValidationResult(ok=True, violations=(), warnings=())


def test_an_unknown_lift_in_the_plan_is_a_programming_error():
    curl = _lift("squat").model_copy(update={"lift_code": "curl"})
    plan = (_session(1, 1, 1, curl),)

    with pytest.raises(ValueError, match="curl"):
        validate(plan, _ctx())


@pytest.mark.parametrize(
    "overrides",
    [
        {"pain_lifts": {"curl"}},
        {"test_week": {"curl": 11}},
        {"last_done_pct": {"curl": Fraction(1, 2)}},
        {"rest_min_days": {("squat", "curl"): 2}},
    ],
)
def test_an_unknown_lift_in_the_context_is_a_programming_error(overrides):
    with pytest.raises(ValueError, match="curl"):
        validate((_session(1, 1, 1),), _ctx(**overrides))


def test_a_duplicate_seq_is_a_programming_error():
    plan = (_session(1, 1, 1), _session(1, 1, 3))

    with pytest.raises(ValueError, match="seq 1"):
        validate(plan, _ctx())


def test_a_plan_breaking_every_rule_returns_rather_than_raises(golden_draft):
    plan = tuple(
        s.model_copy(update={"date": START}) for s in golden_draft.sessions
    )  # everything on one day, most of it off its weekday

    result = validate(plan, _ctx(plate_increment_kg=Decimal("2.5")))

    assert result.ok is False
    assert {"one_per_day", "off_weekday", "order", "plate_multiple"} <= _rules(result.violations)


# --- Configuration and the Protocol ---------------------------------------


def test_the_default_thresholds_are_the_documented_ones():
    config = ValidatorConfig()

    assert config.pct_cap == Decimal("1.10")
    assert config.rel_intensity_cap == Decimal("1.10")
    assert config.same_lift_rest_days == 2
    assert config.cross_lift_rest_days == 1


def test_a_stricter_config_is_honoured():
    plan = (_session(1, 11, 1, _lift("squat", sets=1, reps=1, num="108", den="100")),)

    result = validate(plan, _ctx(), config=ValidatorConfig(pct_cap=Decimal("1.05")))

    assert "pct_cap" in _rules(result.violations)


def test_the_strength_validator_reads_the_plan_and_changes_from_a_payload(golden_draft):
    from domains.strength.schemas import StrengthPayload

    change = _change("move", BEFORE_START - dt.timedelta(days=1), from_date=START)
    payload = StrengthPayload(changes=(change,), plan=golden_draft.sessions)

    result = StrengthValidator().validate(payload, _ctx())

    assert _rules(result.violations) == {"past_date"}


# --- Purity (spec §11) ----------------------------------------------------


def test_the_validator_module_has_no_clock_database_or_randomness():
    source = Path(validator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"sqlalchemy", "sqlmodel", "random", "planner_core.db"}

    clock_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "today", "utcnow"}
    ]
    assert clock_calls == []
