"""Which day each remaining session lands on, when something changes.

This is the "when" half of the optimisation (spec 09 is the "what"). A session
was missed, the athlete is away for ten days, a meet is fixed on a date — the
template's Mon/Wed/Fri pattern no longer fits, and something has to give. This
decides what.

It returns **2-3 options**, not one answer, each with its cost broken down into
the three things that can be sacrificed:

* `delay` — a session trained later than the template wanted it
* `off_weekday` — a session off its home weekday, so the week's rhythm breaks
* `end_slip` — the whole block, and therefore the max test, finishing later

Those three cannot be reconciled into a single number that means anything to a
lifter, which is the point: the agent explains the trade-off and the human
picks. The weights (1 / 3 / 10) only order the options for presentation.

Four properties it is built around:

1. **Pure.** No database, no clock, no randomness. `today` arrives on the
   problem, and the only seed CP-SAT gets is the one passed in. Same inputs,
   same schedule — which is what lets a test assert exact dates.
2. **It never decides anything is acceptable.** Every option is put through
   the spec 05 validator before it is returned, and one that fails is dropped
   and logged rather than explained away. That gate is a *required* argument,
   so it cannot be forgotten (see `solve`).
3. **It only moves dates.** Weights, sets and reps are carried through
   untouched. Nothing here recomputes a kilogram (CLAUDE.md non-negotiable #1).
4. **It cannot rewrite history.** A session the athlete already did or already
   missed is pinned to the day it happened. It still constrains everything
   around it — the rest gap from last Monday's squat is just as real as the one
   from next Monday's — but no option may re-date it (ADR-0007).

Day indexes are relative to `today` throughout; dates exist only at the edges
(`problem_from_plan` coming in, `to_plan` and `to_changes` going out). A
negative index is a session whose planned day has already passed, which is
exactly the missed session this module exists to re-place.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Literal

from ortools.sat.python import cp_model
from ortools.util.python.sorted_interval_list import Domain
from pydantic import BaseModel, ConfigDict, Field, model_validator

from domains.strength.schemas import DraftSession, SessionChange
from domains.strength.validator import (
    DEFAULT_CONFIG,
    ValidationContext,
    ValidatorConfig,
    rest_days_needed,
    validate,
)
from planner_core.proposal import ValidationResult

logger = logging.getLogger(__name__)

# How far past the block's own end a session may be pushed before the problem is
# called infeasible rather than solved. Four weeks is a **starting value**: it
# is one mesocycle, long enough to absorb an illness and short enough that a
# block sliding further is a conversation, not an optimisation (CONTEXT.md §9's
# "after a long break" question is the real answer here).
DEFAULT_HORIZON_SLACK_DAYS = 28

# An option costing more than this multiple of the best is not a trade-off, it
# is a worse answer, and offering it only makes the choice harder (spec 07 §5B).
COST_RATIO_LIMIT = 3


class CostWeights(BaseModel):
    """What each sacrifice is worth, relative to one day of delay.

    **Starting values.** The ordering they encode is the defensible part: the
    block finishing late (and with it the max test) is worse than a session
    drifting off Wednesday, which is worse than a session simply being later.
    The exact 1 / 3 / 10 is spec 07 §5A's guess and is presented as one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    delay: int = Field(default=1, ge=0)
    off_weekday: int = Field(default=3, ge=0)
    end_slip: int = Field(default=10, ge=0)


class CostBreakdown(BaseModel):
    """What one option costs, and in which currency.

    Integers, not floats: these are counts of days and sessions, and `total` is
    an exact weighted sum that a test compares against CP-SAT's objective.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    delay_days: int = Field(ge=0)
    off_weekday_count: int = Field(ge=0)
    end_slip_days: int = Field(ge=0)


class SolverStats(BaseModel):
    """What the solve itself did, for spec 14's trace.

    Attached to every option and to `Infeasible`, because "we looked for 5
    seconds and found nothing" and "it is provably impossible" are different
    answers and the user deserves to know which one they got.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    objective: int | None = None
    wall_time_s: float = Field(ge=0)
    num_booleans: int = Field(ge=0)


