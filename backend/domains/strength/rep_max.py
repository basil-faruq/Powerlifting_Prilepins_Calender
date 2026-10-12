"""Rep-max percentages, relative intensity and 1RM estimation.

A percentage of 1RM does not say how hard a set is -- that depends on the reps.
Three sets at 58% is easy for ten reps and trivial for three. This module turns
the two numbers into one comparable one, which is what makes spec 05's rules
expressible at all:

* `rep_max_pct(reps)` -- the share of a 1RM a trained lifter holds for `reps`
  (CONTEXT.md §3 calls it P1RM);
* `rel_intensity(pct, reps)` -- how close a prescribed set is to that limit.
  1.0 is exactly at it, and above 1.0 is overreaching, which the template
  permits only from its overreach week;
* `estimate_1rm(weight, reps)` -- the inverse: the max a completed set implies.
  The only way a never-tested lift (`row`) can move between blocks.

Spec 04 §5A names this module `brzycki.py`, after the surname attached to the
relation. Renamed at the author's direction (ADR-0005): the file is about rep
maxes, and the person is provenance rather than subject matter.

`REP_PCT` is the authored workbook's own table (`P1RM` sheet, column A), not the
formula evaluated here -- and the two differ. Brzycki's `36 / (37 - reps)`
gives 0.9730 at two reps and 0.9474 at three, where the sheet says 0.95 and
0.90. The author rounded, and those rounded numbers are the ones every
intensity in the block was chosen against, so the table is the authority for
1..13 and the formula only extends past it. A test reads the sheet and compares.

No float arithmetic anywhere (CLAUDE.md non-negotiable #4): every value is a
`Decimal` built from a string, or an exact `Fraction` quantized once at the end.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

# The Brzycki column of the workbook's `P1RM` sheet, reps 1..13.
#
# Strings, so no float ever appears. Deliberately not computed: a test asserts
# this matches `P1RM!A1:A13`, and that comparison would be circular if both
# sides came from the same formula.
REP_PCT: dict[int, Decimal] = {
    1: Decimal("1.00"),
    2: Decimal("0.95"),
    3: Decimal("0.90"),
    4: Decimal("0.88"),
    5: Decimal("0.86"),
    6: Decimal("0.83"),
    7: Decimal("0.80"),
    8: Decimal("0.78"),
    9: Decimal("0.76"),
    10: Decimal("0.75"),
    11: Decimal("0.72"),
    12: Decimal("0.70"),
    13: Decimal("0.68"),
}

# The relation behind the table: `pct = (37 - reps) / 36`, inverted for
# `estimate_1rm` as `1RM = weight x 36 / (37 - reps)`.
_NUMERATOR = 36
_LIMIT = 37

# `rel_intensity` is stored in `planned_lift.rel_intensity`, NUMERIC(6,4).
REL_INTENSITY_PLACES = Decimal("0.0001")

# `estimate_1rm` feeds `one_rep_max.kg`, NUMERIC(6,2).
KG_PLACES = Decimal("0.01")

# Spec 04 §5A bounds estimation at 12 reps. Past that the estimate says more
# about conditioning than about a maximum, and a 1RM is the one number in this
# system that has to mean something (CLAUDE.md non-negotiable #9).
MAX_ESTIMATE_REPS = 12


def rep_max_pct(reps: int) -> Decimal:
    """The share of a 1RM that a set of `reps` reps represents.

    The authored table for 1..13; above that, the formula `(37 - reps) / 36`.
    """
    if reps < 1:
        raise ValueError(f"reps must be at least 1, got {reps}")
    if reps in REP_PCT:
        return REP_PCT[reps]
    if reps >= _LIMIT:
        # At 37 the formula divides by zero and past it the share is negative,
        # so there is no answer to give rather than a wrong one.
        raise ValueError(f"reps must be below {_LIMIT} for this relation, got {reps}")
    return _quantize(Fraction(_LIMIT - reps, _NUMERATOR), KG_PLACES)


def rel_intensity(pct: Fraction, reps: int) -> Decimal:
    """How close a set at `pct` of 1RM is to the limit for that many reps.

    `pct / rep_max_pct(reps)`. Squat W1 D1 (70/120 for 10 reps) gives 0.7778;
    the W11 test (130/120 for 1) gives 1.0833, which is the overreach spec 05
    allows only from the template's overreach week.

    Spec 04 §5A specifies 4 dp and names no rounding mode; half-up is this
    implementation's choice (ADR-0005), applied once, after exact arithmetic.
    """
    return _quantize(pct / Fraction(rep_max_pct(reps)), REL_INTENSITY_PLACES)


def estimate_1rm(weight_kg: Decimal, reps: int) -> Decimal:
    """The 1RM a completed set of `reps` at `weight_kg` implies.

    `weight x 36 / (37 - reps)`, which returns the weight itself at one rep.

    This is an *estimate* and is treated as one: `generator.next_block_one_rms`
    only ever lets it raise a max, never lower one, and a real test single
    always wins over it.
    """
    if not 1 <= reps <= MAX_ESTIMATE_REPS:
        raise ValueError(
            f"reps must be between 1 and {MAX_ESTIMATE_REPS} to estimate a 1RM, got {reps}"
        )
    if weight_kg <= 0:
        raise ValueError(f"weight_kg must be positive, got {weight_kg}")
    return _quantize(Fraction(weight_kg) * Fraction(_NUMERATOR, _LIMIT - reps), KG_PLACES)


def _quantize(value: Fraction, places: Decimal) -> Decimal:
    """An exact `Fraction` rendered as a `Decimal`, half-up, at `places`.

    The division runs in `Decimal`, never in `float`.
    """
    quotient = Decimal(value.numerator) / Decimal(value.denominator)
    return quotient.quantize(places, rounding=ROUND_HALF_UP)
