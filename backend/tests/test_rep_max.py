"""Rep-max percentage, relative intensity and 1RM estimation.

Pure arithmetic: no database and no workbook, except for the one test that
checks `REP_PCT` still matches the sheet it was copied from.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from domains.strength import rep_max

# The P1RM sheet's Brzycki column, reps 1..13. Spelled out again rather than
# read from the module, so this asserts the table rather than agreeing with it.
SHEET_VALUES = [
    (1, "1.00"),
    (2, "0.95"),
    (3, "0.90"),
    (4, "0.88"),
    (5, "0.86"),
    (6, "0.83"),
    (7, "0.80"),
    (8, "0.78"),
    (9, "0.76"),
    (10, "0.75"),
    (11, "0.72"),
    (12, "0.70"),
    (13, "0.68"),
]

# (pct_num, pct_den, reps, expected) -- real rows from the authored block.
REL_INTENSITIES = [
    # Squat W1 Day 1: 3x10 at 58.33%, comfortably inside the 10-rep limit.
    ("70", "120", 10, "0.7778"),
    # Squat W11 Day 1, the test single: above 100% of the 1RM by design.
    ("130", "120", 1, "1.0833"),
    # Bench W10 Day 3's test.
    ("112.5", "105", 1, "1.0714"),
    # Press W9's test.
    ("72.5", "70", 1, "1.0357"),
    # A set exactly at the rep-max limit.
    ("75", "100", 10, "1.0000"),
]

ESTIMATES = [
    # One rep is the max, by definition.
    ("100", 1, "100.00"),
    ("90", 5, "101.25"),
    ("100", 10, "133.33"),
    ("142.5", 3, "150.88"),
    ("60", 12, "86.40"),
]


# --- The table ------------------------------------------------------------


@pytest.mark.parametrize(("reps", "expected"), SHEET_VALUES)
def test_the_table_holds_the_authored_percentage_for_each_rep_count(reps, expected):
    assert rep_max.rep_max_pct(reps) == Decimal(expected)


def test_the_table_covers_exactly_one_to_thirteen_reps():
    assert sorted(rep_max.REP_PCT) == list(range(1, 14))


def test_every_table_value_is_a_decimal_rather_than_a_float():
    """A float here would quietly infect every relative intensity."""
    assert all(isinstance(value, Decimal) for value in rep_max.REP_PCT.values())


def test_the_table_decreases_as_reps_rise():
    values = [rep_max.REP_PCT[reps] for reps in sorted(rep_max.REP_PCT)]
    assert values == sorted(values, reverse=True)


def test_the_authored_table_differs_from_the_raw_formula_where_it_was_rounded():
    """Why the table is copied and not computed.

    Brzycki gives 0.9730 at two reps where the sheet says 0.95. The sheet is
    the authority: those are the numbers the block's intensities were chosen
    against.
    """
    assert rep_max.rep_max_pct(2) == Decimal("0.95")
    assert Decimal(35) / Decimal(36) != Decimal("0.95")


# --- Past the table -------------------------------------------------------


def test_above_thirteen_reps_the_formula_extends_the_table():
    """`(37 - reps) / 36` at 14 reps."""
    assert rep_max.rep_max_pct(14) == Decimal("0.64")


def test_the_formula_picks_up_where_the_table_leaves_off():
    """No cliff at the boundary: 14 reps is a little under the table's 13."""
    assert rep_max.rep_max_pct(14) < rep_max.rep_max_pct(13)


@pytest.mark.parametrize("reps", [0, -1])
def test_a_rep_count_below_one_is_rejected(reps):
    with pytest.raises(ValueError, match="at least 1"):
        rep_max.rep_max_pct(reps)


@pytest.mark.parametrize("reps", [37, 50])
def test_a_rep_count_the_relation_cannot_answer_is_rejected(reps):
    """At 37 the formula divides by zero and past it goes negative."""
    with pytest.raises(ValueError, match="below 37"):
        rep_max.rep_max_pct(reps)


# --- Relative intensity ---------------------------------------------------


@pytest.mark.parametrize(("num", "den", "reps", "expected"), REL_INTENSITIES)
def test_relative_intensity_matches_the_worked_values(num, den, reps, expected):
    pct = Fraction(Decimal(num)) / Fraction(Decimal(den))
    assert rep_max.rel_intensity(pct, reps) == Decimal(expected)