class SessionToPlace(BaseModel):
    """One session the solver may move, or one it must schedule around.

    `pinned_date` set means the day is a fact, not a decision: the session was
    done, or was missed, or the human fixed it. It still participates in every
    constraint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    # Lift codes only. The solver has no use for sets, reps or kilograms, and
    # not carrying them makes it impossible for this module to change one.
    lifts: frozenset[str] = Field(min_length=1)
    original_date: dt.date
    pinned_date: dt.date | None = None

    @property
    def is_pinned(self) -> bool:
        return self.pinned_date is not None


class CalendarProblem(BaseModel):
    """Everything the solver needs, as plain data.

    Built from the plan by `problem_from_plan`; held separately from
    `ValidationContext` because the solver constructs schedules where the
    validator judges them, and the two need different things.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sessions: tuple[SessionToPlace, ...] = ()
    today: dt.date
    # Pass None (or leave it out) for "the last original date + 28 days", which
    # `_default_horizon` fills in before validation. Not `date | None` after
    # that point: a horizon is required to build a domain at all, and an
    # optional type here would put a `None` check at every use.
    horizon_end: dt.date
    # A meet or a test day: the last session must land on or before it.
    hard_end_date: dt.date | None = None
    # {slot: weekday}, 0 = Monday, as everywhere else in the domain.
    weekday_map: dict[int, int] = Field(default_factory=dict)
    # May a session leave its home weekday at all? False keeps the week's shape
    # and pays for it in `end_slip`; True trades the shape for a sooner finish.
    allow_flex_days: bool = False
    unavailable: frozenset[dt.date] = frozenset()
    # Per-pair rest overrides, in either order, as `ValidationContext` holds
    # them. Unset pairs fall back to `config`.
    rest_min_days: dict[tuple[str, str], int] = Field(default_factory=dict)
    # The same thresholds the validator will judge the result by. Carried whole
    # rather than as two ints so there is one source for the rest rule.
    config: ValidatorConfig = DEFAULT_CONFIG
    weights: CostWeights = CostWeights()

    @model_validator(mode="before")
    @classmethod
    def _default_horizon(cls, data: object) -> object:
        """Fill in `horizon_end` before the field is typed, not after.

        Spec 07 §5A gives `horizon_end` a default of "original block end + 28
        days", which depends on another field and so cannot be a plain default.
        Doing it here rather than in an `after` validator keeps the field
        non-optional, which is what lets the rest of the module treat a horizon
        as a fact.
        """
        if not isinstance(data, dict) or data.get("horizon_end") is not None:
            return data

        today = data.get("today")
        if not isinstance(today, dt.date):
            # Let the field validators report the real problem.
            return data

        def original(session: object) -> dt.date:
            if isinstance(session, SessionToPlace):
                return session.original_date
            if isinstance(session, dict) and isinstance(session.get("original_date"), dt.date):
                return session["original_date"]
            return today

        sessions = data.get("sessions") or ()
        last = max((original(session) for session in sessions), default=today)
        return {
            **data,
            "horizon_end": max(last, today) + dt.timedelta(days=DEFAULT_HORIZON_SLACK_DAYS),
        }

    @model_validator(mode="after")
    def _check_setup(self) -> CalendarProblem:
        """Setup errors raise; an unsatisfiable *schedule* does not.

        The difference matters. A duplicate `seq` or a slot with no home
        weekday is a bug in the caller, and reporting it as "infeasible" would
        send the agent off explaining a trade-off that does not exist. A
        calendar that genuinely does not fit is this module's whole subject and
        comes back as `Infeasible`.
        """
        seqs = [s.seq for s in self.sessions]
        if len(seqs) != len(set(seqs)):
            dupes = sorted({seq for seq in seqs if seqs.count(seq) > 1})
            raise ValueError(f"duplicate seq in sessions: {dupes}")

        if self.today > self.horizon_end:
            raise ValueError(f"today {self.today} is after horizon_end {self.horizon_end}")
        if self.hard_end_date is not None and self.hard_end_date < self.today:
            raise ValueError(
                f"hard_end_date {self.hard_end_date} is before today {self.today}: "
                "a date that has passed cannot be met, and relaxing it is the "
                "caller's decision to make"
            )

        missing = sorted(
            {s.slot for s in self.sessions if not s.is_pinned and s.slot not in self.weekday_map}
        )
        if missing:
            raise ValueError(
                f"slots {missing} have no home weekday in weekday_map: a session "
                "with nowhere to belong cannot be placed or judged"
            )
        return self

    @property
    def horizon_days(self) -> int:
        """The last placeable day index, inclusive."""
        return (self.horizon_end - self.today).days

    def day_of(self, index: int) -> dt.date:
        return self.today + dt.timedelta(days=index)

    def index_of(self, day: dt.date) -> int:
        return (day - self.today).days


