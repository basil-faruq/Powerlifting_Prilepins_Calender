"""Snapping a raw weight to a loadable one.

Pure arithmetic; needs neither the database nor the workbook.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from domains.strength.rounding import round_weight

# Squat W1 Day 1 against the authored 140 kg example max: 140 x 70/120, which
# is 81.666... and therefore not loadable on any bar.
SQUAT_W1_RAW = Fraction(Decimal("140")) * Fraction(Decimal("70")) / Fraction(Decimal("120"))

# (raw, increment, mode, expected)
CASES = [
    # The authored spreadsheet's own setting: whole kilograms, always down.
    (SQUAT_W1_RAW, "1", "floor", "81.00"),
    (SQUAT_W1_RAW, "1", "nearest", "82.00"),
    # Spec §12: the same row at a real gym's plate increment.
    (SQUAT_W1_RAW, "2.5", "floor", "80.00"),
    (SQUAT_W1_RAW, "2.5", "nearest", "82.50"),
    (SQUAT_W1_RAW, "0.5", "floor", "81.50"),
    (SQUAT_W1_RAW, "0.5", "nearest", "81.50"),
    # An exact multiple is left alone by both modes.
    (Fraction(100), "2.5", "floor", "100.00"),
    (Fraction(100), "2.5", "nearest", "100.00"),
    (Fraction(100), "1", "floor", "100.00"),
    # Just below a multiple: floor drops a whole step, nearest does not.
    (Fraction(Decimal("99.9")), "2.5", "floor", "97.50"),
    (Fraction(Decimal("99.9")), "2.5", "nearest", "100.00"),
]

TIES = [
    # Exactly half a step above a multiple, in each increment.
    (Fraction(Decimal("81.25")), "2.5", "82.50"),
    (Fraction(Decimal("83.75")), "2.5", "85.00"),
    (Fraction(Decimal("100.5")), "1", "101.00"),
    (Fraction(Decimal("100.25")), "0.5", "100.50"),
]


@pytest.mark.parametrize(("raw", "increment", "mode", "expected"), CASES)
def test_rounding_matches_the_worked_values(raw, increment, mode, expected):
    assert round_weight(raw, Decimal(increment), mode) == Decimal(expected)


@pytest.mark.parametrize(("raw", "increment", "expected"), TIES)
def test_a_tie_rounds_up_in_nearest_mode(raw, increment, expected):
    """Spec §5B: "nearest multiple, ties round up".

    Python's own `round` would send 81.25 up and 83.75 down at a 2.5 kg step,
    which is not what a lifter reading a program expects.
    """
    assert round_weight(raw, Decimal(increment), "nearest") == Decimal(expected)


@pytest.mark.parametrize("increment", ["1", "2.5", "0.5", "5"])
def test_floor_never_exceeds_the_raw_weight(increment):
    """The reason the author chose it: never prescribe more than asked for."""
    step = Decimal(increment)
    assert round_weight(SQUAT_W1_RAW, step, "floor") <= Decimal(SQUAT_W1_RAW.numerator) / Decimal(
        SQUAT_W1_RAW.denominator
    )


@pytest.mark.parametrize("increment", ["1", "2.5", "0.5", "5", "1.25"])
@pytest.mark.parametrize("mode", ["floor", "nearest"])
def test_every_result_is_an_exact_multiple_of_the_increment(increment, mode):
    """Spec 05 rejects a weight that is not a multiple, so this can never fail there."""
    step = Decimal(increment)
    result = round_weight(SQUAT_W1_RAW, step, mode)
    assert result % step == 0


@pytest.mark.parametrize("mode", ["floor", "nearest"])
def test_the_result_is_quantized_to_two_places(mode):
    """`planned_lift.weight_kg` is NUMERIC(6,2), so the golden test's `==` works."""
    assert round_weight(SQUAT_W1_RAW, Decimal("2.5"), mode).as_tuple().exponent == -2


def test_a_raw_weight_below_one_increment_floors_to_zero():
    """Documented rather than special-cased.

    The generator cannot produce this -- `DraftLift.weight_kg` requires a
    positive number, and a positive 1RM times a positive intensity clears a
    1 kg step -- but the arithmetic is worth pinning.
    """
    assert round_weight(Fraction(1, 2), Decimal("1"), "floor") == Decimal("0.00")


def test_rounding_is_exact_for_a_value_a_float_cannot_hold():
    """0.1 + 0.2 is not 0.3 in binary; none of this runs in binary."""
    raw = Fraction(1, 10) + Fraction(2, 10)
    assert round_weight(raw, Decimal("0.1"), "nearest") == Decimal("0.30")


@pytest.mark.parametrize("increment", ["0", "-1", "-2.5"])
def test_a_non_positive_increment_is_rejected(increment):
    with pytest.raises(ValueError, match="increment must be positive"):
        round_weight(SQUAT_W1_RAW, Decimal(increment), "floor")


@pytest.mark.parametrize("mode", ["ceiling", "up", "FLOOR", ""])
def test_an_unknown_mode_is_rejected(mode):
    with pytest.raises(ValueError, match="unknown rounding mode"):
        round_weight(SQUAT_W1_RAW, Decimal("1"), mode)
