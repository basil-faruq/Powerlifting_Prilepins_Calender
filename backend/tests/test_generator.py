"""Block generator tests.

The golden test builds its template entries from the **committed fixture**
(via `tests/golden.py`, shared with the validator suite)
rather than from the gitignored workbook, so it runs on a fresh clone -- which
matters, because CLAUDE.md non-negotiable #4 makes it the project's single
strongest correctness claim and a skipped test proves nothing. Each fixture row
carries the authored intensity pair and the example max Excel multiplied by, so
the file is a complete (inputs -> expected output) oracle.
`test_importer.py::test_the_golden_fixture_agrees_with_the_workbook_on_every_entry`
is what keeps that substitution honest whenever the real file is present.

The example maxes (160/140/105/70/105) appear **only** through that fixture, as
`one_rm_kg`. Every other test uses different numbers, so a reader can always
tell which block a weight came from (CLAUDE.md guardrail).

Database tests request `db_session` and skip when Postgres is unreachable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from domains.strength import generator, models
from domains.strength.schemas import (
    BlockDraft,
    DraftLift,
    DraftSession,
    LoggedLift,
    LoggedSession,
    ParsedEntry,
)
from tests.golden import MON_WED_FRI, START
from tests.golden import athlete as _athlete
from tests.golden import template as _template

# Spec §12's documented spot checks: (lift, week, slot, sets, reps, weight).
SPOT_CHECKS = [
    ("squat", 1, 1, 3, 10, "81.00"),
    ("squat", 11, 1, 1, 1, "151.00"),
    ("bench", 10, 3, 1, 1, "112.00"),
    ("press", 9, 2, 1, 1, "72.00"),
]

# Maxes for the tests that are not the golden one. Deliberately not the
# spreadsheet's examples, and not the dev seeder's defaults either.
MAXES = {
    "squat": Decimal("132.5"),
    "bench": Decimal("97.5"),
    "deadlift": Decimal("155"),
    "press": Decimal("62.5"),
    "row": Decimal("87.5"),
}


def _entry(
    lift_code: str = "squat",
    *,
    week: int = 1,
    slot: int = 1,
    sets: int = 3,
    reps: int = 10,
    num: str = "70",
    den: str = "120",
    variation: str | None = None,
    is_test: bool = False,
) -> ParsedEntry:
    return ParsedEntry(
        lift_code=lift_code,
        variation=variation,
        week=week,
        slot=slot,
        sets=sets,
        reps=reps,
        pct_num=Decimal(num),
        pct_den=Decimal(den),
        is_test=is_test,
        source_sheet=f"B1 {lift_code.title()}",
        source_row=5,
    )


def _generate(entries, *, lifts=None, maxes=None, athlete=None, weekday_map=None, start=START):
    return generator.generate_block(
        athlete or _athlete(),
        _template(),
        entries,
        maxes if maxes is not None else MAXES,
        lifts if lifts is not None else sorted(MAXES),
        start,
        weekday_map or MON_WED_FRI,
    )


def _lift_at(draft: BlockDraft, lift: str, week: int, slot: int) -> DraftLift:
    session = next(s for s in draft.sessions if (s.week, s.slot) == (week, slot))
    return next(lift_row for lift_row in session.lifts if lift_row.lift_code == lift)


# --- The golden test ------------------------------------------------------


def test_the_golden_block_has_thirty_three_sessions(golden_draft):
    assert len(golden_draft.sessions) == 33


def test_the_golden_block_has_seventy_seven_planned_lifts(golden_draft):
    assert sum(len(session.lifts) for session in golden_draft.sessions) == 77


def test_the_golden_block_reproduces_every_one_of_the_seventy_seven_weights(
    golden_draft, golden_rows
):
    """CLAUDE.md non-negotiable #4, and the point of this spec.

    Excel computed these weights with `FLOOR($D$1*F5,1)` years before this
    code existed, which is what makes the fixture evidence rather than a
    restatement of the implementation. Not to be weakened for a refactor.
    """
    produced = {
        (lift.lift_code, session.week, session.slot): lift.weight_kg
        for session in golden_draft.sessions
        for lift in session.lifts
    }
    expected = {
        (row["lift"], row["week"], row["slot"]): Decimal(row["weight_kg"]) for row in golden_rows
    }
    assert produced == expected


@pytest.mark.parametrize(("lift", "week", "slot", "sets", "reps", "weight"), SPOT_CHECKS)
def test_the_golden_block_matches_the_documented_spot_checks(
    golden_draft, lift, week, slot, sets, reps, weight
):
    """Spec §12's worked examples, asserted on sets and reps as well as kg."""
    row = _lift_at(golden_draft, lift, week, slot)
    assert (row.sets, row.reps) == (sets, reps)
    assert row.weight_kg == Decimal(weight)