class ScheduleOption(BaseModel):
    """One way the remaining sessions could be laid out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # {seq: date}, every session the solver was asked to place, pinned ones
    # included -- so a caller can apply an option without consulting the
    # problem again, and `to_changes` can tell moved from unmoved.
    assignments: dict[int, dt.date]
    cost: CostBreakdown
    status: Literal["OPTIMAL", "FEASIBLE"]
    stats: SolverStats


class Infeasible(BaseModel):
    """No schedule fits, and what would have to give for one to.

    Deliberately not a thrown exception: "your calendar does not fit unless you
    drop the meet date" is an answer to be explained, not an error.

    When a relaxation works, this reports it rather than returning the relaxed
    options. Going off-weekday or abandoning a meet date changes what the
    athlete asked for, and spec 08 re-solves with the relaxed problem once a
    human has agreed to it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    relaxations_tried: tuple[str, ...] = ()
    # The first relaxation that produced a feasible schedule, if any did.
    relaxation: str | None = None
    stats: SolverStats | None = None


# The spec 05 gate, as a function so `solve` can apply it without knowing how a
# plan is validated. `validator_check` builds the real one.
OptionCheck = Callable[[ScheduleOption], ValidationResult]


# --- Building the problem from a plan --------------------------------------


def problem_from_plan(
    plan: Sequence[DraftSession],
    *,
    today: dt.date,
    weekday_map: Mapping[int, int],
    history_seqs: Collection[int] = (),
    allow_flex_days: bool = False,
    unavailable: Iterable[dt.date] = (),
    hard_end_date: dt.date | None = None,
    horizon_end: dt.date | None = None,
    rest_min_days: Mapping[tuple[str, str], int] | None = None,
    config: ValidatorConfig = DEFAULT_CONFIG,
    weights: CostWeights | None = None,
) -> CalendarProblem:
    """A `CalendarProblem` from the **whole** plan, history included.

    `plan` is the full schedule, not the remaining tail: ADR-0006 has callers
    pass everything and never pre-filter, because a rest gap is measured
    against the session before it whether or not that session has happened.

    `history_seqs` are the sessions whose dates are facts — status `done` or
    `missed`. They are pinned. Everything else is free to move, including a
    session whose planned date is already in the past: that is the missed
    session nobody has logged yet, and re-placing it is the job.
    """
    history = set(history_seqs)
    sessions = tuple(
        SessionToPlace(
            seq=session.seq,
            week=session.week,
            slot=session.slot,
            lifts=frozenset(lift.lift_code for lift in session.lifts),
            original_date=session.date,
            pinned_date=session.date if session.seq in history else None,
        )
        for session in sorted(plan, key=lambda s: s.seq)
    )
    return CalendarProblem(
        sessions=sessions,
        today=today,
        horizon_end=horizon_end,
        hard_end_date=hard_end_date,
        weekday_map=dict(weekday_map),
        allow_flex_days=allow_flex_days,
        unavailable=frozenset(unavailable),
        rest_min_days=dict(rest_min_days or {}),
        config=config,
        weights=weights or CostWeights(),
    )


