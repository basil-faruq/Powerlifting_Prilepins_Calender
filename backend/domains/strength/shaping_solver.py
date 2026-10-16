"""Deciding *what* to lift when the template does not fit. Spec 09, as amended.

The generator (spec 04) multiplies the template by a 1RM, and for an ordinary
block that is the whole job -- no solver required, which is precisely why
shaping exists as a separate module rather than replacing it. Shaping runs
only when the athlete asks for something the template cannot express: a
shorter block, a meet on a fixed date, a lift peaking in a different week.

With default settings it **reproduces the template exactly**. That is the
honest test of a soft-constrained model: the deviation terms outweigh the
Prilepin and trend penalties ten to one, so bending the author's own block to
score a point of theory costs more than it saves.

Two amendments to spec 09, both forced by the template itself:

1. **Bands and peaks are per `(lift, slot)`, derived from the reference.**
   Spec 09's per-lift week-1 band of 0.55-0.60 excludes squat slot 3, which
   starts at 0.625, and its "ramp to the lift's test week" contradicts squat
   slot 3 dropping from 1.042 in week 10 to 0.500 in week 11. Both would make
   the golden reproduction infeasible. Reading them off the reference makes
   the template feasible by construction.
2. **`rep_dev` exists.** With only `pct_dev` and `vol_dev`, `vol = sets x reps`
   leaves the factorisation free and the Prilepin term prefers fewer reps per
   set, so the solver reproduces every intensity and every volume while
   quietly rewriting 4x8 as 8x4. Spec 09's own definition of done -- "every
   `sets`/`reps` equal" -- is unreachable without this term.

Pure: no clock, no database, no randomness beyond the seed CP-SAT is given.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field, model_validator

from domains.strength import enums, generator, models, rep_max
from domains.strength.calendar_solver import SolverStats
from domains.strength.generator import TemplateRow
from domains.strength.schemas import BlockDraft
from domains.strength.validator import (
    DEFAULT_CONFIG,
    PRILEPIN_ZONES,
    ValidationContext,
    ValidatorConfig,
    validate,
)
from planner_core.proposal import ValidationResult

logger = logging.getLogger(__name__)

# Intensities live in the model as integer tenths of a percent, because
# CP-SAT is an integer solver and the authored values are not round: squat
# week 1 is 70/120 = 58.333%. A tenth is fine enough that no template entry
# moves by more than one unit, which is what spec 09 §12 asks the golden test
# to prove.
TENTHS = 1000

MIN_WEEKS = 6
MAX_WEEKS = 14


class ShapingWeights(BaseModel):
    """What the objective cares about, and how much.

    The two deviation terms are ten times the theory terms on purpose. Moving
    deadlift week 1 from 36 reps to 30 would save six Prilepin points and cost
    sixty deviation points, so the solver keeps the author's 6x6.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pct_dev: int = Field(default=10, ge=0)
    vol_dev: int = Field(default=10, ge=0)
    # Not in spec 09. Without it the solver re-factors 42 of the 77 entries --
    # same volume, same intensity, different sets and reps -- because nothing
    # in the objective prefers 4x8 to 8x4 and Prilepin prefers the latter.
    rep_dev: int = Field(default=10, ge=0)
    prilepin: int = Field(default=1, ge=0)
    trend: int = Field(default=1, ge=0)
    jump: int = Field(default=2, ge=0)


class ShapingConfig(BaseModel):
    """Bounds and tolerances. Every one of these is a starting value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    p_min: int = Field(default=300, ge=0)
    # `pct_cap` x 1000. Above this the validator rejects outright.
    p_max: int = Field(default=1100, ge=0)
    sets_max: int = Field(default=8, ge=1)
    reps_max: int = Field(default=12, ge=1)
    week1_margin_tenths: int = Field(
        default=30,
        ge=0,
        description="Starting value, not derived. How far week 1 may drift "
        "from the reference's own opening intensity, in tenths of a percent.",
    )
    jump_tenths: int = Field(
        default=75,
        ge=0,
        description="Starting value, not derived. A week-on-week rise above "
        "this is penalised. Soft only: the author's own block exceeds it six "
        "times, so a hard rule here would reject the source template.",
    )
    test_min_p: int = Field(default=1000, ge=0)


DEFAULT_WEIGHTS = ShapingWeights()
DEFAULT_SHAPING = ShapingConfig()


class RefEntry(BaseModel):
    """One slot of one week of the reference, in the solver's own units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    variation: enums.Variation | None = None
    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    p: int = Field(ge=0)
    sets: int = Field(ge=1)
    reps: int = Field(ge=1)
    # The authored pair, carried so an unchanged entry can keep it. `583/1000`
    # is not `70/120`, and swapping one for the other moves the weight.
    pct_num: Decimal = Field(gt=0)
    pct_den: Decimal = Field(gt=0)
    is_test: bool = False
    # True when this row came straight from a template week rather than from
    # interpolating between two. Only an exact row may keep its pair.
    exact: bool = True

    @property
    def key(self) -> tuple[str, int]:
        return (self.lift_code, self.slot)