def test_the_golden_block_marks_the_four_test_singles(golden_draft):
    tests = {
        (lift.lift_code, session.week, session.slot)
        for session in golden_draft.sessions
        for lift in session.lifts
        if lift.is_test
    }
    assert tests == {("deadlift", 11, 2), ("squat", 11, 1), ("bench", 10, 3), ("press", 9, 2)}


def test_the_golden_block_carries_the_authored_pairs_through_unreduced(golden_draft):
    """`70/120` reaches the draft as 70 and 120, so it can be shown back."""
    squat = _lift_at(golden_draft, "squat", 1, 1)
    assert (squat.pct_num, squat.pct_den) == (Decimal("70"), Decimal("120"))


def test_the_golden_blocks_relative_intensities_are_all_plausible(golden_draft):
    """A sanity bound, not a validator: spec 05 owns the real rules."""
    intensities = [
        lift.rel_intensity for session in golden_draft.sessions for lift in session.lifts
    ]
    assert all(Decimal("0.4") < value < Decimal("1.2") for value in intensities)


def test_only_week_eleven_and_ten_overreach_in_the_golden_block(golden_draft):
    """Above 1.0 is overreaching, which the template permits from week 7."""
    overreaching = {
        session.week
        for session in golden_draft.sessions
        for lift in session.lifts
        if lift.rel_intensity > 1
    }
    assert overreaching and min(overreaching) >= 7


def test_the_golden_block_snapshots_the_maxes_it_was_computed_from(golden_draft, golden_maxes):
    assert golden_draft.one_rm_snapshot == golden_maxes


def test_a_larger_plate_increment_rounds_the_golden_weights_down(golden_entries, golden_maxes):
    """Spec §12: "With increment 2.5 / floor: squat W1 Day 1 = 80 kg"."""
    draft = generator.generate_block(
        _athlete("2.5", "floor"),
        _template(),
        golden_entries,
        golden_maxes,
        ["squat"],
        START,
        MON_WED_FRI,
    )
    assert _lift_at(draft, "squat", 1, 1).weight_kg == Decimal("80.00")


# --- Dates and ordering ---------------------------------------------------


def test_the_first_session_falls_on_the_start_date(golden_draft):
    assert golden_draft.sessions[0].date == START


def test_every_session_date_follows_the_weekday_map(golden_draft):
    for session in golden_draft.sessions:
        offset = 7 * (session.week - 1) + MON_WED_FRI[session.slot]
        assert session.date == START + dt.timedelta(days=offset)


def test_sessions_are_sequenced_one_to_n_in_date_order(golden_draft):
    assert [session.seq for session in golden_draft.sessions] == list(range(1, 34))
    dates = [session.date for session in golden_draft.sessions]
    assert dates == sorted(dates)


def test_every_session_lands_on_its_slots_weekday(golden_draft):
    """0 = Monday here, as everywhere in Python (ADR-0002 rule 4)."""
    for session in golden_draft.sessions:
        assert session.date.weekday() == MON_WED_FRI[session.slot]


def test_the_block_spans_eleven_weeks(golden_draft):
    assert golden_draft.sessions[-1].date == START + dt.timedelta(days=7 * 10 + 4)


def test_lifts_within_a_session_are_ordered_deterministically(golden_draft):
    """Day 3 holds squat, bench and row; the order is the canonical lift order."""
    day_three = next(s for s in golden_draft.sessions if (s.week, s.slot) == (1, 3))
    assert [lift.lift_code for lift in day_three.lifts] == ["squat", "bench", "row"]


# --- Choosing a subset of lifts ------------------------------------------


