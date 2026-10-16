"""Whether a plan is safe to train. Ten rules that reject, three that warn.

The validator is the gate every change passes through: the generator drafts a
block, a solver reshuffles it, the agent proposes it — and none of them is
trusted. `validate` is the only thing that says yes.

Three properties it is built around:

1. **It is a pure function.** No database, no clock, no randomness, no LLM. The
   present moment arrives as `ctx.today`, so a test can place itself anywhere in
   a block and the same inputs always give the same verdict.
2. **It accepts the author's own training plan** (CLAUDE.md non-negotiable #5).
   That is a calibration requirement, not an aspiration: the authored template
   breaks Prilepin's rep ranges on purpose and lets volume and intensity rise
   together in four places, so those are *warnings*. A rule that rejects the
   source material is miscalibrated, and `test_validator.py` pins the counts so
   a future change to them has to be deliberate.
3. **It never raises for a broken rule, and always raises for a broken input.**
   An unknown lift or a duplicated `seq` is a programming error upstream; a
   plan that schedules 115% in week 2 is exactly what this function is for.

Two judgements the spec left open, decided by the author (ADR-0006):

* **The schedule rules judge today onward.** `plan` is the whole resulting
  schedule, history included, and a proposal cannot change what already
  happened — so a rest gap the athlete trained through in week 2 must not veto
  a reschedule in week 9. A pair straddling `today` is still judged. The
  intensity rules read the whole plan, because a weight in week 10 is still in
  the future regardless of what week 2 looked like.
* **A pain note with nothing logged is blocking.** With no completed set to
  compare against, there is no evidence the planned load is tolerable, and the
  safe direction is to stop (CLAUDE.md non-negotiable #3's spirit).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from domains.strength import enums, rep_max
from domains.strength.schemas import DraftLift, DraftSession, SessionChange, StrengthPayload
from planner_core.proposal import ValidationResult, Violation

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Prilepin's table, hard-coded because the workbook's copy of it was coerced
# into dates by Excel and is unrecoverable (CONTEXT.md §4). Each row is
# (lower bound on % of 1RM, reps per set, total reps); the bound is inclusive
# and the band below 55% is deliberately absent — at those intensities the
# table has nothing to say, so nothing is checked.
#
# These are *warnings* everywhere, for the reason spec 05 §1 gives: deadlift
# week 1 is 6x6 = 36 reps at 56%, well above the 18-30 this table allows.
PRILEPIN_ZONES: tuple[tuple[Fraction, tuple[int, int], tuple[int, int]], ...] = (
    (Fraction(90, 100), (1, 2), (4, 10)),
    (Fraction(80, 100), (2, 4), (10, 20)),
    (Fraction(70, 100), (3, 6), (12, 24)),
    (Fraction(55, 100), (3, 6), (18, 30)),
)


class ValidatorConfig(BaseModel):
    """Every threshold in one place (spec 05 §11), with the documented defaults.

    Note `overreach_from_week` is *not* here. It is per-template data, stored on
    `block_template.overreach_from_week` and carried on the context, so a
    template that overreaches from week 8 needs no new configuration and the two
    cannot disagree (ADR-0006).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 110% rather than the 108% that would look tighter and still be defensible:
    # the author's own squat test is 130/120 = 108.3%, so a lower cap would
    # reject the source template (CONTEXT.md §5).
    pct_cap: Decimal = Decimal("1.10")
    # Above this a set is harder than the rep-max table says is possible.
    overreach_rel_intensity: Decimal = Decimal("1.0")
    rel_intensity_cap: Decimal = Decimal("1.10")
    # **Starting values, not derived.** Both come from the author's own
    # Mon/Wed/Fri layout, where a twice-trained lift sits 2 days apart. Whether
    # a deadlift may follow a squat by one day is an open question
    # (CONTEXT.md §9), so the cross-lift default is the permissive answer and is
    # overridable per pair through `ValidationContext.rest_min_days`.
    same_lift_rest_days: int = Field(default=2, ge=0)
    cross_lift_rest_days: int = Field(default=1, ge=0)


DEFAULT_CONFIG = ValidatorConfig()