class ShapingProblem(BaseModel):
    """A shaped block as the solver sees it.

    Not persisted and not JSON-serialisable -- the `(lift, slot)` tuple keys
    are the natural shape here and the problem never leaves the process.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: tuple[RefEntry, ...] = Field(min_length=1)
    weeks: int = Field(ge=MIN_WEEKS, le=MAX_WEEKS)
    # {lift: week}. Derived from the reference unless a caller moves one.
    test_week: dict[str, int] = Field(default_factory=dict)
    test_slot: dict[str, int] = Field(default_factory=dict)
    # {(lift, slot): last week that must not dip}. Each slot ramps to its own
    # peak, not to its lift's test week -- see the module docstring.
    ramp_end: dict[tuple[str, int], int] = Field(default_factory=dict)
    week1_band: dict[tuple[str, int], tuple[int, int]] = Field(default_factory=dict)
    overreach_from_week: int = Field(ge=1)
    weights: ShapingWeights = DEFAULT_WEIGHTS
    config: ShapingConfig = DEFAULT_SHAPING
    validator_config: ValidatorConfig = DEFAULT_CONFIG

    @model_validator(mode="after")
    def _reference_is_coherent(self) -> ShapingProblem:
        seen = {(entry.lift_code, entry.week, entry.slot) for entry in self.reference}
        if len(seen) != len(self.reference):
            raise ValueError("the reference repeats a (lift, week, slot)")
        highest = max(entry.week for entry in self.reference)
        if highest != self.weeks:
            raise ValueError(f"reference runs to week {highest}, but weeks is {self.weeks}")
        lifts = {entry.lift_code for entry in self.reference}
        unknown = sorted(set(self.test_week) - lifts)
        if unknown:
            raise ValueError(f"test_week names lifts the reference does not have: {unknown}")
        for lift, week in self.test_week.items():
            if not 1 <= week <= self.weeks:
                raise ValueError(f"{lift}'s test week {week} is outside 1..{self.weeks}")
        return self


class ShapingCost(BaseModel):
    """The objective, broken out so a reader can see what it paid for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    pct_dev: int = 0
    vol_dev: int = 0
    rep_dev: int = 0
    prilepin: int = 0
    trend: int = 0
    jump: int = 0


class ShapedEntry(BaseModel):
    """One solved entry. Structurally a `generator.TemplateRow`, deliberately.

    That is what lets a shaped block go through `generate_block` and inherit
    the dates, the seq numbering and the golden-tested weight arithmetic
    rather than reimplementing any of it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    variation: enums.Variation | None = None
    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    sets: int = Field(ge=1)
    reps: int = Field(ge=1)
    pct_num: Decimal = Field(gt=0)
    pct_den: Decimal = Field(gt=0)
    is_test: bool = False
    p: int = Field(ge=0)

    @property
    def pct(self) -> Fraction:
        return Fraction(self.pct_num) / Fraction(self.pct_den)


class ShapedBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ShapedEntry, ...]
    weeks: int
    test_week: dict[str, int]
    overreach_from_week: int
    cost: ShapingCost
    status: Literal["OPTIMAL", "FEASIBLE"]
    stats: SolverStats


class ShapingInfeasible(BaseModel):
    """No shaped block satisfies the request. Returned, never raised.

    Named `ShapingInfeasible` rather than `Infeasible` because `services.py`
    imports this module alongside `calendar_solver`, and two types with the
    same name one import line apart is a bug waiting to be written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    stats: SolverStats | None = None


ShapedCheck = Callable[[Sequence[ShapedEntry]], ValidationResult]


# --- Resampling ------------------------------------------------------------