def test_choosing_squat_and_deadlift_keeps_all_three_days(golden_entries, golden_maxes):
    """Spec §12: squat is on Days 1 and 3, deadlift on Day 2."""
    draft = generator.generate_block(
        _athlete(),
        _template(),
        golden_entries,
        golden_maxes,
        ["squat", "deadlift"],
        START,
        MON_WED_FRI,
    )
    assert len(draft.sessions) == 33
    produced = {lift.lift_code for session in draft.sessions for lift in session.lifts}
    assert produced == {"squat", "deadlift"}


def test_choosing_deadlift_alone_gives_eleven_sessions_on_day_two(golden_entries, golden_maxes):
    draft = generator.generate_block(
        _athlete(),
        _template(),
        golden_entries,
        golden_maxes,
        ["deadlift"],
        START,
        MON_WED_FRI,
    )
    assert len(draft.sessions) == 11
    assert {session.slot for session in draft.sessions} == {2}
    assert [session.seq for session in draft.sessions] == list(range(1, 12))


def test_a_subset_does_not_change_the_remaining_lifts_numbers(
    golden_draft, golden_entries, golden_maxes
):
    """Spec §11, and the reason the snapshot holds only the chosen lifts."""
    subset = generator.generate_block(
        _athlete(),
        _template(),
        golden_entries,
        golden_maxes,
        ["squat", "deadlift"],
        START,
        MON_WED_FRI,
    )
    for session in subset.sessions:
        for lift in session.lifts:
            full = _lift_at(golden_draft, lift.lift_code, session.week, session.slot)
            assert lift.weight_kg == full.weight_kg
            assert lift.rel_intensity == full.rel_intensity


def test_the_snapshot_holds_only_the_chosen_lifts(golden_entries, golden_maxes):
    draft = generator.generate_block(
        _athlete(),
        _template(),
        golden_entries,
        golden_maxes,
        ["deadlift"],
        START,
        MON_WED_FRI,
    )
    assert draft.one_rm_snapshot == {"deadlift": golden_maxes["deadlift"]}


def test_a_lift_chosen_twice_is_counted_once(golden_entries, golden_maxes):
    draft = generator.generate_block(
        _athlete(),
        _template(),
        golden_entries,
        golden_maxes,
        ["deadlift", "deadlift"],
        START,
        MON_WED_FRI,
    )
    assert draft.lifts == ("deadlift",)


# --- Errors ---------------------------------------------------------------


@pytest.mark.parametrize("offset", [1, 2, 3, 4, 5, 6])
def test_a_start_date_that_is_not_a_monday_is_rejected(offset):
    """Spec §13's exact message. `block.start_date` has a CHECK for it too, but
    an IntegrityError is a poor way to learn this."""
    with pytest.raises(ValueError, match="start_date must be a Monday"):
        _generate([_entry()], lifts=["squat"], start=START + dt.timedelta(days=offset))


def test_a_chosen_lift_without_a_one_rm_is_rejected():
    """Spec §13: `ValueError("no 1RM for press")`."""
    entries = [_entry("squat"), _entry("press", slot=2)]
    with pytest.raises(ValueError, match="no 1RM for press"):
        _generate(entries, lifts=["squat", "press"], maxes={"squat": Decimal("132.5")})


def test_a_non_positive_one_rm_is_rejected():
    with pytest.raises(ValueError, match="must be positive"):
        _generate([_entry()], lifts=["squat"], maxes={"squat": Decimal("0")})


def test_a_weekday_map_missing_a_needed_slot_is_rejected():
    """Spec §13's third case."""
    entries = [_entry("squat"), _entry("deadlift", slot=2)]
    with pytest.raises(ValueError, match="Day 2 .* has no weekday"):
        _generate(entries, lifts=["squat", "deadlift"], weekday_map={1: 0})


def test_an_empty_lift_selection_is_rejected():
    with pytest.raises(ValueError, match="at least one lift"):
        _generate([_entry()], lifts=[])


def test_an_unknown_lift_is_rejected():
    with pytest.raises(ValueError, match="unknown lift"):
        _generate([_entry()], lifts=["bicep_curl"], maxes={"bicep_curl": Decimal("20")})


def test_a_lift_the_template_does_not_train_is_rejected():
    """Chosen, has a max, but the template has no row for it."""
    with pytest.raises(ValueError, match="no entries for row"):
        _generate([_entry("squat")], lifts=["squat", "row"])