class ValidationContext(BaseModel):
    """Everything about the athlete and the block that the rules measure against.

    Assembled by the caller — spec 06's route, spec 13's tool, spec 15's
    activity — because each piece comes from a different place: the weekday map
    and the plate increment from the athlete, the test weeks from the template,
    the pain lifts from spec 12's note screening, and `today` from the caller's
    clock rather than this module's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    # {slot: weekday}, 0 = Monday. A session whose slot is absent here has no
    # home weekday to be on, which `off_weekday` reports.
    weekday_map: dict[int, int]
    # From `block_template`. Before it, relative intensity above 1.0 is a
    # violation; from it, the template is allowed to overreach.
    overreach_from_week: int = Field(default=enums.OVERREACH_FROM_WEEK, ge=1)
    # {lift: the week its test single is in}. A lift that is absent is never
    # tested -- `row` -- and may therefore never exceed 100% at all.
    test_week: dict[str, int] = Field(default_factory=dict)
    # Per-pair overrides, in either order. Unset pairs fall back to the config's
    # same-lift or cross-lift default.
    rest_min_days: dict[tuple[str, str], int] = Field(default_factory=dict)
    plate_increment_kg: Decimal = Field(gt=0)
    # Lifts with a pain or injury note in the last 7 days (spec 12's keyword
    # screening, which is deterministic Python and never an embedding).
    pain_lifts: set[str] = Field(default_factory=set)
    # {lift: the highest % completed so far}, exact. The ceiling a painful lift
    # may not be progressed past.
    last_done_pct: dict[str, Fraction] = Field(default_factory=dict)
    # Session `seq`s the athlete has explicitly approved off their home weekday.
    approved_off_weekday: set[int] = Field(default_factory=set)
    # The caller's clock reading, passed in so this module has none.
    today: dt.date


def read_test_weeks(entries: Iterable[object]) -> dict[str, int]:
    """`{lift: the week of its test}`, read from the **template**.

    Named `read_test_weeks` rather than `test_weeks`: pytest collects any
    importable name beginning with `test_`, so the obvious name turns every
    module that imports it into a broken test.

    From the template and never from the plan under validation: a plan that has
    moved its own test single must not thereby move the week before which
    exceeding 100% is a violation. A lift with no test is simply absent, which
    `pct_early` reads as "may never exceed 100%".
    """
    found: dict[str, int] = {}
    for entry in entries:
        if not entry.is_test:
            continue
        previous = found.get(entry.lift_code)
        if previous is not None and previous != entry.week:
            raise ValueError(
                f"{entry.lift_code} has tests in weeks {previous} and {entry.week}; "
                "a lift is tested at most once per block"
            )
        found[entry.lift_code] = entry.week
    return found


# --- Formatting -----------------------------------------------------------
#
# Every message names the lift, week and slot (spec 05 §11), because the reader
# is an athlete looking at a calendar, not a developer reading a stack trace.


def _pct(pct: Fraction) -> str:
    """An exact intensity as a percentage, one decimal place, for display only."""
    hundred = pct * 100
    quotient = Decimal(hundred.numerator) / Decimal(hundred.denominator)
    return f"{quotient.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def _where(lift_code: str, session: DraftSession) -> str:
    return f"{lift_code} W{session.week} S{session.slot}"


def _session_label(session: DraftSession) -> str:
    """A session named by the lifts in it: `squat+bench W3 S1`."""
    lifts = list(dict.fromkeys(lift.lift_code for lift in session.lifts))
    return f"{'+'.join(lifts)} W{session.week} S{session.slot}"


def _ref(session: DraftSession) -> str:
    return f"seq:{session.seq}"


def _days(count: int) -> str:
    return f"{count} day{'' if count == 1 else 's'}"


# --- Entry points ---------------------------------------------------------


def validate(
    plan: Sequence[DraftSession],
    ctx: ValidationContext,
    *,
    changes: Sequence[SessionChange] = (),
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> ValidationResult:
    """Check a whole schedule. Violations reject it; warnings come with it.

    `plan` is the full resulting schedule, not a diff (spec 05 §5B). `changes`
    is keyword-only and optional because spec 05 §5C signs this function with
    the plan alone, yet `past_date` is a rule about a *requested change* rather
    than about the resulting plan — a session already sitting in the past is
    history, and only moving one there is a mistake (ADR-0006).

    Raises `ValueError` for malformed input — an unknown lift, a duplicated
    `seq` — and never for a broken rule (spec 05 §13).
    """
    _reject_malformed(plan, ctx)
    if not plan:
        # Nothing to break. Stated explicitly because every rule below would
        # also vacuously pass, and a reader should not have to prove that.
        return ValidationResult.of()

    ordered = sorted(plan, key=lambda session: session.seq)
    violations = (
        _check_off_weekday(ordered, ctx)
        + _check_one_per_day(ordered, ctx)
        + _check_order(ordered, ctx)
        + _check_rest_gap(ordered, ctx, config)
        + _check_intensity(ordered, ctx, config)
        + _check_plate_multiple(ordered, ctx)
        + _check_pain_progression(ordered, ctx)
        + _check_past_date(ordered, ctx, changes)
    )
    warnings = _check_prilepin(ordered) + _check_volume_trend(ordered)
    return ValidationResult.of(tuple(violations), tuple(warnings))


class StrengthValidator:
    """The strength domain's `planner_core.Validator`.

    A thin adapter: it unpacks a `StrengthPayload` into the plan and the changes
    that `validate` takes separately, so the engine's protocol stays
    domain-agnostic while the function underneath stays easy to call directly.
    """

    def __init__(self, config: ValidatorConfig = DEFAULT_CONFIG) -> None:
        self.config = config

    def validate(self, plan: StrengthPayload, context: ValidationContext) -> ValidationResult:
        return validate(
            plan.plan,
            context,
            changes=plan.changes,
            config=self.config,
        )


def _reject_malformed(plan: Sequence[DraftSession], ctx: ValidationContext) -> None:
    """Programming errors, which are not validation results (spec 05 §13).

    `DraftLift` already rejects an unknown lift on construction, so reaching
    here needs `model_construct` or a `model_copy` — but a silently ignored lift
    would mean a plan quietly unchecked, which is worse than a crash.
    """
    known = set(enums.LIFT_CODES)

    seen: set[int] = set()
    for session in plan:
        if session.seq in seen:
            raise ValueError(f"two sessions share seq {session.seq}")
        seen.add(session.seq)
        for lift in session.lifts:
            if lift.lift_code not in known:
                raise ValueError(f"unknown lift {lift.lift_code!r} in {_session_label(session)}")

    context_lifts = (
        set(ctx.test_week)
        | set(ctx.pain_lifts)
        | set(ctx.last_done_pct)
        | {lift for pair in ctx.rest_min_days for lift in pair}
    )
    unknown = sorted(context_lifts - known)
    if unknown:
        raise ValueError(f"unknown lift {unknown[0]!r} in the validation context")


# --- Hard rules: the calendar ---------------------------------------------
#
# These four judge today onward. A proposal cannot move a session that has
# already been trained, so a breach back there is a fact about the athlete's
# history rather than a reason to refuse the next change.


def _check_off_weekday(plan: Sequence[DraftSession], ctx: ValidationContext) -> list[Violation]:
    out = []
    for session in plan:
        if session.date < ctx.today or session.seq in ctx.approved_off_weekday:
            continue
        expected = ctx.weekday_map.get(session.slot)
        if expected is None:
            out.append(
                Violation(
                    rule="off_weekday",
                    message=(
                        f"{_session_label(session)}: Day {session.slot} has no weekday "
                        "in the athlete's map"
                    ),
                    refs=(_ref(session),),
                )
            )
        elif session.date.weekday() != expected:
            out.append(
                Violation(
                    rule="off_weekday",
                    message=(
                        f"{_session_label(session)}: {session.date} is a "
                        f"{WEEKDAY_NAMES[session.date.weekday()]}, but Day {session.slot} "
                        f"is pinned to {WEEKDAY_NAMES[expected]}"
                    ),
                    refs=(_ref(session),),
                )
            )
    return out


def _check_one_per_day(plan: Sequence[DraftSession], ctx: ValidationContext) -> list[Violation]:
    by_date: dict[dt.date, DraftSession] = {}
    out = []
    for session in plan:
        if session.date < ctx.today:
            continue
        first = by_date.get(session.date)
        if first is None:
            by_date[session.date] = session
            continue
        out.append(
            Violation(
                rule="one_per_day",
                message=(
                    f"{_session_label(session)}: {session.date} already holds "
                    f"{_session_label(first)}"
                ),
                refs=(_ref(first), _ref(session)),
            )
        )
    return out


def _check_order(plan: Sequence[DraftSession], ctx: ValidationContext) -> list[Violation]:
    """Dates strictly increasing in `seq` order.

    A repeated week is given fresh `seq`s by the solver, so it appends rather
    than interleaves and stays valid (spec 05 §5C).
    """
    out = []
    for earlier, later in zip(plan, plan[1:], strict=False):
        if earlier.date < ctx.today and later.date < ctx.today:
            continue
        if later.date <= earlier.date:
            out.append(
                Violation(
                    rule="order",
                    message=(
                        f"{_session_label(later)}: {later.date} is not after "
                        f"{_session_label(earlier)} on {earlier.date}"
                    ),
                    refs=(_ref(earlier), _ref(later)),
                )
            )
    return out


def rest_days_needed(
    lift_a: str,
    lift_b: str,
    overrides: Mapping[tuple[str, str], int],
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> int:
    """How many days this pair of lifts needs between them, in either order.

    Public because spec 07's calendar solver has to *build* schedules that
    satisfy the same rule this module judges them by, and it holds a
    `CalendarProblem` rather than a `ValidationContext`. Two implementations of
    one rule would drift, and the drift would show up as the solver confidently
    returning options the validator then rejects.
    """
    for pair in ((lift_a, lift_b), (lift_b, lift_a)):
        if pair in overrides:
            return overrides[pair]
    return config.same_lift_rest_days if lift_a == lift_b else config.cross_lift_rest_days


def _rest_days(lift_a: str, lift_b: str, ctx: ValidationContext, config: ValidatorConfig) -> int:
    return rest_days_needed(lift_a, lift_b, ctx.rest_min_days, config)


def _check_rest_gap(
    plan: Sequence[DraftSession], ctx: ValidationContext, config: ValidatorConfig
) -> list[Violation]:
    """Two sessions sharing a lift closer than that lift needs.

    Lifts inside one session are not a gap at all -- squat and bench on the same
    Monday is the authored template, not a mistake.
    """
    out = []
    for index, earlier in enumerate(plan):
        for later in plan[index + 1 :]:
            if earlier.date < ctx.today and later.date < ctx.today:
                continue
            gap = (later.date - earlier.date).days
            for lift_a in dict.fromkeys(lift.lift_code for lift in earlier.lifts):
                for lift_b in dict.fromkeys(lift.lift_code for lift in later.lifts):
                    needed = _rest_days(lift_a, lift_b, ctx, config)
                    if gap >= needed:
                        continue
                    out.append(
                        Violation(
                            rule="rest_gap",
                            message=(
                                f"{_where(lift_a, earlier)} to {_where(lift_b, later)}: "
                                f"only {_days(gap)} apart, and {needed} are needed"
                            ),
                            refs=(_ref(earlier), _ref(later)),
                        )
                    )
    return out


# --- Hard rules: the loading ----------------------------------------------
#
# These read the whole plan. A weight in week 10 is in the future whatever
# happened in week 2, and the arithmetic does not care what day it is.


def _check_intensity(
    plan: Sequence[DraftSession], ctx: ValidationContext, config: ValidatorConfig
) -> list[Violation]:
    """`pct_early`, `pct_cap` and `rel_intensity`, which all read the same set."""
    cap = Fraction(config.pct_cap)
    rel_cap = Fraction(config.rel_intensity_cap)
    overreach = Fraction(config.overreach_rel_intensity)

    out = []
    for session in plan:
        for lift in session.lifts:
            pct = lift.pct
            out += _check_pct(pct, cap, lift, session, ctx)
            # Recomputed from the pair and the reps rather than read from
            # `lift.rel_intensity`: that field is a 4 dp cache written by the
            # generator, and a solver that changed the reps without recomputing
            # it would otherwise be validated against a stale number.
            actual = Fraction(rep_max.rel_intensity(pct, lift.reps))
            if actual > rel_cap:
                out.append(
                    Violation(
                        rule="rel_intensity",
                        message=(
                            f"{_where(lift.lift_code, session)}: relative intensity "
                            f"{rep_max.rel_intensity(pct, lift.reps)} at {lift.reps} reps "
                            f"is above the {config.rel_intensity_cap} cap"
                        ),
                        refs=(_ref(session),),
                    )
                )
            elif actual > overreach and session.week < ctx.overreach_from_week:
                out.append(
                    Violation(
                        rule="rel_intensity",
                        message=(
                            f"{_where(lift.lift_code, session)}: relative intensity "
                            f"{rep_max.rel_intensity(pct, lift.reps)} at {lift.reps} reps "
                            f"is above {config.overreach_rel_intensity} before week "
                            f"{ctx.overreach_from_week}"
                        ),
                        refs=(_ref(session),),
                    )
                )
    return out


def _check_pct(
    pct: Fraction,
    cap: Fraction,
    lift: DraftLift,
    session: DraftSession,
    ctx: ValidationContext,
) -> list[Violation]:
    if pct > cap:
        return [
            Violation(
                rule="pct_cap",
                message=(
                    f"{_where(lift.lift_code, session)}: {_pct(pct)} is above the {_pct(cap)} cap"
                ),
                refs=(_ref(session),),
            )
        ]
    if pct <= 1:
        return []

    tested_in = ctx.test_week.get(lift.lift_code)
    if tested_in is None:
        return [
            Violation(
                rule="pct_early",
                message=(
                    f"{_where(lift.lift_code, session)}: {_pct(pct)} is above 100% and "
                    f"{lift.lift_code} has no test in this block"
                ),
                refs=(_ref(session),),
            )
        ]
    if session.week < tested_in - 1:
        return [
            Violation(
                rule="pct_early",
                message=(
                    f"{_where(lift.lift_code, session)}: {_pct(pct)} is above 100% "
                    f"before week {tested_in - 1}"
                ),
                refs=(_ref(session),),
            )
        ]
    return []


def _check_plate_multiple(plan: Sequence[DraftSession], ctx: ValidationContext) -> list[Violation]:
    """A weight the athlete cannot actually load.

    Exact `Decimal` remainder, never a float: 0.1 + 0.2 kg is the sort of
    arithmetic this project refuses to do (CLAUDE.md non-negotiable #4).
    """
    out = []
    for session in plan:
        for lift in session.lifts:
            if lift.weight_kg % ctx.plate_increment_kg != 0:
                out.append(
                    Violation(
                        rule="plate_multiple",
                        message=(
                            f"{_where(lift.lift_code, session)}: {lift.weight_kg} kg is not "
                            f"a multiple of {ctx.plate_increment_kg} kg"
                        ),
                        refs=(_ref(session),),
                    )
                )
    return out


def _check_pain_progression(
    plan: Sequence[DraftSession], ctx: ValidationContext
) -> list[Violation]:
    """No going heavier on a lift that hurt in the last week.

    With nothing completed for that lift there is no baseline, and the decision
    (ADR-0006) is to block rather than wave it through: an absent number is not
    evidence that the planned load is tolerable.
    """
    out = []
    for session in plan:
        if session.date < ctx.today:
            continue
        for lift_code in dict.fromkeys(lift.lift_code for lift in session.lifts):
            if lift_code not in ctx.pain_lifts:
                continue
            top = max(lift.pct for lift in session.lifts if lift.lift_code == lift_code)
            ceiling = ctx.last_done_pct.get(lift_code)
            if ceiling is None:
                out.append(
                    Violation(
                        rule="pain_progression",
                        message=(
                            f"{_where(lift_code, session)}: {lift_code} has a pain note from "
                            f"the last 7 days and no completed session to compare "
                            f"{_pct(top)} against"
                        ),
                        refs=(_ref(session),),
                    )
                )
            elif top > ceiling:
                out.append(
                    Violation(
                        rule="pain_progression",
                        message=(
                            f"{_where(lift_code, session)}: {_pct(top)} is above "
                            f"{_pct(ceiling)}, the highest completed intensity, and "
                            f"{lift_code} has a pain note from the last 7 days"
                        ),
                        refs=(_ref(session),),
                    )
                )
    return out


def _check_past_date(
    plan: Sequence[DraftSession],
    ctx: ValidationContext,
    changes: Sequence[SessionChange],
) -> list[Violation]:
    """A requested change that would place a session before today.

    Only `create` and `move` place a session. A `skip` names the day that was
    missed, which is necessarily in the past, and a `repeat_week` carries no
    date of its own.
    """
    by_seq = {session.seq: session for session in plan}
    out = []
    for change in changes:
        if change.action not in {"create", "move"} or change.to_date is None:
            continue
        if change.to_date >= ctx.today:
            continue
        session = by_seq.get(change.seq)
        label = _session_label(session) if session is not None else f"W{change.week} S{change.slot}"
        out.append(
            Violation(
                rule="past_date",
                message=(
                    f"{label}: a {change.action} to {change.to_date} is before today ({ctx.today})"
                ),
                refs=(f"seq:{change.seq}",),
            )
        )
    return out


# --- Soft rules -----------------------------------------------------------
#
# Warnings, deliberately. Both of these fire on the author's own block, and
# CLAUDE.md non-negotiable #5 says a rule that rejects it is miscalibrated.


def _zone(pct: Fraction) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Prilepin's row for this intensity, or None below 55%."""
    for lower, per_set, total in PRILEPIN_ZONES:
        if pct >= lower:
            return per_set, total
    return None


def _check_prilepin(plan: Sequence[DraftSession]) -> list[Violation]:
    """Reps per set and total reps against the zone, tests excluded.

    A test single is a maximal attempt, not a working set: by Prilepin's own
    terms it has no prescribed volume, so judging 1x1 against "4-10 total reps"
    would warn about the one set in the block whose job is to be a single.
    """
    out = []
    for session in plan:
        for lift in session.lifts:
            if lift.is_test:
                continue
            zone = _zone(lift.pct)
            if zone is None:
                continue
            (per_set_low, per_set_high), (total_low, total_high) = zone
            label = _where(lift.lift_code, session)
            pct = _pct(lift.pct)
            if not per_set_low <= lift.reps <= per_set_high:
                out.append(
                    Violation(
                        rule="prilepin_reps_per_set",
                        message=(
                            f"{label}: {lift.reps} reps per set at {pct} is outside "
                            f"Prilepin's {per_set_low}-{per_set_high} for that zone"
                        ),
                        refs=(_ref(session),),
                    )
                )
            total = lift.sets * lift.reps
            if not total_low <= total <= total_high:
                out.append(
                    Violation(
                        rule="prilepin_total_reps",
                        message=(
                            f"{label}: {lift.sets}x{lift.reps} = {total} reps at {pct} is "
                            f"outside Prilepin's {total_low}-{total_high} for that zone"
                        ),
                        refs=(_ref(session),),
                    )
                )
    return out


def _training_weeks(plan: Sequence[DraftSession]) -> list[list[DraftSession]]:
    """The plan split into the weeks the athlete will actually train.

    A run of consecutive sessions in `seq` order carrying the same week number
    — broken when the week number changes, *or* when a (week, slot) pair comes
    round again, which is what a repeated week looks like. Grouping by week
    number alone would merge a repeat into the original and read the doubled
    volume as a rise (ADR-0006).
    """
    runs: list[list[DraftSession]] = []
    seen: set[tuple[int, int]] = set()
    for session in plan:
        key = (session.week, session.slot)
        if not runs or session.week != runs[-1][0].week or key in seen:
            runs.append([session])
            seen = {key}
        else:
            runs[-1].append(session)
            seen.add(key)
    return runs


def _check_volume_trend(plan: Sequence[DraftSession]) -> list[Violation]:
    """A lift whose weekly reps and weekly top intensity both rise.

    Classic programming advice: raise one or the other, not both. The authored
    template does it four times anyway, which is why this warns.
    """
    weeks = _training_weeks(plan)
    out = []
    for earlier, later in zip(weeks, weeks[1:], strict=False):
        before = _weekly_load(earlier)
        after = _weekly_load(later)
        for lift_code in sorted(before.keys() & after.keys()):
            reps_before, top_before = before[lift_code]
            reps_after, top_after = after[lift_code]
            if reps_after > reps_before and top_after > top_before:
                out.append(
                    Violation(
                        rule="volume_trend",
                        message=(
                            f"{lift_code} W{earlier[0].week} to W{later[0].week}: total reps "
                            f"rise {reps_before} to {reps_after} while top intensity rises "
                            f"{_pct(top_before)} to {_pct(top_after)}"
                        ),
                        refs=(_ref(later[-1]),),
                    )
                )
    return out


def _weekly_load(sessions: Sequence[DraftSession]) -> dict[str, tuple[int, Fraction]]:
    """`{lift: (total reps, top intensity)}` across one training week."""
    load: dict[str, tuple[int, Fraction]] = {}
    for session in sessions:
        for lift in session.lifts:
            reps, top = load.get(lift.lift_code, (0, Fraction(0)))
            load[lift.lift_code] = (reps + lift.sets * lift.reps, max(top, lift.pct))
    return load