# --- Cost, computed in plain Python ----------------------------------------


def cost_of(problem: CalendarProblem, assignments: Mapping[int, dt.date]) -> CostBreakdown:
    """What this layout costs, independent of CP-SAT.

    The objective is defined twice on purpose: once as constraints for the
    solver and once here. The tests assert the two agree, which is how a
    mis-stated objective gets caught rather than silently optimising for
    something nobody asked for.
    """
    free = [s for s in problem.sessions if not s.is_pinned]
    delay_days = 0
    off_weekday = 0
    for session in free:
        placed = assignments[session.seq]
        delay_days += max(0, (placed - session.original_date).days)
        if placed.weekday() != problem.weekday_map.get(session.slot):
            off_weekday += 1

    end_slip = 0
    if free:
        last = max(problem.sessions, key=lambda s: s.seq)
        if not last.is_pinned:
            end_slip = max(0, (assignments[last.seq] - last.original_date).days)

    weights = problem.weights
    total = (
        weights.delay * delay_days + weights.off_weekday * off_weekday + weights.end_slip * end_slip
    )
    return CostBreakdown(
        total=total,
        delay_days=delay_days,
        off_weekday_count=off_weekday,
        end_slip_days=end_slip,
    )


# --- Applying an option -----------------------------------------------------


Placed = ScheduleOption | Mapping[int, dt.date]


def _assignments(placed: Placed) -> Mapping[int, dt.date]:
    """The seq-to-date map, however it arrived.

    A `ScheduleOption` is what this module produces, but not every re-dating
    comes from CP-SAT: spec 08's week-shift is a `+7` translation it would be
    dishonest to dress up as a solve, with a fabricated status and wall time.
    Letting both forms in keeps the "what counts as a move" rule below in one
    place, which is the half that actually matters -- the approval path reads
    it to decide which weekday waivers it is granting.
    """
    return placed.assignments if isinstance(placed, ScheduleOption) else placed


def to_plan(draft_sessions: Sequence[DraftSession], placed: Placed) -> tuple[DraftSession, ...]:
    """The plan with these dates applied. Nothing else changes.

    `model_copy` rather than mutation: `DraftSession` is frozen, and the
    weights it carries must arrive at the validator exactly as the generator
    computed them.
    """
    assignments = _assignments(placed)
    known = {session.seq for session in draft_sessions}
    unknown = sorted(set(assignments) - known)
    if unknown:
        raise ValueError(f"option places seqs that are not in the plan: {unknown}")

    return tuple(
        session.model_copy(update={"date": assignments[session.seq]})
        if session.seq in assignments
        else session
        for session in sorted(draft_sessions, key=lambda s: s.seq)
    )


def to_changes(draft_sessions: Sequence[DraftSession], placed: Placed) -> tuple[SessionChange, ...]:
    """One `move` per session whose date actually changed, and nothing else.

    Exactly the sessions placed, no more (ADR-0007): the approval path derives
    `approved_off_weekday` from this list, so a change listed here is a
    weekday rule waived. Over-reporting would quietly widen that waiver to
    sessions nobody agreed to move.
    """
    assignments = _assignments(placed)
    return tuple(
        SessionChange(
            seq=session.seq,
            week=session.week,
            slot=session.slot,
            action="move",
            from_date=session.date,
            to_date=assignments[session.seq],
        )
        for session in sorted(draft_sessions, key=lambda s: s.seq)
        if session.seq in assignments and assignments[session.seq] != session.date
    )