def test_a_template_entry_beyond_the_declared_weeks_is_rejected():
    """The cross-table bound ADR-0002 rule 3 left to this spec."""
    with pytest.raises(ValueError, match="week 12 exceeds 11 weeks"):
        _generate([_entry("squat", week=12)], lifts=["squat"])


def test_a_template_entry_beyond_the_declared_slots_is_rejected():
    with pytest.raises(ValueError, match="slot 4 exceeds 3 slots"):
        _generate([_entry("squat", slot=4)], lifts=["squat"], weekday_map={4: 0})


# --- check_weekday_map ---------------------------------------------------


def _b1_slots() -> list[ParsedEntry]:
    """The authored weekly layout, one week of it."""
    return [
        _entry("squat", slot=1),
        _entry("bench", slot=1, variation="close_grip"),
        _entry("deadlift", slot=2),
        _entry("press", slot=2),
        _entry("squat", slot=3),
        _entry("bench", slot=3, variation="wide_grip"),
        _entry("row", slot=3),
    ]


def test_the_authored_layout_is_accepted():
    assert generator.check_weekday_map(MON_WED_FRI, _b1_slots(), sorted(MAXES)) == []


def test_sunday_as_day_three_is_rejected_for_squat_across_the_week_boundary():
    """Spec §12: squat on Sunday then Monday is only one day apart."""
    problems = generator.check_weekday_map({1: 0, 2: 2, 3: 6}, _b1_slots(), sorted(MAXES))
    assert any("squat" in problem and "week boundary" in problem for problem in problems)
    assert any("1 day apart" in problem for problem in problems)


def test_slots_out_of_order_are_rejected():
    """Spec §12: Day 1 = Wednesday, Day 2 = Monday."""
    problems = generator.check_weekday_map({1: 2, 2: 0}, _b1_slots(), ["squat", "deadlift"])
    assert any("must come after" in problem for problem in problems)


def test_two_slots_on_one_weekday_are_rejected():
    problems = generator.check_weekday_map({1: 0, 2: 0, 3: 4}, _b1_slots(), sorted(MAXES))
    assert any("both on Mon" in problem for problem in problems)


def test_a_weekday_outside_the_week_is_rejected():
    problems = generator.check_weekday_map({1: 0, 2: 2, 3: 7}, _b1_slots(), sorted(MAXES))
    assert any("not in 0-6" in problem for problem in problems)


def test_a_missing_slot_names_the_lifts_that_needed_it():
    problems = generator.check_weekday_map({1: 0, 3: 4}, _b1_slots(), sorted(MAXES))
    assert any("Day 2" in problem and "deadlift" in problem for problem in problems)


def test_only_the_slots_the_chosen_lifts_need_are_checked():
    """A deadlift-only block needs Day 2 alone, so Days 1 and 3 may be absent."""
    assert generator.check_weekday_map({2: 2}, _b1_slots(), ["deadlift"]) == []


def test_a_lift_in_one_slot_has_no_rest_requirement():
    """Deadlift trains once a week, so no gap can be too small."""
    assert generator.check_weekday_map({2: 0}, _b1_slots(), ["deadlift"]) == []


def test_back_to_back_days_are_rejected_for_a_twice_trained_lift():
    """Squat trains on Days 1 and 3; here that is Monday then Tuesday.

    Day 2 is absent from the map because a squat-only block does not need it.
    """
    problems = generator.check_weekday_map({1: 0, 3: 1}, _b1_slots(), ["squat"])
    assert any("squat" in problem and "1 day apart" in problem for problem in problems)


def test_the_minimum_rest_gap_is_accepted():
    """Exactly two days apart is fine -- the bound is inclusive."""
    assert generator.check_weekday_map({1: 0, 2: 1, 3: 2}, _b1_slots(), ["bench"]) == []


def test_every_problem_is_reported_not_just_the_first():
    """One call tells the athlete everything they need to change.

    Day 2 on Tuesday comes before Day 1 on Wednesday, *and* squat's two days
    (Wednesday and Thursday) are back to back.
    """
    problems = generator.check_weekday_map({1: 2, 2: 1, 3: 3}, _b1_slots(), sorted(MAXES))
    assert any("must come after" in problem for problem in problems)
    assert any("squat" in problem and "apart" in problem for problem in problems)
    assert len(problems) > 1


# --- next_block_one_rms --------------------------------------------------