def _round_half_up(value: Fraction) -> int:
    """Nearest integer, ties away from zero. Exact: no float anywhere."""
    return int((value * 2 + 1) // 2) if value >= 0 else -int((-value * 2 + 1) // 2)


def resample_reference(
    entries: Sequence[TemplateRow],
    weeks: int,
    *,
    template_weeks: int,
) -> tuple[RefEntry, ...]:
    """The template stretched or squeezed onto `weeks` weeks.

    New week `w` maps to template position `1 + (w-1)(T-1)/(weeks-1)`, and the
    intensity is interpolated linearly between the two template weeks either
    side of it. Sets and reps come from the nearest week, because they are not
    quantities that mean anything half way -- there is no 4.5x8.

    At `weeks == template_weeks` this is the identity, by construction rather
    than by a short-circuit: `pos` lands on `w`, the fraction is zero and the
    nearest week is `w` itself. Short-circuiting would let a resampler bug
    hide behind the golden test, which is the one test that would catch it.

    The endpoints are always exact. `pos` is 1 at `w = 1` and `T` at
    `w = weeks`, so week 1's opening intensity and the last week's deload both
    survive verbatim however far the block is stretched.
    """
    if weeks < 1:
        raise ValueError(f"weeks must be at least 1, got {weeks}")
    if template_weeks < 1:
        raise ValueError(f"template_weeks must be at least 1, got {template_weeks}")

    by_slot: dict[tuple[str, int], dict[int, TemplateRow]] = {}
    for entry in entries:
        by_slot.setdefault((entry.lift_code, entry.slot), {})[entry.week] = entry

    out: list[RefEntry] = []
    for (lift, slot), rows in sorted(by_slot.items()):
        weeks_present = sorted(rows)
        for week in range(1, weeks + 1):
            if weeks == 1:
                position = Fraction(weeks_present[0])
            else:
                position = 1 + Fraction((week - 1) * (template_weeks - 1), weeks - 1)
            low = _nearest_at_or_below(weeks_present, position)
            high = _nearest_above(weeks_present, position)
            exact = low == high

            low_row, high_row = rows[low], rows[high]
            low_pct = Fraction(low_row.pct_num) / Fraction(low_row.pct_den)
            if exact:
                pct = low_pct
            else:
                high_pct = Fraction(high_row.pct_num) / Fraction(high_row.pct_den)
                span = Fraction(high - low)
                pct = low_pct + (high_pct - low_pct) * (position - low) / span

            near_row = rows[_nearest(weeks_present, position)]
            out.append(
                RefEntry(
                    lift_code=lift,
                    variation=near_row.variation,
                    week=week,
                    slot=slot,
                    p=_round_half_up(pct * TENTHS),
                    sets=near_row.sets,
                    reps=near_row.reps,
                    pct_num=low_row.pct_num,
                    pct_den=low_row.pct_den,
                    # Set below, not here: a test must survive resampling even
                    # when no new week lands exactly on the old one.
                    is_test=False,
                    exact=exact,
                )
            )
        out = _place_tests(out, lift, slot, rows, weeks, template_weeks)
    return tuple(sorted(out, key=lambda entry: (entry.week, entry.slot, entry.lift_code)))


def _place_tests(
    out: list[RefEntry],
    lift: str,
    slot: int,
    rows: Mapping[int, TemplateRow],
    weeks: int,
    template_weeks: int,
) -> list[RefEntry]:
    """Carry each template test onto the resampled week nearest to it.

    Without this a shortened block silently loses its tests: at eight weeks
    no new week lands exactly on the template's week 10, so bench's test
    would simply disappear and the block would end with nothing measured.
    Spec 09 §5B asks for the test single to stay on the last week of its
    lift's ramp, and the nearest resampled week is where that is.
    """
    tested = [week for week, row in rows.items() if row.is_test]
    if not tested:
        return out

    mine = [
        index for index, entry in enumerate(out) if entry.lift_code == lift and entry.slot == slot
    ]
    for template_week in tested:
        best = min(
            mine,
            key=lambda index: (
                abs(_position(out[index].week, weeks, template_weeks) - template_week),
                out[index].week,
            ),
        )
        out[best] = out[best].model_copy(update={"is_test": True})
    return out


def _position(week: int, weeks: int, template_weeks: int) -> Fraction:
    """Where a shaped week sits on the template's own week axis."""
    if weeks == 1:
        return Fraction(1)
    return 1 + Fraction((week - 1) * (template_weeks - 1), weeks - 1)


def rescale_overreach(overreach_from_week: int, weeks: int, template_weeks: int) -> int:
    """Where the template's overreach week lands in a block of `weeks` weeks.

    Spec 09 never mentions this, and leaving it at the template's 7 would
    mean an eight-week block spending its last two weeks forbidden from
    overreaching -- or an eleven-week one overreaching two weeks early. The
    first shaped week at or past the template's overreach point is the
    honest translation.

    Identity at `weeks == template_weeks`: the template's 7 stays 7.
    """
    for week in range(1, weeks + 1):
        if _position(week, weeks, template_weeks) >= overreach_from_week:
            return week
    return weeks


def _nearest_at_or_below(weeks_present: Sequence[int], position: Fraction) -> int:
    return max((week for week in weeks_present if week <= position), default=weeks_present[0])


def _nearest_above(weeks_present: Sequence[int], position: Fraction) -> int:
    return min((week for week in weeks_present if week >= position), default=weeks_present[-1])


def _nearest(weeks_present: Sequence[int], position: Fraction) -> int:
    return min(weeks_present, key=lambda week: (abs(Fraction(week) - position), week))


# --- Building the problem --------------------------------------------------


def problem_from_reference(
    reference: Sequence[RefEntry],
    *,
    weeks: int,
    overreach_from_week: int,
    test_week: Mapping[str, int] | None = None,
    weights: ShapingWeights = DEFAULT_WEIGHTS,
    config: ShapingConfig = DEFAULT_SHAPING,
    validator_config: ValidatorConfig = DEFAULT_CONFIG,
) -> ShapingProblem:
    """Derive the per-slot peaks and bands this reference implies.

    `ramp_end` is each slot's own argmax, not its lift's test week. Squat
    tests in week 11 on slot 1 while slot 3 peaks in week 10 and then drops to
    a 50% deload; ramping slot 3 to week 11 would demand that the deload rise.

    A caller's `test_week` override moves a lift's test and extends that
    slot's ramp to meet it, which is the whole point of asking.
    """
    ramp_end: dict[tuple[str, int], int] = {}
    peak_p: dict[tuple[str, int], int] = {}
    week1: dict[tuple[str, int], tuple[int, int]] = {}

    by_slot: dict[tuple[str, int], list[RefEntry]] = {}
    for entry in reference:
        by_slot.setdefault(entry.key, []).append(entry)

    for key, rows in by_slot.items():
        rows.sort(key=lambda entry: entry.week)
        best = max(rows, key=lambda entry: (entry.p, -entry.week))
        ramp_end[key] = best.week
        peak_p[key] = best.p
        opening = rows[0].p
        week1[key] = (
            max(0, opening - config.week1_margin_tenths),
            min(config.p_max, opening + config.week1_margin_tenths),
        )

    # A lift's test sits on whichever of its slots climbs highest.
    derived_slot: dict[str, int] = {}
    derived_week: dict[str, int] = {}
    for lift, slot in ramp_end:
        if any(entry.is_test for entry in by_slot[(lift, slot)]):
            derived_slot[lift] = slot
            derived_week[lift] = next(
                entry.week for entry in by_slot[(lift, slot)] if entry.is_test
            )
    for lift in {entry.lift_code for entry in reference}:
        if lift in derived_slot:
            continue
        slots = [key for key in ramp_end if key[0] == lift]
        if not slots:
            continue
        top = max(slots, key=lambda key: peak_p[key])
        # No test in the reference means the lift is untested -- row. It gets
        # a peak for the ramp's sake but never a test week.
        derived_slot[lift] = top[1]

    moved: dict[str, int] = {}
    for lift, week in (test_week or {}).items():
        if lift not in derived_slot:
            raise ValueError(f"{lift} has no slot to put a test week on")
        if derived_week.get(lift) != week:
            moved[lift] = week
        derived_week[lift] = week
        slot = derived_slot[lift]
        # The ramp has to reach the test, wherever the athlete moved it.
        ramp_end[(lift, slot)] = max(ramp_end[(lift, slot)], week)

    # Moving a test week moves the flag with it. Leaving it where it was
    # would have the solver hold week 11 to a test's bounds while the
    # validator still judged the old week -- and the block would come back
    # with two tests, or none.
    entries = tuple(reference)
    if moved:
        entries = tuple(_retag_test(entry, moved, derived_slot) for entry in entries)

    return ShapingProblem(
        reference=entries,
        weeks=weeks,
        test_week=derived_week,
        test_slot={lift: slot for lift, slot in derived_slot.items() if lift in derived_week},
        ramp_end=ramp_end,
        week1_band=week1,
        overreach_from_week=overreach_from_week,
        weights=weights,
        config=config,
        validator_config=validator_config,
    )


def _retag_test(
    entry: RefEntry,
    moved: Mapping[str, int],
    test_slot: Mapping[str, int],
) -> RefEntry:
    """Put a moved lift's test on its new week and take it off the old one."""
    if entry.lift_code not in moved:
        return entry
    on_test_slot = entry.slot == test_slot[entry.lift_code]
    wanted = on_test_slot and entry.week == moved[entry.lift_code]
    if wanted == entry.is_test:
        return entry
    # A week that becomes a test loses its authored pair: the intensity is
    # about to be raised to a single, so the template's number for that week
    # no longer describes it.
    return entry.model_copy(update={"is_test": wanted, "exact": entry.exact and not wanted})


# --- Precomputed tables ----------------------------------------------------


def _rel_intensity_bounds(cap: Decimal, reps_max: int, p_max: int) -> list[int]:
    """The highest `p` each rep count may carry, indexed by reps.

    `p / rep_max_pct(reps) <= cap`, rearranged and floored so the bound is
    never looser than the validator's own check. Index 0 is unreachable
    padding, because `reps` starts at 1.

    **The bound is inclusive**, and that is load-bearing: the author's press
    week 5 sits at exactly 1.0000 relative intensity, so a strict bound would
    reject the source template.
    """
    bounds = [0]
    for reps in range(1, reps_max + 1):
        limit = Fraction(cap) * Fraction(rep_max.rep_max_pct(reps)) * TENTHS
        bounds.append(min(p_max, int(limit)))
    return bounds


def _ceiling(problem: ShapingProblem, entry: RefEntry) -> int:
    """The highest `p` this entry may take, before relative intensity.

    Folds in two validator rules spec 09 never mentions, for free and at
    build time:

    * `pct_early` -- above 100% is only allowed from the week before that
      lift's test.
    * a lift with no test may never exceed 100% at all, which is what keeps
      row in its place.

    Leaving these to the validator would mean the solver confidently
    returning blocks the gate then throws out, and an athlete asking for
    eight weeks being told it cannot be done.
    """
    test = problem.test_week.get(entry.lift_code)
    if test is not None and entry.week >= test - 1:
        return problem.config.p_max
    return TENTHS


def _prilepin_band(pct: Fraction) -> tuple[tuple[int, int], tuple[int, int]] | None:
    for lower, per_set, total in PRILEPIN_ZONES:
        if pct >= lower:
            return per_set, total
    return None


def _prilepin_bands(problem: ShapingProblem) -> dict[tuple[str, int, int], tuple]:
    """Each entry's Prilepin band, fixed from the *reference* intensity.

    Spec 09 §11 asks for the tables to be precomputed in plain Python, and
    this is the form that allows it. Reading the band off the solved `p`
    instead would need a 76,800-constant lookup per entry -- the shape
    ADR-0009 measured at 3.35 seconds and replaced with arithmetic.

    The approximation only bites if the solver moves an intensity across a
    band edge, which `pct_dev = 10` makes expensive. It is recorded as a
    known limitation rather than hidden.
    """
    bands = {}
    for entry in problem.reference:
        if entry.is_test:
            # The validator exempts test singles from both Prilepin rules
            # (ADR-0006), so penalising them here would push the solver to
            # shape a block around a rule nobody applies.
            continue
        band = _prilepin_band(Fraction(entry.p, TENTHS))
        if band is not None:
            bands[(entry.lift_code, entry.week, entry.slot)] = band
    return bands


# --- The model -------------------------------------------------------------


class _Built:
    """The CP-SAT model and the handles needed to read a solution back."""

    def __init__(self, problem: ShapingProblem) -> None:
        self.problem = problem
        self.model = cp_model.CpModel()
        cfg = problem.config
        self.p: dict[tuple, cp_model.IntVar] = {}
        self.sets: dict[tuple, cp_model.IntVar] = {}
        self.reps: dict[tuple, cp_model.IntVar] = {}
        self.vol: dict[tuple, cp_model.IntVar] = {}

        bands = _prilepin_bands(problem)
        pre = _rel_intensity_bounds(
            problem.validator_config.overreach_rel_intensity, cfg.reps_max, cfg.p_max
        )
        over = _rel_intensity_bounds(
            problem.validator_config.rel_intensity_cap, cfg.reps_max, cfg.p_max
        )

        terms: list = []
        self.parts: dict[str, list] = {
            name: [] for name in ("pct_dev", "vol_dev", "rep_dev", "prilepin", "trend", "jump")
        }

        for entry in problem.reference:
            key = (entry.lift_code, entry.week, entry.slot)
            name = f"{entry.lift_code}_w{entry.week}_s{entry.slot}"

            if entry.is_test:
                # A test is one rep at a maximum, by definition. Fixing it in
                # the domain rather than constraining it keeps the search
                # small and makes the intent unmissable.
                self.p[key] = self.model.new_int_var(cfg.test_min_p, cfg.p_max, f"p_{name}")
                self.sets[key] = self.model.new_int_var(1, 1, f"sets_{name}")
                self.reps[key] = self.model.new_int_var(1, 1, f"reps_{name}")
            else:
                self.p[key] = self.model.new_int_var(
                    cfg.p_min, _ceiling(problem, entry), f"p_{name}"
                )
                self.sets[key] = self.model.new_int_var(1, cfg.sets_max, f"sets_{name}")
                self.reps[key] = self.model.new_int_var(1, cfg.reps_max, f"reps_{name}")

            self.vol[key] = self.model.new_int_var(1, cfg.sets_max * cfg.reps_max, f"vol_{name}")
            self.model.add_multiplication_equality(self.vol[key], [self.sets[key], self.reps[key]])

            # Week 1 opens inside a band around the reference's own opening.
            if entry.week == 1 and entry.key in problem.week1_band:
                low, high = problem.week1_band[entry.key]
                self.model.add(self.p[key] >= low)
                self.model.add(self.p[key] <= high)

            # Relative intensity, via a twelve-entry table per entry. Not
            # `add_allowed_assignments` over every (p, reps) pair: that is
            # 8,412 tuples to say what twelve bounds say. `rep_max_pct` is an
            # authored table, not a formula, so there is no arithmetic form
            # to use instead -- but twelve constants is three orders of
            # magnitude smaller than the table ADR-0009 rejected.
            table = pre if entry.week < problem.overreach_from_week else over
            bound = self.model.new_int_var(0, cfg.p_max, f"relcap_{name}")
            self.model.add_element(self.reps[key], table, bound)
            self.model.add(self.p[key] <= bound)

            # --- soft terms
            self._deviation(key, name, "pct_dev", self.p[key], entry.p, cfg.p_max)
            self._deviation(
                key,
                name,
                "vol_dev",
                self.vol[key],
                entry.sets * entry.reps,
                cfg.sets_max * cfg.reps_max,
            )
            self._deviation(key, name, "rep_dev", self.reps[key], entry.reps, cfg.reps_max)

            band = bands.get(key)
            if band is not None:
                (per_low, per_high), (total_low, total_high) = band
                self._excess(key, f"pps_hi_{name}", self.reps[key], per_high, cfg.reps_max)
                self._shortfall(key, f"pps_lo_{name}", self.reps[key], per_low)
                self._excess(
                    key,
                    f"tot_hi_{name}",
                    self.vol[key],
                    total_high,
                    cfg.sets_max * cfg.reps_max,
                )
                self._shortfall(key, f"tot_lo_{name}", self.vol[key], total_low)

        self._ramp()
        self._jumps()
        self._trend()

        weights = problem.weights
        for name, parts in self.parts.items():
            weight = getattr(weights, name)
            terms.extend(weight * part for part in parts)
        self.model.minimize(sum(terms) if terms else 0)

        # Start CP-SAT from the reference. It cannot change the optimum, and
        # it is the difference between 0.4 s and 10 s single-threaded -- the
        # golden test would otherwise blow spec 09's own time budget.
        for entry in problem.reference:
            key = (entry.lift_code, entry.week, entry.slot)
            self.model.add_hint(self.p[key], entry.p)
            self.model.add_hint(self.sets[key], entry.sets)
            self.model.add_hint(self.reps[key], entry.reps)

    def _deviation(self, key, name: str, part: str, var, target: int, span: int) -> None:
        gap = self.model.new_int_var(-span, span, f"d_{part}_{name}")
        self.model.add(gap == var - target)
        out = self.model.new_int_var(0, span, f"a_{part}_{name}")
        self.model.add_abs_equality(out, gap)
        self.parts[part].append(out)

    def _excess(self, key, name: str, var, limit: int, span: int) -> None:
        out = self.model.new_int_var(0, span, name)
        self.model.add_max_equality(out, [var - limit, 0])
        self.parts["prilepin"].append(out)

    def _shortfall(self, key, name: str, var, limit: int) -> None:
        out = self.model.new_int_var(0, limit, name)
        self.model.add_max_equality(out, [limit - var, 0])
        self.parts["prilepin"].append(out)

    def _by_slot(self) -> dict[tuple[str, int], list[RefEntry]]:
        grouped: dict[tuple[str, int], list[RefEntry]] = {}
        for entry in self.problem.reference:
            grouped.setdefault(entry.key, []).append(entry)
        for rows in grouped.values():
            rows.sort(key=lambda entry: entry.week)
        return grouped

    def _ramp(self) -> None:
        """Intensity never dips while a slot is still climbing to its peak."""
        for slot_key, rows in self._by_slot().items():
            end = self.problem.ramp_end.get(slot_key)
            if end is None:
                continue
            for earlier, later in zip(rows, rows[1:], strict=False):
                if later.week <= end:
                    first = (earlier.lift_code, earlier.week, earlier.slot)
                    second = (later.lift_code, later.week, later.slot)
                    self.model.add(self.p[first] <= self.p[second])

    def _jumps(self) -> None:
        """A week-on-week rise beyond `jump_tenths` is penalised, never barred."""
        limit = self.problem.config.jump_tenths
        span = self.problem.config.p_max
        for rows in self._by_slot().values():
            for earlier, later in zip(rows, rows[1:], strict=False):
                first = (earlier.lift_code, earlier.week, earlier.slot)
                second = (later.lift_code, later.week, later.slot)
                out = self.model.new_int_var(0, span, f"jump_{second}")
                self.model.add_max_equality(out, [self.p[second] - self.p[first] - limit, 0])
                self.parts["jump"].append(out)

    def _trend(self) -> None:
        """Weekly volume rising, per lift, while the lift is still ramping."""
        cfg = self.problem.config
        span = cfg.sets_max * cfg.reps_max * 8
        per_lift_week: dict[tuple[str, int], list] = {}
        for entry in self.problem.reference:
            per_lift_week.setdefault((entry.lift_code, entry.week), []).append(
                self.vol[(entry.lift_code, entry.week, entry.slot)]
            )
        lifts = {entry.lift_code for entry in self.problem.reference}
        for lift in sorted(lifts):
            peak = max(
                (week for (name, _), week in self.problem.ramp_end.items() if name == lift),
                default=self.problem.weeks,
            )
            weeks = sorted(week for (name, week) in per_lift_week if name == lift)
            for earlier, later in zip(weeks, weeks[1:], strict=False):
                if later > peak:
                    continue
                out = self.model.new_int_var(0, span, f"trend_{lift}_{later}")
                self.model.add_max_equality(
                    out,
                    [
                        sum(per_lift_week[(lift, later)]) - sum(per_lift_week[(lift, earlier)]),
                        0,
                    ],
                )
                self.parts["trend"].append(out)


# --- Cost, computed in plain Python ----------------------------------------


def cost_of(problem: ShapingProblem, entries: Sequence[ShapedEntry]) -> ShapingCost:
    """What this block costs, independent of CP-SAT.

    Stated twice on purpose, as the calendar solver states its own: a
    mis-written objective is otherwise invisible, because the solver will
    happily optimise whatever it was actually given.
    """
    weights = problem.weights
    cfg = problem.config
    solved = {(entry.lift_code, entry.week, entry.slot): entry for entry in entries}
    reference = {(entry.lift_code, entry.week, entry.slot): entry for entry in problem.reference}
    bands = _prilepin_bands(problem)

    pct_dev = vol_dev = rep_dev = prilepin = 0
    for key, ref in reference.items():
        got = solved[key]
        pct_dev += abs(got.p - ref.p)
        vol_dev += abs(got.sets * got.reps - ref.sets * ref.reps)
        rep_dev += abs(got.reps - ref.reps)
        band = bands.get(key)
        if band is not None:
            (per_low, per_high), (total_low, total_high) = band
            volume = got.sets * got.reps
            prilepin += max(0, got.reps - per_high) + max(0, per_low - got.reps)
            prilepin += max(0, volume - total_high) + max(0, total_low - volume)

    by_slot: dict[tuple[str, int], list[ShapedEntry]] = {}
    for entry in entries:
        by_slot.setdefault((entry.lift_code, entry.slot), []).append(entry)
    for rows in by_slot.values():
        rows.sort(key=lambda entry: entry.week)

    jump = 0
    for rows in by_slot.values():
        for earlier, later in zip(rows, rows[1:], strict=False):
            jump += max(0, later.p - earlier.p - cfg.jump_tenths)

    per_lift_week: dict[tuple[str, int], int] = {}
    for entry in entries:
        key = (entry.lift_code, entry.week)
        per_lift_week[key] = per_lift_week.get(key, 0) + entry.sets * entry.reps
    trend = 0
    for lift in sorted({entry.lift_code for entry in entries}):
        peak = max(
            (week for (name, _), week in problem.ramp_end.items() if name == lift),
            default=problem.weeks,
        )
        weeks = sorted(week for (name, week) in per_lift_week if name == lift)
        for earlier, later in zip(weeks, weeks[1:], strict=False):
            if later > peak:
                continue
            trend += max(0, per_lift_week[(lift, later)] - per_lift_week[(lift, earlier)])

    total = (
        weights.pct_dev * pct_dev
        + weights.vol_dev * vol_dev
        + weights.rep_dev * rep_dev
        + weights.prilepin * prilepin
        + weights.trend * trend
        + weights.jump * jump
    )
    return ShapingCost(
        total=total,
        pct_dev=pct_dev,
        vol_dev=vol_dev,
        rep_dev=rep_dev,
        prilepin=prilepin,
        trend=trend,
        jump=jump,
    )


# --- Solving ---------------------------------------------------------------


_STATUS_NAMES = {cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE"}


def _solve_once(built: _Built, *, time_limit_s: float, seed: int, num_workers: int):
    """One CP-SAT call. A seam, so a test can force a time-out."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(time_limit_s, 0.01)
    solver.parameters.num_workers = num_workers
    solver.parameters.random_seed = seed
    return solver.solve(built.model), solver


def _stats(status: int, solver) -> SolverStats:
    objective = None
    if status in _STATUS_NAMES:
        objective = int(round(solver.objective_value))
    return SolverStats(
        status=_STATUS_NAMES.get(status, solver.status_name(status)),
        objective=objective,
        wall_time_s=solver.wall_time,
        num_booleans=solver.response_proto.num_booleans,
    )


def _read(problem: ShapingProblem, built: _Built, solver) -> tuple[ShapedEntry, ...]:
    out: list[ShapedEntry] = []
    for entry in problem.reference:
        key = (entry.lift_code, entry.week, entry.slot)
        p = int(solver.value(built.p[key]))
        # An entry the solver left alone keeps the pair the athlete authored.
        # `583/1000` and `70/120` are the same intensity to four decimals and
        # different kilograms once rounded, so this is what lets a default
        # shaped block reproduce the generator's weights exactly.
        if entry.exact and p == entry.p:
            num, den = entry.pct_num, entry.pct_den
        else:
            num, den = Decimal(p), Decimal(TENTHS)
        out.append(
            ShapedEntry(
                lift_code=entry.lift_code,
                variation=entry.variation,
                week=entry.week,
                slot=entry.slot,
                sets=int(solver.value(built.sets[key])),
                reps=int(solver.value(built.reps[key])),
                pct_num=num,
                pct_den=den,
                is_test=entry.is_test,
                p=p,
            )
        )
    return tuple(out)


def solve(
    problem: ShapingProblem,
    time_limit_s: float = 10.0,
    seed: int = 0,
    *,
    check: ShapedCheck,
    num_workers: int = 8,
) -> ShapedBlock | ShapingInfeasible:
    """The shaped block, or why there is not one.

    `check` is required and keyword-only, like the calendar solver's: spec 09
    §11 says every result goes through the validator, and a gate that can be
    omitted is not a gate.
    """
    built = _Built(problem)
    status, solver = _solve_once(
        built, time_limit_s=time_limit_s, seed=seed, num_workers=num_workers
    )
    stats = _stats(status, solver)

    if status == cp_model.MODEL_INVALID:
        raise RuntimeError(f"CP-SAT rejected the model: {built.model.validate()}")
    if status not in _STATUS_NAMES:
        reason = (
            "time limit"
            if status == cp_model.UNKNOWN
            else "infeasible: no block satisfies the constraints"
        )
        return ShapingInfeasible(reason=reason, stats=stats)

    entries = _read(problem, built, solver)
    verdict = check(entries)
    if not verdict.ok:
        rules = ", ".join(sorted({violation.rule for violation in verdict.violations}))
        logger.warning("shaped block dropped by the validator: %s", rules)
        return ShapingInfeasible(
            reason=f"validator rejected the shaped block: {rules}", stats=stats
        )

    return ShapedBlock(
        entries=entries,
        weeks=problem.weeks,
        test_week=dict(problem.test_week),
        overreach_from_week=problem.overreach_from_week,
        cost=cost_of(problem, entries),
        status=_STATUS_NAMES[status],
        stats=stats,
    )


# --- Turning a shaped block into a draft -----------------------------------


def to_draft(
    block: ShapedBlock,
    athlete: models.Athlete,
    template: models.BlockTemplate,
    one_rms: Mapping[str, Decimal],
    lifts: Sequence[str],
    start_date: dt.date,
    weekday_map: Mapping[int, int],
) -> BlockDraft:
    """A shaped block as a `BlockDraft`, via the generator.

    `ShapedEntry` satisfies `generator.TemplateRow` structurally, so one call
    inherits the dates, the date-ordered `seq` numbering, `round_weight`,
    `rel_intensity`, the lift ordering and the whole golden-tested weight
    path. Reimplementing any of that here would be a second arithmetic for
    the 77-weight test to disagree with.

    `template_weeks` is corrected afterwards because it means "weeks in this
    block", not "weeks in the template that inspired it":
    `next_block_one_rms` reads it as the look-back cutoff, so leaving an
    eight-week block claiming eleven would narrow its estimate window to one
    week.
    """
    draft = generator.generate_block(
        athlete, template, block.entries, one_rms, lifts, start_date, weekday_map
    )
    return draft.model_copy(update={"template_weeks": block.weeks})


def validator_check(
    athlete: models.Athlete,
    template: models.BlockTemplate,
    one_rms: Mapping[str, Decimal],
    lifts: Sequence[str],
    start_date: dt.date,
    weekday_map: Mapping[int, int],
    ctx: ValidationContext,
    *,
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> ShapedCheck:
    """The real spec 05 gate, as a `ShapedCheck`.

    The entries have to become dated, loaded sessions before the validator
    can say anything about them -- half its rules are about weights and dates
    -- so the check runs the same `generate_block` the caller will.
    """

    def check(entries: Sequence[ShapedEntry]) -> ValidationResult:
        draft = generator.generate_block(
            athlete, template, entries, one_rms, lifts, start_date, weekday_map
        )
        return validate(draft.sessions, ctx, config=config)

    return check


__all__ = [
    "MAX_WEEKS",
    "MIN_WEEKS",
    "RefEntry",
    "ShapedBlock",
    "ShapedEntry",
    "ShapingCost",
    "ShapingInfeasible",
    "ShapingProblem",
    "ShapingWeights",
    "cost_of",
    "problem_from_reference",
    "rescale_overreach",
    "resample_reference",
    "solve",
    "to_draft",
    "validator_check",
]
