"""Importer tests.

Four groups, each with a different environment requirement, so `make test`
stays green and meaningful in all four combinations of "has Postgres" and
"has the workbook":

* **Units** -- `parse_intensity`, `normalize_variation`, `mark_tests` and
  `check_template` take plain values, so they need neither.
* **Golden fixture** -- `tests/fixtures/b1_expected_weights.json` is committed,
  so these always run. They are also the only coverage of the authored
  workbook's shape available on a fresh clone, which is why the `Wide Grp`
  normalisation and the per-lift counts are asserted here as well as against
  the real file.
* **Workbook** -- request `workbook_path`, which skips when the gitignored
  `data/B1_Mine.xlsx` is absent.
* **Database** -- request `db_session`, which skips when Postgres is
  unreachable. These build a `ParsedTemplate` by hand rather than parsing, so
  they do not need the workbook too.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from openpyxl.worksheet.formula import ArrayFormula
from sqlmodel import Session, select

from domains.strength import enums, importer, models
from domains.strength.schemas import ParsedEntry, ParsedTemplate

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "b1_expected_weights.json"

# CONTEXT.md §4's table, which spec 03 §10 repeats.
LIFT_COUNTS = [("deadlift", 11), ("squat", 22), ("bench", 22), ("press", 11), ("row", 11)]

# (lift, week, slot, pct_num, pct_den) for the four max attempts. Row is absent
# on purpose: it is never tested.
TEST_ENTRIES = [
    ("deadlift", 11, 2, "170", "160"),
    ("squat", 11, 1, "130", "120"),
    ("bench", 10, 3, "112.5", "105"),
    ("press", 9, 2, "72.5", "70"),
]

# Spec 03 §12's documented spot checks, plus the press test.
SPOT_WEIGHTS = [
    ("squat", 1, 1, "81.00"),
    ("deadlift", 11, 2, "170.00"),
    ("bench", 10, 3, "112.00"),
    ("press", 9, 2, "72.00"),
]

# Spec 03 §5G's seven, plus `one_rm_kg` (ADR-0003 mod 21) and spec 04's
# `pct_num`, `pct_den` and `is_test` (ADR-0005). The order is the file's.
GOLDEN_KEYS = [
    "lift",
    "week",
    "slot",
    "variation",
    "sets",
    "reps",
    "one_rm_kg",
    "weight_kg",
    "pct_num",
    "pct_den",
    "is_test",
]

# The example maxes each sheet's weights were computed against. These are the
# fixture's *inputs*, not anyone's real 1RMs -- the workbook's maxes are
# examples and the importer never reads them. They live here so the golden
# fixture is a self-contained (inputs -> expected output) oracle.
REFERENCE_MAXES = {
    "deadlift": "160.00",
    "squat": "140.00",
    "bench": "105.00",
    "press": "70.00",
    "row": "105.00",
}

# A formula keeps the pair exactly as authored -- unreduced.
FORMULA_INTENSITIES = [
    ("=70/120", "70", "120"),
    ("=90/160", "90", "160"),
    ("=112.5/105", "112.5", "105"),
    ("=72.5/70", "72.5", "70"),
    ("= 80 / 120 ", "80", "120"),
]

# A bare number has no authored pair to preserve, so it becomes its reduced
# exact fraction.
LITERAL_INTENSITIES = [
    (0.5, "1", "2"),
    (Decimal("0.5"), "1", "2"),
    ("0.5", "1", "2"),
    (0.75, "3", "4"),
    (1, "1", "1"),
]

BAD_INTENSITIES = [
    None,
    "",
    "   ",
    "x",
    "=70",
    "=70/0",
    "=0/120",
    "=-70/120",
    "=70/120*0.95",
    "=SUM(A1:A2)",
    "=F7",
    True,
    # `D1` on every lift sheet is one of these. The naive implementation calls
    # `.startswith` on it and raises AttributeError instead of saying anything.
    ArrayFormula(ref="D1", text='=INDIRECT("1RM!$A$2")'),
    object(),
]

VARIATION_TEXTS = [
    ("Close Grip", "close_grip"),
    ("Wide Grip", "wide_grip"),
    # The typo in B1 Bench W11 Day 3.
    ("Wide Grp", "wide_grip"),
    ("CLOSE GRIP", "close_grip"),
    ("  wide   grip ", "wide_grip"),
    (None, None),
    ("", None),
    ("   ", None),
]

# (sets, reps, pct_num, pct_den) shapes that must NOT be flagged as a test.
# The first four are real rows from the workbook.
NON_TESTS = [
    (3, 1, "120", "120"),  # squat W10 slot 1: a top single at the max
    (2, 1, "70", "70"),  # press W8
    (3, 1, "100", "105"),  # bench W10 slot 1
    (5, 1, "90", "125"),  # bench W11
    (1, 1, "100", "105"),  # a single below the max
    (1, 1, "100", "100"),  # a single at exactly the max -- pins strict `>`
    (1, 3, "130", "120"),  # a triple above it
]


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
    row: int = 5,
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
        source_row=row,
    )


def _template(entries, *, name: str = "B1", weeks: int = 11, **kwargs) -> ParsedTemplate:
    return ParsedTemplate(
        name=name,
        weeks=weeks,
        slots=kwargs.pop("slots", 3),
        overreach_from_week=kwargs.pop("overreach_from_week", 7),
        source_file=kwargs.pop("source_file", "B1_Mine.xlsx"),
        entries=tuple(entries),
    )


@pytest.fixture(scope="module")
def golden_rows() -> list[dict]:
    """The committed golden fixture.

    No skip: unlike the workbook, this file is version-controlled, so its
    absence is a failure rather than an environment difference.
    """
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parsed_b1(workbook_path: Path) -> ParsedTemplate:
    """The authored workbook, parsed once for the whole module."""
    return importer.parse_workbook(workbook_path)


def _at(rows: list[dict], lift: str, week: int, slot: int) -> dict:
    return next(
        row for row in rows if (row["lift"], row["week"], row["slot"]) == (lift, week, slot)
    )


# --- Units: intensity -----------------------------------------------------


@pytest.mark.parametrize(("value", "num", "den"), FORMULA_INTENSITIES)
def test_an_intensity_formula_keeps_the_authored_numerator_and_denominator(value, num, den):
    """`=70/120` is stored as 70 and 120, never as 7/12 and never as 0.5833.

    The denominator is an independent authored datum -- a cycle target max,
    not the 1RM -- so reducing the pair would destroy information that cannot
    be recovered. Spec 03 §14 names this case explicitly.
    """
    assert importer.parse_intensity(value) == (Decimal(num), Decimal(den))


def test_an_intensity_formula_is_not_reduced():
    """The negative half of the rule above, stated on its own so it cannot rot."""
    assert importer.parse_intensity("=70/120") != (Decimal("7"), Decimal("12"))
    # ...while still being the same exact quantity.
    num, den = importer.parse_intensity("=70/120")
    assert Fraction(num) / Fraction(den) == Fraction(7, 12)


@pytest.mark.parametrize(("value", "num", "den"), LITERAL_INTENSITIES)
def test_a_bare_number_becomes_its_reduced_fraction(value, num, den):
    """B1 Squat W11 Day 3 holds the literal 0.5 where every other cell is a formula."""
    assert importer.parse_intensity(value) == (Decimal(num), Decimal(den))


def test_a_bare_float_never_passes_through_binary_floating_point():
    """`Decimal(0.1)` is 0.1000000000000000055511151231257827; `Decimal("0.1")` is not."""
    assert importer.parse_intensity(0.1) == (Decimal("1"), Decimal("10"))


def test_an_array_formula_holding_a_plain_division_still_parses():
    value = ArrayFormula(ref="F7", text="=70/120")
    assert importer.parse_intensity(value) == (Decimal("70"), Decimal("120"))


@pytest.mark.parametrize("value", BAD_INTENSITIES)
def test_an_unparseable_intensity_names_the_sheet_and_cell(value):
    """Spec §13: the error must identify the cell, e.g. `B1 Squat!F12`.

    The `ArrayFormula` and `object()` cases also assert the failure is a
    TemplateImportError rather than an AttributeError from calling a string
    method on something that is not a string.
    """
    with pytest.raises(importer.TemplateImportError, match=re.escape("B1 Squat!F12")):
        importer.parse_intensity(value, ref="B1 Squat!F12")


def test_a_spreadsheet_formula_is_never_evaluated():
    """Rejected, not computed. Nothing here ever reaches eval()."""
    with pytest.raises(importer.TemplateImportError, match="not a plain"):
        importer.parse_intensity("=70/120*0.95", ref="B1 Squat!F12")


# --- Units: variations ----------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), VARIATION_TEXTS)
def test_variation_text_is_normalised(text, expected):
    """Includes `Wide Grp` -> `wide_grip`, which spec §14 requires."""
    assert importer.normalize_variation(text) == expected


def test_an_unknown_variation_names_the_cell():
    with pytest.raises(importer.TemplateImportError, match=re.escape("B1 Bench!C26")):
        importer.normalize_variation("Wde Grip", ref="B1 Bench!C26")


def test_every_normalised_variation_is_a_value_the_database_accepts():
    """The alias table and `ck_template_entry_variation_known` cannot drift apart."""
    assert set(importer._VARIATION_ALIASES.values()) <= set(enums.VARIATIONS)


# --- Units: marking the test entries -------------------------------------


def test_a_single_above_one_hundred_percent_is_a_test():
    entry = _entry("deadlift", week=11, slot=2, sets=1, reps=1, num="170", den="160")
    assert importer.mark_tests([entry])[0].is_test is True


@pytest.mark.parametrize(("sets", "reps", "num", "den"), NON_TESTS)
def test_a_set_that_is_not_a_single_above_the_max_is_not_a_test(sets, reps, num, den):
    """`>`, not `>=`: a single at exactly the max is a top single, not a new max."""
    entry = _entry(sets=sets, reps=reps, num=num, den=den)
    assert importer.mark_tests([entry])[0].is_test is False


def test_mark_tests_does_not_mutate_its_input():
    entry = _entry("press", week=9, slot=2, sets=1, reps=1, num="72.5", den="70")
    marked = importer.mark_tests([entry])
    assert marked[0].is_test is True
    assert entry.is_test is False


# --- Units: whole-template checks ----------------------------------------


def test_check_template_accepts_a_coherent_template():
    parsed = _template([_entry("squat", week=1, slot=1), _entry("row", week=1, slot=3)])
    assert importer.check_template(parsed) == []


def test_check_template_reports_an_empty_template():
    """The guard against a header misdetection that imports nothing, successfully."""
    assert importer.check_template(_template([])) == ["the workbook produced no entries"]


def test_check_template_reports_a_week_beyond_the_template_length():
    problems = importer.check_template(_template([_entry("squat", week=12, row=26)]))
    assert len(problems) == 1
    assert "B1 Squat!row 26" in problems[0]
    assert "week 12 is outside weeks 1..11" in problems[0]


def test_check_template_reports_a_slot_beyond_the_slot_count():
    problems = importer.check_template(_template([_entry("squat", slot=4, row=9)]))
    assert len(problems) == 1
    assert "slot 4 is outside slots 1..3" in problems[0]


def test_check_template_reports_a_duplicate_lift_week_and_slot_naming_both_rows():
    """Pre-empts uq_template_entry_template_id_lift_code_week_slot."""
    parsed = _template(
        [
            _entry("squat", week=11, slot=3, row=14),
            _entry("squat", week=11, slot=3, row=26),
        ]
    )
    problems = importer.check_template(parsed)
    assert len(problems) == 1
    assert "B1 Squat!row 26 repeats squat week 11 slot 3" in problems[0]
    assert "already at B1 Squat!row 14" in problems[0]


def test_check_template_reports_two_tests_for_one_lift():
    parsed = _template(
        [
            _entry("squat", week=10, slot=1, sets=1, reps=1, num="125", den="120", is_test=True),
            _entry("squat", week=11, slot=1, sets=1, reps=1, num="130", den="120", is_test=True),
        ]
    )
    problems = importer.check_template(parsed)
    assert len(problems) == 1
    assert "squat has 2 test entries" in problems[0]


def test_check_template_accepts_a_lift_with_no_test():
    """Row is never tested, so "exactly one per lift" would reject the real file."""
    parsed = _template([_entry("row", week=week, slot=3, row=4 + week) for week in range(1, 12)])
    assert importer.check_template(parsed) == []


# --- The committed golden fixture ----------------------------------------


def test_the_golden_fixture_has_seventy_seven_rows(golden_rows):
    assert len(golden_rows) == 77


@pytest.mark.parametrize(("lift", "count"), LIFT_COUNTS)
def test_the_golden_fixture_has_the_expected_count_per_lift(golden_rows, lift, count):
    assert sum(1 for row in golden_rows if row["lift"] == lift) == count


@pytest.mark.parametrize(("lift", "week", "slot", "weight"), SPOT_WEIGHTS)
def test_the_golden_fixture_matches_the_documented_spot_weights(
    golden_rows, lift, week, slot, weight
):
    """Spec §12's own worked examples, read back from the committed file."""
    assert Decimal(_at(golden_rows, lift, week, slot)["weight_kg"]) == Decimal(weight)