def _one_lift_block(
    *,
    lift: str = "squat",
    weeks: int = 11,
    is_test: bool = True,
    weight: str = "140.00",
    reps: int = 1,
    sets: int = 1,
    week: int = 11,
    snapshot: str = "132.5",
) -> BlockDraft:
    """A minimal draft carrying one prescribed lift, for the rollover rules."""
    return BlockDraft(
        athlete_id=uuid.uuid4(),
        template_id=uuid.uuid4(),
        template_weeks=weeks,
        start_date=START,
        lifts=(lift,),
        one_rm_snapshot={lift: Decimal(snapshot)},
        sessions=(
            DraftSession(
                week=week,
                slot=1,
                seq=1,
                date=START + dt.timedelta(days=7 * (week - 1)),
                lifts=(
                    DraftLift(
                        lift_code=lift,
                        sets=sets,
                        reps=reps,
                        pct_num=Decimal("130"),
                        pct_den=Decimal("120"),
                        weight_kg=Decimal(weight),
                        rel_intensity=Decimal("1.0833"),
                        is_test=is_test,
                    ),
                ),
            ),
        ),
    )


def _log(
    *,
    lift: str = "squat",
    seq: int = 1,
    weight: str = "140",
    sets_done: int = 1,
    reps_done: int | tuple[int, ...] = 1,
    performed_on: dt.date | None = None,
) -> LoggedSession:
    return LoggedSession(
        seq=seq,
        performed_on=performed_on or dt.date(2026, 3, 23),
        entries=(
            LoggedLift(
                lift_code=lift,
                sets_done=sets_done,
                reps_done=reps_done,
                weight_kg=Decimal(weight),
            ),
        ),
    )


def test_a_completed_test_single_becomes_the_new_one_rm():
    """The author's own rollover: week 11's test is what block 2 starts from."""
    result = generator.next_block_one_rms(_one_lift_block(), [_log()])
    assert result["squat"].kg == Decimal("140")
    assert result["squat"].source == "tested"
    assert result["squat"].set_on == dt.date(2026, 3, 23)


def test_a_test_single_taken_heavier_than_prescribed_counts_in_full():
    """They lifted it, so it is their max -- not the number on the sheet."""
    result = generator.next_block_one_rms(_one_lift_block(), [_log(weight="145")])
    assert result["squat"].kg == Decimal("145")
    assert result["squat"].source == "tested"


def test_a_missed_test_keeps_the_old_one_rm():
    """Spec §5C and spec 08: a test that did not happen is not evidence."""
    result = generator.next_block_one_rms(_one_lift_block(), [])
    assert result["squat"].kg == Decimal("132.5")
    assert result["squat"].source is None
    assert result["squat"].set_on is None


def test_a_failed_test_keeps_the_old_one_rm():
    """Logged, but below the prescribed weight."""
    result = generator.next_block_one_rms(_one_lift_block(), [_log(weight="137.5")])
    assert result["squat"].kg == Decimal("132.5")
    assert result["squat"].source is None


def test_a_test_logged_with_no_completed_set_keeps_the_old_one_rm():
    result = generator.next_block_one_rms(_one_lift_block(), [_log(sets_done=0)])
    assert result["squat"].source is None


def test_a_test_logged_with_zero_reps_keeps_the_old_one_rm():
    """Walked out and failed the lift."""
    result = generator.next_block_one_rms(_one_lift_block(), [_log(reps_done=0)])
    assert result["squat"].source is None


def test_a_lift_with_no_test_is_estimated_from_its_best_recent_set():
    """Row is never tested, so logged work is the only way its max can move."""
    block = _one_lift_block(lift="row", is_test=False, reps=5, sets=3, snapshot="87.5")
    result = generator.next_block_one_rms(block, [_log(lift="row", weight="80", reps_done=5)])
    # 80 x 36/32 = 90.00, which beats the 87.5 snapshot.
    assert result["row"].kg == Decimal("90.00")
    assert result["row"].source == "estimated"
    assert result["row"].set_on == dt.date(2026, 3, 23)


def test_an_estimate_below_the_old_one_rm_leaves_it_alone():
    """Spec §5C: `source = estimated` only if it went up."""
    block = _one_lift_block(lift="row", is_test=False, reps=5, sets=3, snapshot="120")
    result = generator.next_block_one_rms(block, [_log(lift="row", weight="80", reps_done=5)])
    assert result["row"].kg == Decimal("120")
    assert result["row"].source is None