def test_relative_intensity_is_quantized_to_four_places():
    """`planned_lift.rel_intensity` is NUMERIC(6,4)."""
    pct = Fraction(Decimal("70")) / Fraction(Decimal("120"))
    assert rep_max.rel_intensity(pct, 10).as_tuple().exponent == -4


def test_a_set_at_its_rep_max_is_exactly_one():
    assert rep_max.rel_intensity(Fraction(3, 4), 10) == Decimal("1.0000")


def test_overreaching_sets_come_out_above_one():
    """What spec 05 keys its overreach rule on."""
    assert rep_max.rel_intensity(Fraction(1), 1) == Decimal("1.0000")
    assert rep_max.rel_intensity(Fraction(11, 10), 1) > Decimal("1.0000")


def test_relative_intensity_rounds_half_up():
    """The documented choice (ADR-0005), pinned so a refactor cannot drift it.

    `0.68125 / 1.00` sits exactly on the 4 dp boundary; banker's rounding would
    give 0.6812.
    """
    assert rep_max.rel_intensity(Fraction(Decimal("0.68125")), 1) == Decimal("0.6813")


def test_relative_intensity_is_exact_for_a_ratio_a_float_cannot_hold():
    """0.1 is not representable in binary; the Fraction path does not care."""
    assert rep_max.rel_intensity(Fraction(1, 10), 1) == Decimal("0.1000")


# --- Estimating a 1RM -----------------------------------------------------


@pytest.mark.parametrize(("weight", "reps", "expected"), ESTIMATES)
def test_estimate_matches_the_brzycki_formula(weight, reps, expected):
    assert rep_max.estimate_1rm(Decimal(weight), reps) == Decimal(expected)


def test_a_single_estimates_itself():
    assert rep_max.estimate_1rm(Decimal("137.5"), 1) == Decimal("137.50")


def test_the_estimate_rises_with_reps_at_the_same_weight():
    weights = [rep_max.estimate_1rm(Decimal("100"), reps) for reps in range(1, 13)]
    assert weights == sorted(weights)


def test_the_estimate_is_quantized_to_two_places():
    """`one_rep_max.kg` is NUMERIC(6,2)."""
    assert rep_max.estimate_1rm(Decimal("100"), 10).as_tuple().exponent == -2


@pytest.mark.parametrize("reps", [0, -3, 13, 20])
def test_estimating_outside_one_to_twelve_reps_is_rejected(reps):
    """Spec §13: reps >= 13 raises. Zero and negatives are not sets at all."""
    with pytest.raises(ValueError, match="between 1 and 12"):
        rep_max.estimate_1rm(Decimal("100"), reps)


@pytest.mark.parametrize("weight", ["0", "-5"])
def test_estimating_from_a_non_positive_weight_is_rejected(weight):
    with pytest.raises(ValueError, match="positive"):
        rep_max.estimate_1rm(Decimal(weight), 5)


def test_estimating_and_prescribing_are_inverses():
    """A set at the tabulated 5-rep share of a max estimates back to near it.

    Not exactly: the sheet's 0.86 is rounded from the formula's 0.8889, so the
    round trip lands low by about 3%. Pinned because that gap is the reason
    `next_block_one_rms` only ever lets an estimate *raise* a max.
    """
    five_rep_weight = Decimal("200") * rep_max.rep_max_pct(5)
    assert rep_max.estimate_1rm(five_rep_weight, 5) == Decimal("193.50")


# --- Against the authored workbook (skips when it is absent) -------------


def test_the_table_still_matches_the_workbooks_p1rm_sheet(workbook_path: Path):
    """Spec §11: "Brzycki table values must match the P1RM sheet exactly".

    The sheet keeps its header at the *bottom* (row 14, reading
    `'        Brzycki'` with leading spaces), so the values are rows 1..13 of
    column A with no header to skip.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(workbook_path, data_only=True)
    try:
        sheet = workbook["P1RM"]
        # str() first: the cells are floats, and Decimal(float) is the trap.
        values = {row: Decimal(str(sheet.cell(row=row, column=1).value)) for row in range(1, 14)}
    finally:
        workbook.close()

    assert values == {reps: pct for reps, pct in rep_max.REP_PCT.items()}