def test_the_golden_fixture_rows_carry_exactly_the_documented_keys(golden_rows):
    assert all(list(row) == GOLDEN_KEYS for row in golden_rows)


def test_every_golden_weight_is_a_decimal_string_rather_than_a_json_number(golden_rows):
    """`json.load` turns 81.0 into a float, and `Decimal(81.0)` is the trap.

    Keeping these as strings makes a float physically unable to enter spec 04's
    comparison. This test exists so nobody "simplifies" them to numbers.
    """
    assert all(isinstance(row["weight_kg"], str) for row in golden_rows)
    assert all(isinstance(row["one_rm_kg"], str) for row in golden_rows)
    # Same trap, same reason: spec 04 divides these two as Fractions.
    assert all(isinstance(row["pct_num"], str) for row in golden_rows)
    assert all(isinstance(row["pct_den"], str) for row in golden_rows)


def test_the_golden_fixture_keeps_the_authored_pair_unreduced(golden_rows):
    """`=70/120` stays 70 and 120, never 7/12 (ADR-0003 rule 2).

    The denominator is the target max the block was programmed against, so
    reducing the pair would destroy a datum the author chose deliberately.
    """
    squat = _at(golden_rows, "squat", 1, 1)
    assert (squat["pct_num"], squat["pct_den"]) == ("70", "120")