def test_an_estimate_from_outside_the_last_four_weeks_is_ignored():
    """The window is the block's final four template weeks (ADR-0005)."""
    block = _one_lift_block(lift="row", is_test=False, reps=5, sets=3, week=6, snapshot="87.5")
    result = generator.next_block_one_rms(block, [_log(lift="row", weight="80", reps_done=5)])
    assert result["row"].source is None


def test_an_estimate_inside_the_window_boundary_counts():
    """Week 8 of an 11-week block is the earliest week that counts."""
    block = _one_lift_block(lift="row", is_test=False, reps=5, sets=3, week=8, snapshot="87.5")
    result = generator.next_block_one_rms(block, [_log(lift="row", weight="80", reps_done=5)])
    assert result["row"].source == "estimated"


def test_the_best_set_of_a_per_set_rep_list_is_used():
    """Spec 20 may log reps as one number or one per set."""
    block = _one_lift_block(lift="row", is_test=False, reps=5, sets=3, snapshot="87.5")
    result = generator.next_block_one_rms(
        block, [_log(lift="row", weight="80", sets_done=3, reps_done=(5, 4, 3))]
    )
    # The 5-rep set gives the highest estimate.
    assert result["row"].kg == Decimal("90.00")


def test_a_rep_count_too_high_to_estimate_from_is_skipped():
    """A 20-rep back-off set says nothing useful about a maximum."""
    block = _one_lift_block(lift="row", is_test=False, reps=20, sets=3, snapshot="87.5")
    result = generator.next_block_one_rms(block, [_log(lift="row", weight="80", reps_done=20)])
    assert result["row"].source is None


def test_a_log_for_another_lift_does_not_move_this_ones_max():
    block = _one_lift_block(lift="row", is_test=False, reps=5, sets=3, snapshot="87.5")
    result = generator.next_block_one_rms(block, [_log(lift="squat", weight="200", reps_done=5)])
    assert result["row"].source is None


def test_every_chosen_lift_gets_an_answer():
    """Even with no logs at all, so a caller never has to handle a gap."""
    draft = _generate(_b1_slots(), weekday_map=MON_WED_FRI)
    result = generator.next_block_one_rms(draft, [])
    assert sorted(result) == sorted(MAXES)
    assert all(new.kg == MAXES[lift] for lift, new in result.items())


def test_a_tested_max_feeds_the_next_blocks_week_one(golden_entries, golden_maxes):
    """The rollover end to end: block 2's week 1 comes off the new max.

    The author's scenario -- a 100%+ single in week 11 becomes the max that
    week 1 of the next block is computed from.
    """
    block = _one_lift_block(weight="140.00")
    tested = generator.next_block_one_rms(block, [_log(weight="145")])["squat"]

    nxt = generator.generate_block(
        _athlete("1.0", "floor"),
        _template(),
        golden_entries,
        {"squat": tested.kg},
        ["squat"],
        dt.date(2026, 3, 30),
        MON_WED_FRI,
    )
    # FLOOR(145 x 70/120, 1) = 84
    assert _lift_at(nxt, "squat", 1, 1).weight_kg == Decimal("84.00")


# --- persist_block (skips when Postgres is unreachable) ------------------


@pytest.fixture
def saved_athlete(db_session: Session) -> models.Athlete:
    athlete = _athlete()
    db_session.add(athlete)
    db_session.flush()
    return athlete


@pytest.fixture
def saved_template(db_session: Session) -> models.BlockTemplate:
    template = _template()
    db_session.add(template)
    db_session.flush()
    return template


def _persisted(db_session, athlete, template, entries, maxes, lifts, **kwargs):
    draft = generator.generate_block(athlete, template, entries, maxes, lifts, START, MON_WED_FRI)
    return draft, generator.persist_block(db_session, draft, **kwargs)