def validator_check(
    draft_sessions: Sequence[DraftSession],
    ctx: ValidationContext,
    *,
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> OptionCheck:
    """The real spec 05 gate, as an `OptionCheck`.

    The waiver is built from the option's own moves, which is precisely what
    `services.validation_context` derives at approval time. So an option that
    passes here passes there — if this used a wider waiver, the solver would
    hand back options that fail the moment a human clicks approve.
    """

    def check(option: ScheduleOption) -> ValidationResult:
        plan = to_plan(draft_sessions, option)
        changes = to_changes(draft_sessions, option)
        moved = {change.seq for change in changes}
        option_ctx = ctx.model_copy(
            update={"approved_off_weekday": set(ctx.approved_off_weekday) | moved}
        )
        return validate(plan, option_ctx, changes=changes, config=config)

    return check


# --- The model --------------------------------------------------------------


def _allowed_days(problem: CalendarProblem, session: SessionToPlace) -> list[int]:
    """Every day index this session may take.

    Its home weekday (or any day, with flex), minus the unavailable ones, minus
    anything past the horizon, and never before today.
    """
    home = problem.weekday_map.get(session.slot)
    days = []
    for index in range(0, problem.horizon_days + 1):
        day = problem.day_of(index)
        if day in problem.unavailable:
            continue
        if not problem.allow_flex_days and day.weekday() != home:
            continue
        days.append(index)
    return days


def _rest_needed(problem: CalendarProblem, a: SessionToPlace, b: SessionToPlace) -> int:
    """The widest gap any lift pair across these two sessions needs."""
    return max(
        rest_days_needed(lift_a, lift_b, problem.rest_min_days, problem.config)
        for lift_a in a.lifts
        for lift_b in b.lifts
    )


def _order_conflict(problem: CalendarProblem, ordered: Sequence[SessionToPlace]) -> str | None:
    """A free session trapped before a pinned day that has already passed.

    The real case: Monday was missed, Wednesday was trained anyway, and it is
    now Thursday. Monday's session must come before Wednesday's to keep `seq`
    order, and it cannot, because the only days left are after today. No
    relaxation helps — the block has to be reshaped (spec 08's step-back), not
    rescheduled — so this is reported directly instead of walking the ladder.
    """
    for position, session in enumerate(ordered):
        if session.is_pinned:
            continue
        for later in ordered[position + 1 :]:
            if later.is_pinned and later.pinned_date is not None:
                # The free session needs a day >= today, and strict order needs
                # at least one day per session in between.
                earliest = problem.index_of(later.pinned_date) - 1
                if earliest < 0:
                    return (
                        f"order: seq {session.seq} must be placed from {problem.today} "
                        f"onward but has to fall before seq {later.seq}, already "
                        f"fixed on {later.pinned_date}"
                    )
                break
    return None


class _Model:
    """The CP-SAT model plus the handles needed to read a solution back."""

    def __init__(self, problem: CalendarProblem) -> None:
        self.problem = problem
        self.ordered = sorted(problem.sessions, key=lambda s: s.seq)
        self.model = cp_model.CpModel()
        # seq -> an IntVar for a free session, or a plain int for a pinned one.
        self.day: dict[int, cp_model.IntVar | int] = {}
        self.free: list[SessionToPlace] = []
        self.empty_domain: SessionToPlace | None = None

        for session in self.ordered:
            if session.pinned_date is not None:
                self.day[session.seq] = problem.index_of(session.pinned_date)
                continue
            allowed = _allowed_days(problem, session)
            if not allowed:
                # CP-SAT would reject an empty domain as MODEL_INVALID, which
                # reads as a bug rather than the honest answer: there is no day
                # this session is allowed to take.
                self.empty_domain = session
                return
            self.day[session.seq] = self.model.new_int_var_from_domain(
                Domain.from_values(allowed), f"d{session.seq}"
            )
            self.free.append(session)

        self._add_constraints()
        self._add_objective()

    def _is_past(self, session: SessionToPlace) -> bool:
        """Fixed on a day that has already gone by."""
        return session.pinned_date is not None and session.pinned_date < self.problem.today

    # Both sessions already behind us: the validator skips such a pair, and so
    # must the solver, or an old week the athlete trained through would make
    # today's reschedule impossible.
    def _both_past(self, a: SessionToPlace, b: SessionToPlace) -> bool:
        return self._is_past(a) and self._is_past(b)

    def _add_constraints(self) -> None:
        ordered = self.ordered

        # Template order. Strictly increasing, which also gives one-per-day.
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if self._both_past(earlier, later):
                continue
            self.model.add(self.day[earlier.seq] < self.day[later.seq])

        # One session per day. Implied by the above, but stated because it
        # propagates better and because spec 07 §5B asks for it by name.
        live = [self.day[s.seq] for s in ordered if not self._is_past(s)]
        if len(live) > 1:
            self.model.add_all_different(live)

        # Rest between lifts, every pair, exactly as the validator measures it.
        for position, earlier in enumerate(ordered):
            for offset, later in enumerate(ordered[position + 1 :], start=1):
                if self._both_past(earlier, later):
                    continue
                needed = _rest_needed(self.problem, earlier, later)
                # Strict order already puts `offset` days between them, so only
                # a gap wider than that needs stating. Keeps the model small on
                # a 33-session block without weakening anything.
                if needed <= offset:
                    continue
                self.model.add(self.day[later.seq] - self.day[earlier.seq] >= needed)

        if self.problem.hard_end_date is not None and ordered:
            last = ordered[-1]
            self.model.add(self.day[last.seq] <= self.problem.index_of(self.problem.hard_end_date))

    def _add_objective(self) -> None:
        problem = self.problem
        horizon = problem.horizon_days
        terms = []

        # Off-weekday, as a boolean per free session.
        #
        # Via `d % 7` rather than spec 07 §5B's `AddElement` over a 0/1 list
        # per day: the home weekdays are an arithmetic progression, so one
        # modulo says what an 88-entry table would, and the table version made
        # the 33-session block take seconds instead of milliseconds.
        for session in self.free:
            home = problem.weekday_map[session.slot]
            # Which remainder lands on the home weekday, given day 0 is today.
            offset = (home - problem.today.weekday()) % 7
            remainder = self.model.new_int_var(0, 6, f"wd{session.seq}")
            self.model.add_modulo_equality(remainder, self.day[session.seq], 7)
            is_home = self.model.new_bool_var(f"home{session.seq}")
            self.model.add(remainder == offset).only_enforce_if(is_home)
            self.model.add(remainder != offset).only_enforce_if(~is_home)
            terms.append(problem.weights.off_weekday * (1 - is_home))

        # Delay. `max(0, ...)` so placing a session earlier than planned is
        # free rather than a saving -- the solver must not pull week 9 forward
        # to pay for week 3 being late.
        for session in self.free:
            original = problem.index_of(session.original_date)
            ceiling = max(0, horizon - min(original, 0))
            late = self.model.new_int_var(0, ceiling, f"late{session.seq}")
            self.model.add_max_equality(late, [self.day[session.seq] - original, 0])
            terms.append(problem.weights.delay * late)

        # End slip: the last session, and with it the max test, finishing late.
        if self.ordered:
            last = self.ordered[-1]
            if not last.is_pinned:
                original = problem.index_of(last.original_date)
                slip = self.model.new_int_var(0, max(0, horizon - min(original, 0)), "slip")
                self.model.add_max_equality(slip, [self.day[last.seq] - original, 0])
                terms.append(problem.weights.end_slip * slip)

        if terms:
            self.model.minimize(sum(terms))

    def forbid(self, assignments: Mapping[int, dt.date]) -> None:
        """A no-good cut: at least one free session must move off this day.

        Without it the second solve returns the first answer again.
        """
        literals = []
        for session in self.free:
            same = self.model.new_bool_var(f"same{session.seq}_{assignments[session.seq]}")
            index = self.problem.index_of(assignments[session.seq])
            self.model.add(self.day[session.seq] == index).only_enforce_if(same)
            self.model.add(self.day[session.seq] != index).only_enforce_if(~same)
            literals.append(~same)
        if literals:
            self.model.add_bool_or(literals)

    def read(self, solver: cp_model.CpSolver) -> dict[int, dt.date]:
        return {
            session.seq: self.problem.day_of(int(solver.value(self.day[session.seq])))
            for session in self.ordered
        }


_STATUS_NAMES = {cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE"}


def _solve_once(
    built: _Model, *, time_limit_s: float, seed: int, num_workers: int
) -> tuple[int, cp_model.CpSolver]:
    """One CP-SAT call. A seam, so a test can force a time-out."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(time_limit_s, 0.01)
    solver.parameters.num_workers = num_workers
    solver.parameters.random_seed = seed
    return solver.solve(built.model), solver


def _stats(status: int, solver: cp_model.CpSolver | None) -> SolverStats:
    name = _STATUS_NAMES.get(status) or (
        solver.status_name(status) if solver is not None else "UNKNOWN"
    )
    if solver is None:
        return SolverStats(status=name, wall_time_s=0.0, num_booleans=0)
    objective = int(round(solver.objective_value)) if status in _STATUS_NAMES else None
    return SolverStats(
        status=name,
        objective=objective,
        wall_time_s=solver.wall_time,
        num_booleans=solver.response_proto.num_booleans,
    )


def _empty_option() -> ScheduleOption:
    """Nothing to place, so nothing is sacrificed."""
    return ScheduleOption(
        assignments={},
        cost=CostBreakdown(total=0, delay_days=0, off_weekday_count=0, end_slip_days=0),
        status="OPTIMAL",
        stats=SolverStats(status="OPTIMAL", objective=0, wall_time_s=0.0, num_booleans=0),
    )


# --- The entry point --------------------------------------------------------


def solve(
    problem: CalendarProblem,
    n_options: int = 3,
    time_limit_s: float = 5.0,
    seed: int = 0,
    *,
    check: OptionCheck,
    num_workers: int = 8,
) -> tuple[ScheduleOption, ...] | Infeasible:
    """2-3 distinct schedules, cheapest first, each already validated.

    `check` is a required keyword rather than an optional extra: spec 07 §11
    says every option must pass the spec 05 validator before it is returned,
    and a defaulted gate is a gate that gets skipped in the one call that
    mattered. `validator_check` builds the real one; a test may pass its own.

    `time_limit_s` is a **total** budget across every solve, not per solve, so
    asking for three options cannot take three times as long as the caller
    allowed. Spent time is read from the solver rather than a clock, which is
    what keeps this module free of `datetime.now()`.

    Returns options, or `Infeasible` naming what would have to give.
    """
    if not problem.sessions:
        option = _empty_option()
        result = check(option)
        if not result.ok:
            return Infeasible(
                reason=f"empty schedule rejected: {_rules(result)}",
                stats=option.stats,
            )
        return (option,)

    ladder: list[tuple[str, CalendarProblem]] = [("", problem)]
    for name, relaxed in _relaxations(problem):
        ladder.append((name, relaxed))

    tried: list[str] = []
    budget = time_limit_s
    last_stats: SolverStats | None = None

    for name, candidate in ladder:
        if name:
            tried.append(name)
        options, stats, reason = _solve_candidate(
            candidate,
            n_options=n_options if not name else 1,
            budget=budget,
            seed=seed,
            num_workers=num_workers,
            check=check,
        )
        last_stats = stats or last_stats
        if stats is not None:
            budget = max(budget - stats.wall_time_s, 0.01)

        if not name:
            # The problem as posed.
            if options:
                return options
            if reason is not None and not reason.startswith("infeasible"):
                # A time-out, or every option rejected: relaxing the calendar
                # does not address either, so say so plainly.
                return Infeasible(reason=reason, stats=stats)
            conflict = _order_conflict(problem, sorted(problem.sessions, key=lambda s: s.seq))
            if conflict is not None:
                return Infeasible(reason=conflict, stats=stats)
            continue

        if options:
            return Infeasible(
                reason=f"infeasible as posed; feasible after: {name}",
                relaxations_tried=tuple(tried),
                relaxation=name,
                stats=stats,
            )

    return Infeasible(
        reason="infeasible after all relaxations",
        relaxations_tried=tuple(tried),
        stats=last_stats,
    )


def _relaxations(problem: CalendarProblem) -> list[tuple[str, CalendarProblem]]:
    """The ladder from spec 07 §5B, cumulative, skipping no-ops.

    Cumulative because the relaxations are not alternatives: if allowing flex
    days was not enough, the next question is whether flex days *and* dropping
    the meet date would be.
    """
    out: list[tuple[str, CalendarProblem]] = []
    current = problem
    if not problem.allow_flex_days:
        current = current.model_copy(update={"allow_flex_days": True})
        out.append(("allow_flex_days", current))
    if problem.hard_end_date is not None:
        current = current.model_copy(update={"hard_end_date": None})
        out.append(("drop hard_end_date", current))
    current = current.model_copy(
        update={"horizon_end": problem.horizon_end + dt.timedelta(days=DEFAULT_HORIZON_SLACK_DAYS)}
    )
    out.append((f"extend horizon_end by {DEFAULT_HORIZON_SLACK_DAYS} days", current))
    return out


def _solve_candidate(
    problem: CalendarProblem,
    *,
    n_options: int,
    budget: float,
    seed: int,
    num_workers: int,
    check: OptionCheck,
) -> tuple[tuple[ScheduleOption, ...], SolverStats | None, str | None]:
    """Solve one form of the problem, returning validated options."""
    built = _Model(problem)
    if built.empty_domain is not None:
        session = built.empty_domain
        return (
            (),
            None,
            (
                f"infeasible: seq {session.seq} (week {session.week} slot {session.slot}) "
                "has no allowed day left in the horizon"
            ),
        )

    found: list[ScheduleOption] = []
    rejected: list[str] = []
    stats: SolverStats | None = None
    remaining = budget

    for attempt in range(n_options):
        status, solver = _solve_once(
            built, time_limit_s=remaining, seed=seed, num_workers=num_workers
        )
        stats = _stats(status, solver)
        remaining = max(remaining - solver.wall_time, 0.01)

        if status == cp_model.MODEL_INVALID:
            raise RuntimeError(f"CP-SAT rejected the model: {built.model.validate()}")
        if status not in _STATUS_NAMES:
            if attempt == 0:
                if status == cp_model.INFEASIBLE:
                    return (), stats, "infeasible: no schedule satisfies the constraints"
                return (), stats, "time limit"
            break

        assignments = built.read(solver)
        option = ScheduleOption(
            assignments=assignments,
            cost=cost_of(problem, assignments),
            status=_STATUS_NAMES[status],
            stats=stats,
        )
        result = check(option)
        if result.ok:
            found.append(option)
        else:
            rules = _rules(result)
            rejected.append(rules)
            logger.warning(
                "calendar option dropped by the validator: %s (cost %d)",
                rules,
                option.cost.total,
            )

        if not built.free:
            break
        built.forbid(assignments)

    if not found:
        if rejected:
            return (), stats, f"validator rejected every option: {'; '.join(rejected)}"
        return (), stats, "infeasible: no schedule satisfies the constraints"

    best = min(option.cost.total for option in found)
    kept = [option for option in found if option.cost.total <= COST_RATIO_LIMIT * best]
    kept.sort(key=lambda option: (option.cost.total, sorted(option.assignments.items())))
    return tuple(kept), stats, None


def _rules(result: ValidationResult) -> str:
    return ", ".join(sorted({violation.rule for violation in result.violations}))