def test_the_golden_fixture_marks_exactly_the_four_tests(golden_rows):
    """The same four rows spec 03 found, assertable with no workbook present."""
    tests = [row for row in golden_rows if row["is_test"]]
    assert len(tests) == 4
    assert {(row["lift"], row["week"], row["slot"]) for row in tests} == {
        (lift, week, slot) for lift, week, slot, _, _ in TEST_ENTRIES
    }


@pytest.mark.parametrize(("lift", "week", "slot", "num", "den"), TEST_ENTRIES)
def test_the_golden_fixture_records_each_tests_authored_pair(
    lift, week, slot, num, den, golden_rows
):
    row = _at(golden_rows, lift, week, slot)
    assert (row["pct_num"], row["pct_den"]) == (num, den)


def test_every_golden_row_reproduces_its_weight_from_its_own_inputs(golden_rows):
    """The fixture is self-consistent: FLOOR(one_rm x num/den, 1) for all 77 rows.

    This is the check ADR-0003 ran by hand when `one_rm_kg` was added, now that
    the pair lives in the file too. It is deliberately *not* the generator's
    code path -- it is the arithmetic spelled out independently, so the oracle
    stays evidence rather than a copy of the implementation.
    """
    for row in golden_rows:
        raw = (
            Fraction(Decimal(row["one_rm_kg"]))
            * Fraction(Decimal(row["pct_num"]))
            / Fraction(Decimal(row["pct_den"]))
        )
        assert Decimal(raw // 1) == Decimal(row["weight_kg"]), row


@pytest.mark.parametrize(("lift", "one_rm"), sorted(REFERENCE_MAXES.items()))
def test_every_golden_row_records_the_max_its_weight_was_computed_from(golden_rows, lift, one_rm):
    """The fixture is a complete oracle, not a list of answers.

    Excel computed each weight as `FLOOR(max x intensity, 1)` from the max
    cached on that lift sheet. Those maxes are examples, and since the importer
    deliberately does not read them, the fixture has to carry them or spec 04
    cannot reproduce a single weight. Spec 04's golden test feeds `one_rm_kg`
    to the generator -- never the athlete's own 1RM, which is a different
    number and would give different weights.
    """
    rows = [row for row in golden_rows if row["lift"] == lift]
    assert rows
    assert {row["one_rm_kg"] for row in rows} == {one_rm}


def test_the_golden_fixture_is_unique_by_lift_week_and_slot(golden_rows):
    keys = [(row["lift"], row["week"], row["slot"]) for row in golden_rows]
    assert len(set(keys)) == len(keys)


def test_the_golden_fixture_is_sorted_deterministically(golden_rows):
    """Byte-stable across runs, so a regeneration that changes nothing is a no-op diff."""
    assert golden_rows == sorted(
        golden_rows, key=lambda row: (row["lift"], row["week"], row["slot"])
    )


def test_only_bench_rows_carry_a_variation_in_the_golden_fixture(golden_rows):
    assert {row["lift"] for row in golden_rows if row["variation"] is not None} == {"bench"}


def test_the_golden_fixture_shows_the_week_eleven_typo_normalised_to_wide_grip(golden_rows):
    """Spec §14's `Wide Grp` item, verifiable with no workbook present."""
    assert _at(golden_rows, "bench", 11, 3)["variation"] == "wide_grip"


def test_the_golden_fixture_shows_week_ten_bench_as_wide_in_both_slots(golden_rows):
    assert _at(golden_rows, "bench", 10, 1)["variation"] == "wide_grip"
    assert _at(golden_rows, "bench", 10, 3)["variation"] == "wide_grip"


# --- The authored workbook (skips when it is absent) ---------------------


def test_the_authored_workbook_yields_seventy_seven_entries(parsed_b1):
    assert len(parsed_b1.entries) == 77


@pytest.mark.parametrize(("lift", "count"), LIFT_COUNTS)
def test_each_lift_contributes_the_documented_number_of_entries(parsed_b1, lift, count):
    """Also proves the trailing style-only rows produce nothing.

    `B1 Deadlift` reports max_row 35 against 11 entries and `B1 Squat` reports
    33 against 22, so a loop that did not skip blank `Day` rows would be well
    over count here.
    """
    assert sum(1 for entry in parsed_b1.entries if entry.lift_code == lift) == count


def test_exactly_four_entries_are_marked_as_tests(parsed_b1):
    assert sum(1 for entry in parsed_b1.entries if entry.is_test) == 4


@pytest.mark.parametrize(("lift", "week", "slot", "num", "den"), TEST_ENTRIES)
def test_the_four_tests_are_the_documented_rows(parsed_b1, lift, week, slot, num, den):
    tests = [entry for entry in parsed_b1.entries if entry.is_test and entry.lift_code == lift]
    assert len(tests) == 1
    entry = tests[0]
    assert (entry.week, entry.slot) == (week, slot)
    assert (entry.sets, entry.reps) == (1, 1)
    assert (entry.pct_num, entry.pct_den) == (Decimal(num), Decimal(den))


def test_row_is_never_tested(parsed_b1):
    assert not [entry for entry in parsed_b1.entries if entry.lift_code == "row" and entry.is_test]


def test_the_squat_week_eleven_slot_three_intensity_is_the_bare_literal_one_half(parsed_b1):
    """The one cell in the workbook holding a value instead of a formula."""
    entry = next(e for e in parsed_b1.entries if (e.lift_code, e.week, e.slot) == ("squat", 11, 3))
    assert (entry.pct_num, entry.pct_den) == (Decimal("1"), Decimal("2"))
    assert entry.source_row == 26


def test_the_workbooks_example_maxes_are_not_read(parsed_b1):
    """The `1RM` sheet is example data, so nothing in the parse result carries it.

    A template is a pattern -- sets, reps and intensity -- that any athlete's
    own 1RM can be applied to. If a max ever leaks out of the workbook it will
    silently become someone's training weight.
    """
    assert not hasattr(parsed_b1, "one_rms")
    assert "one_rms" not in parsed_b1.model_dump()


def test_the_golden_fixture_agrees_with_the_workbook_on_every_entry(parsed_b1, golden_rows):
    """The committed fixture is still a faithful copy of the authored file.

    Spec 04's golden test builds its template entries from the fixture rather
    than from the workbook, so that it runs on a clone that does not have the
    gitignored file. This is what keeps that substitution honest: whenever the
    workbook *is* present, every structural field the generator consumes is
    compared against it.
    """
    assert len(golden_rows) == len(parsed_b1.entries)
    for entry in parsed_b1.entries:
        row = _at(golden_rows, entry.lift_code, entry.week, entry.slot)
        assert row["variation"] == entry.variation
        assert (row["sets"], row["reps"]) == (entry.sets, entry.reps)
        assert row["is_test"] == entry.is_test
        # The pair exactly, not just the Fraction: an unreduced 70/120 that
        # regenerated as 7/12 would still compare equal as a ratio.
        assert (Decimal(row["pct_num"]), Decimal(row["pct_den"])) == (entry.pct_num, entry.pct_den)
        assert Fraction(Decimal(row["pct_num"])) / Fraction(Decimal(row["pct_den"])) == entry.pct


def test_the_authored_weeks_and_slots_are_eleven_by_three(parsed_b1):
    assert (parsed_b1.weeks, parsed_b1.slots) == (11, 3)
    assert max(entry.week for entry in parsed_b1.entries) == 11
    assert max(entry.slot for entry in parsed_b1.entries) == 3


def test_the_authored_workbook_has_no_structural_problems(parsed_b1):
    assert importer.check_template(parsed_b1) == []


def test_a_missing_lift_sheet_names_the_sheet(tmp_path, workbook_path):
    """Spec §13's wording, checked against a workbook with one sheet removed."""
    from openpyxl import load_workbook

    workbook = load_workbook(workbook_path)
    del workbook["B1 Press"]
    path = tmp_path / "no_press.xlsx"
    workbook.save(path)
    workbook.close()

    with pytest.raises(importer.TemplateImportError, match="sheet 'B1 Press' not found"):
        importer.parse_workbook(path)


# --- Database-backed (skips when Postgres is unreachable) ----------------


def test_the_exact_percentages_survive_the_round_trip(db_session: Session):
    """Spec §14's "stored as exact fractions" item, through NUMERIC(10,4).

    The two shapes that make the column NUMERIC rather than INTEGER: the bench
    test's 112.5/105, and the reduced pair from squat's bare 0.5.
    """
    parsed = _template(
        [
            _entry(
                "bench",
                week=10,
                slot=3,
                sets=1,
                reps=1,
                num="112.5",
                den="105",
                variation="wide_grip",
            ),
            _entry("squat", week=11, slot=3, sets=3, reps=3, num="1", den="2"),
        ],
        name="round-trip-probe",
    )
    template_id = importer.persist_template(db_session, parsed)

    rows = db_session.exec(
        select(models.TemplateEntry).where(models.TemplateEntry.template_id == template_id)
    ).all()
    for row in rows:
        db_session.refresh(row)
    by_lift = {row.lift_code: row for row in rows}

    assert (by_lift["bench"].pct_num, by_lift["bench"].pct_den) == (
        Decimal("112.5"),
        Decimal("105"),
    )
    assert Fraction(by_lift["bench"].pct_num) / Fraction(by_lift["bench"].pct_den) == Fraction(
        15, 14
    )
    assert (by_lift["squat"].pct_num, by_lift["squat"].pct_den) == (Decimal("1"), Decimal("2"))


def test_importing_the_same_name_twice_is_refused(db_session: Session):
    parsed = _template([_entry("squat")], name="twice-probe")
    importer.persist_template(db_session, parsed)
    with pytest.raises(importer.TemplateExistsError, match="already exists"):
        importer.persist_template(db_session, parsed)


def test_replace_keeps_the_template_id_and_swaps_the_entries(db_session: Session):
    """Stable identity matters: spec 06 exposes the template by id."""
    first = _template([_entry("squat", week=1, slot=1)], name="replace-probe")
    original_id = importer.persist_template(db_session, first)

    second = _template(
        [_entry("bench", week=2, slot=3, variation="wide_grip"), _entry("row", week=2, slot=3)],
        name="replace-probe",
    )
    replaced_id = importer.persist_template(db_session, second, replace=True)

    assert replaced_id == original_id
    rows = db_session.exec(
        select(models.TemplateEntry).where(models.TemplateEntry.template_id == original_id)
    ).all()
    assert sorted(row.lift_code for row in rows) == ["bench", "row"]


def test_a_variation_the_lift_does_not_declare_is_rejected(db_session: Session):
    """`ck_template_entry_variation_known` only restricts the value set.

    The database accepts `close_grip` on `squat` without complaint, because
    the constraint knows nothing about which lift owns which variation. The
    seeded `lift.variations` is the authority, and this is the check that
    consults it.
    """
    parsed = _template(
        [_entry("squat", variation="close_grip", row=7)],
        name="variation-coherence-probe",
    )
    with pytest.raises(importer.TemplateImportError, match="does not declare variation"):
        importer.persist_template(db_session, parsed)


def test_a_failed_import_leaves_no_rows(db_session: Session):
    """All-or-nothing (spec §11): everything is checked before the first add."""
    before = len(db_session.exec(select(models.BlockTemplate)).all())
    parsed = _template(
        [
            _entry("squat", week=11, slot=3, row=14),
            _entry("squat", week=11, slot=3, row=26),
        ],
        name="all-or-nothing-probe",
    )
    with pytest.raises(importer.TemplateImportError, match="repeats squat week 11 slot 3"):
        importer.persist_template(db_session, parsed)

    db_session.flush()
    assert len(db_session.exec(select(models.BlockTemplate)).all()) == before


def test_the_importer_does_not_commit(db_session: Session):
    """The caller owns the transaction, which is what makes these tests rollback-safe."""
    importer.persist_template(db_session, _template([_entry("squat")], name="no-commit-probe"))
    assert db_session.in_transaction()


def test_the_source_file_is_recorded_on_the_template(db_session: Session):
    parsed = _template([_entry("squat")], name="source-file-probe")
    template_id = importer.persist_template(db_session, parsed)
    template = db_session.get(models.BlockTemplate, template_id)
    assert template is not None
    assert template.source_file == "B1_Mine.xlsx"


def test_persisting_a_template_writes_no_one_rep_max_rows(db_session: Session):
    """A max is the athlete's own measurement, never a by-product of an import.

    The workbook's `1RM` sheet holds example values and no test date at all, so
    importing a block pattern must not put a number in `one_rep_max`. The
    athlete enters their maxes with the date they tested them, and those are
    the only ones the planner ever reads.
    """
    before = len(db_session.exec(select(models.OneRepMax)).all())
    importer.persist_template(db_session, _template([_entry("squat")], name="no-one-rm-probe"))
    db_session.flush()
    assert len(db_session.exec(select(models.OneRepMax)).all()) == before


# --- Both the workbook and a database ------------------------------------


def test_importing_the_authored_workbook_creates_seventy_seven_entries_and_four_tests(
    workbook_path: Path, db_session: Session
):
    """Spec §14's first and third items, end to end through `import_template`."""
    template_id = importer.import_template(db_session, workbook_path, name="B1-e2e-probe")

    rows = db_session.exec(
        select(models.TemplateEntry).where(models.TemplateEntry.template_id == template_id)
    ).all()
    assert len(rows) == 77
    assert sum(1 for row in rows if row.is_test) == 4
    assert {row.lift_code for row in rows} == set(enums.LIFT_CODES)