def test_persisting_the_golden_block_writes_every_session_and_lift(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    draft, block = _persisted(
        db_session,
        saved_athlete,
        saved_template,
        golden_entries,
        golden_maxes,
        sorted(golden_maxes),
    )

    sessions = db_session.exec(
        select(models.PlannedSession).where(models.PlannedSession.block_id == block.id)
    ).all()
    assert len(sessions) == 33
    lifts = db_session.exec(
        select(models.PlannedLift).where(
            models.PlannedLift.session_id.in_([s.id for s in sessions])
        )
    ).all()
    assert len(lifts) == 77
    assert len(draft.sessions) == 33


def test_a_persisted_weight_survives_the_numeric_round_trip(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """NUMERIC(6,2) in, the same Decimal out -- no float in the middle."""
    _, block = _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["squat"]
    )
    first = db_session.exec(
        select(models.PlannedSession)
        .where(models.PlannedSession.block_id == block.id)
        .where(models.PlannedSession.seq == 1)
    ).one()
    lift = db_session.exec(
        select(models.PlannedLift).where(models.PlannedLift.session_id == first.id)
    ).one()
    db_session.refresh(lift)
    assert lift.weight_kg == Decimal("81.00")
    assert lift.pct_num == Decimal("70")
    assert lift.pct_den == Decimal("120")


def test_the_original_date_starts_equal_to_the_date(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """So a later reschedule leaves the first plan visible."""
    _, block = _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    sessions = db_session.exec(
        select(models.PlannedSession).where(models.PlannedSession.block_id == block.id)
    ).all()
    assert sessions
    assert all(session.original_date == session.date for session in sessions)


def test_the_first_block_is_number_one(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    _, block = _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    assert block.number == 1


def test_the_next_block_takes_the_next_number(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """Spec §5C: "number = previous max + 1 for the athlete"."""
    args = (db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"])
    _, first = _persisted(*args)
    _, second = _persisted(*args)
    assert (first.number, second.number) == (1, 2)


def test_numbering_is_per_athlete(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """`uq_block_athlete_id_number` is scoped to the athlete, so both start at 1."""
    other = _athlete()
    db_session.add(other)
    db_session.flush()

    _, mine = _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    _, theirs = _persisted(
        db_session, other, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    assert (mine.number, theirs.number) == (1, 1)


def test_a_block_is_persisted_as_a_draft_by_default(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """Spec 06 moves it to `active` at approval, which is the only write path."""
    _, block = _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    assert block.status == "draft"


def test_the_snapshot_is_stored_as_strings(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """JSONB cannot hold a Decimal, and a float would defeat the exact arithmetic."""
    _, block = _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    db_session.refresh(block)
    assert block.one_rm_snapshot == {"deadlift": "160.00"}
    assert block.lifts == ["deadlift"]


def test_the_generator_accepts_template_rows_read_back_from_the_database(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """The path spec 06 will actually use, and the one `TemplateRow` exists for.

    A stored `pct_num` comes back as `Decimal('70.0000')` rather than the
    `Decimal('70')` that was written, so this is where a naive
    `Fraction(num, den)` or a tuple comparison would break (ADR-0002 rule 2).
    The weights must still be the golden ones.
    """
    from domains.strength import importer

    parsed = importer.persist_template(
        db_session,
        importer.ParsedTemplate(
            name="golden-from-db",
            weeks=11,
            slots=3,
            overreach_from_week=7,
            source_file="B1_Mine.xlsx",
            entries=golden_entries,
        ),
    )
    rows = db_session.exec(
        select(models.TemplateEntry).where(models.TemplateEntry.template_id == parsed)
    ).all()
    for row in rows:
        db_session.refresh(row)
    assert len(rows) == 77
    # The scale really did change on the way through NUMERIC(10,4).
    assert any(row.pct_num == Decimal("70.0000") for row in rows)

    draft = generator.generate_block(
        saved_athlete, saved_template, rows, golden_maxes, sorted(golden_maxes), START, MON_WED_FRI
    )
    assert _lift_at(draft, "squat", 1, 1).weight_kg == Decimal("81.00")
    assert _lift_at(draft, "bench", 10, 3).weight_kg == Decimal("112.00")
    assert sum(len(session.lifts) for session in draft.sessions) == 77


def test_persist_block_does_not_commit(
    db_session, saved_athlete, saved_template, golden_entries, golden_maxes
):
    """The caller owns the transaction, so the whole block lands or none does."""
    _persisted(
        db_session, saved_athlete, saved_template, golden_entries, golden_maxes, ["deadlift"]
    )
    assert db_session.in_transaction()
