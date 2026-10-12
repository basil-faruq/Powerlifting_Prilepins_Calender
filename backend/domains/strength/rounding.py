"""Snapping a raw barbell weight to something that can actually be loaded.

`1RM x pct` is almost never a loadable number: 140 x 70/120 is 81.666..., and
no combination of plates gives that. The athlete's `plate_increment_kg` says
what the bar can actually be set to, and `rounding` says which way to go.

The authored workbook uses `FLOOR(x, 1)` -- whole kilograms, always down -- so
those are the defaults (`models.Athlete`). Whether a real gym's 2.5 kg plate
reality should win instead is still open (CONTEXT.md §9); both modes exist so
that question is a setting rather than a rewrite.

Exact arithmetic end to end (CLAUDE.md non-negotiable #4). The input is a
`Fraction` so nothing has been rounded before it arrives, and the single
conversion to `Decimal` happens on a value that is already an exact multiple of
the increment -- so it cannot itself round anything.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction

from domains.strength import enums

# Matches `planned_lift.weight_kg`, NUMERIC(6,2), so the golden test's
# comparison against the fixture is a plain `==` with no rescaling.
WEIGHT_PLACES = Decimal("0.01")


def round_weight(raw: Fraction, increment: Decimal, mode: enums.Rounding) -> Decimal:
    """`raw` snapped to a multiple of `increment`.

    * `floor` -- the largest multiple at or below `raw`. Never prescribes more
      than the percentage asked for, which is why the author chose it.
    * `nearest` -- the closest multiple, with a tie going up.

    Both return an exact multiple of `increment`, so spec 05's "a weight that
    is not a multiple of the plate increment" rule can never fire on output
    from here.
    """
    if increment <= 0:
        raise ValueError(f"increment must be positive, got {increment}")
    if mode not in enums.ROUNDINGS:
        raise ValueError(f"unknown rounding mode {mode!r}, expected one of {enums.ROUNDINGS}")

    step = Fraction(increment)
    if mode == "floor":
        multiples = math.floor(raw / step)
    else:
        # Half-up rather than Python's banker's rounding: at a 2.5 kg
        # increment, 81.25 going to 82.5 and 83.75 going to 85 is what a lifter
        # expects, where round() would send one up and one down.
        multiples = math.floor(raw / step + Fraction(1, 2))

    # `multiples` is an int and `increment` an exact Decimal, so this product is
    # exact and the quantize only fixes the scale.
    return (Decimal(multiples) * increment).quantize(WEIGHT_PLACES)
