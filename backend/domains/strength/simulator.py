"""What the rest of a block might look like. Spec 10, as amended (ADR-0012).

A Monte-Carlo simulation: the unfinished part of a block is played out about a
thousand times with random illness and travel, and the answer is a *range* --
when the block ends, how likely each test single is to be made, where each 1RM
might land. It is a decision aid, never a prediction of a body (CLAUDE.md
non-negotiable #8): every result carries the assumptions it rests on, and
every constant behind them is a starting value (CLAUDE.md guardrail).

**How a run works.** One SimPy `Environment`, one unit of time per day, three
processes:

* the *athlete* walks the sessions still to do in `seq` order and performs each
  one unless a disruption covers its date;
* an *illness* process and a *travel* process open unavailable windows.

When a window closes the athlete does not improvise a new schedule. It asks
`missed_policy.propose` -- the same policy the real planner uses, with
`fast=True` -- and takes the first option it returns (the lowest cost). That
is why the simulator inherits the policy's tiers: a long enough absence becomes
a `week_shift`, and two empty weeks become a `restart`, exactly as they would
for the athlete.

**What fitness and fatigue do.** A Banister impulse-response model turns the
sessions actually performed into a readiness figure, normalised against a
clean run of the same plan so that readiness is 1.0 where the plan expected the
athlete to be. A test single is made or missed by a sigmoid on how far readiness
falls short of what the test asks (`SimConfig.test_demand`). Only test singles
are sampled: the spec gives a missed ordinary set no consequence, so sampling
one would cost time and change nothing.

**What it may not do.** It never writes, never reads the database and has no
clock of its own: randomness arrives only as `numpy.random.Generator` objects
spawned from the seed, so one seed is one answer. `time.perf_counter` appears
once, to report how long the run took, and that figure is the one thing two
identical runs may disagree on. The 1RMs it projects are projections: nothing
here is ever a measured max (CLAUDE.md non-negotiable #9, ADR-0005 rule 6).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Literal

import numpy as np
import simpy
from pydantic import BaseModel, ConfigDict, Field, model_validator

from domains.strength import missed_policy, rep_max
from domains.strength.missed_policy import DEFAULT_MISSED_POLICY, MissedEvent, MissedPolicyConfig
from domains.strength.schemas import (
    BlockState,
    DraftLift,
    DraftSession,
    ForcedEvent,
)
from domains.strength.validator import ValidationContext

# Where `scripts/calibrate_simulator.py` writes. Absent until the athlete runs
# it against the OpenPowerlifting export, which is why the fallback below exists.
GAINS_PATH = Path(__file__).parent / "calibration" / "gains.json"

# The quantiles `gains.json` records, in the order the sampler interpolates.
_QUANTILE_POINTS = (0.10, 0.25, 0.50, 0.75, 0.90)

# A guard, not a model parameter: a run that needs more re-plans than this is
# not converging, and is reported as unplannable instead of looping.
_MAX_REPLANS_PER_RUN = 200
# Likewise for a window that keeps being extended by the next one (a trip every
# week of a long block): bounded rather than followed forever.
_MAX_RECHECKS = 50

RunStatus = Literal["finished", "restart", "unplannable"]
TestStatus = Literal["pending", "behind", "none"]


# --- Configuration ---------------------------------------------------------


class SimConfig(BaseModel):
    """Every knob of the simulation. Every number here is a starting value.

    None of these is fitted to the athlete. They are a first guess at
    plausible behaviour, kept in one place so they can be tuned, and listed in
    full in `SimulationResult.assumptions` so a reader never mistakes them for
    measurements.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int = Field(
        default=1000, ge=1, le=5000, description="Monte Carlo repetitions. Spec 10 caps it at 5000."
    )
    illness_mean_interval_days: float = Field(
        default=120.0,
        gt=0,
        description="Starting value, not derived. Mean of the exponential gap between the end "
        "of one illness and the start of the next. `inf` switches illness off.",
    )
    illness_days_range: tuple[int, int] = Field(
        default=(3, 10),
        description="Starting value, not derived. Uniform length of an illness, in days.",
    )
    travel_prob_per_week: float = Field(
        default=0.03,
        ge=0,
        le=1,
        description="Starting value, not derived. Chance that a given week holds a trip.",
    )
    travel_days_range: tuple[int, int] = Field(
        default=(3, 5),
        description="Starting value, not derived. Uniform length of a trip, in days.",
    )
    tau_fitness: float = Field(
        default=42.0, gt=0, description="Starting value (Banister). Fitness decay constant, days."
    )
    tau_fatigue: float = Field(
        default=7.0, gt=0, description="Starting value (Banister). Fatigue decay constant, days."
    )
    k_fitness: float = Field(
        default=1.0, ge=0, description="Starting value (Banister). Weight of fitness."
    )
    k_fatigue: float = Field(
        default=2.0, ge=0, description="Starting value (Banister). Weight of fatigue."
    )
    fail_softness: float = Field(
        default=0.03,
        gt=0,
        description="Starting value, not derived. Width of the sigmoid that turns a readiness "
        "shortfall into a chance of missing the test.",
    )
    readiness_scale: float = Field(
        default=0.25,
        ge=0,
        description="Starting value, not derived. Readiness points lost per 100% shortfall of "
        "performance against a clean run of the plan. At 1.0 a two-week layoff (about 28% "
        "of fitness at tau 42) made the test all but impossible; 0.25 was picked by eye so a "
        "clean block makes its tests ~85% of the time and a 10-day illness in week 6 ~50%.",
    )
    test_demand: float = Field(
        default=0.95,
        gt=0,
        le=1.5,
        description="Starting value, not derived. How much of full readiness a test single "
        "asks for. Below 1.0 so that a clean block usually makes its tests.",
    )
    fallback_gain_range: tuple[float, float] = Field(
        default=(0.02, 0.05),
        description="Starting value, not derived. Uniform relative 1RM gain used when no "
        "calibration file exists.",
    )

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> SimConfig:
        for name in ("illness_days_range", "travel_days_range"):
            low, high = getattr(self, name)
            if not 1 <= low <= high:
                raise ValueError(f"{name} must satisfy 1 <= low <= high, got {(low, high)}")
        low_gain, high_gain = self.fallback_gain_range
        if not 0 <= low_gain <= high_gain:
            raise ValueError("fallback_gain_range must satisfy 0 <= low <= high")
        return self


DEFAULT_SIM_CONFIG = SimConfig()


class ScenarioError(ValueError):
    """A forced event that does not fit the block it was asked about.

    A subclass so the service can turn exactly this into a 422 without also
    catching a `ValueError` raised from deeper inside the policy, which would
    be a bug and should surface as one.
    """


# --- What the caller injects, and what comes back ---------------------------


class DatePercentiles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    p10: dt.date
    p50: dt.date
    p90: dt.date


class KgPercentiles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    p10: Decimal
    p50: Decimal
    p90: Decimal


class CountPercentiles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    p10: int
    p50: int
    p90: int


class LiftOutlook(BaseModel):
    """One lift's range of outcomes.

    `test` says why `p_test_made` is what it is: `pending` -- the test single is
    still ahead and was simulated; `behind` -- it is already done or written off,
    so there is nothing left to sample and `p_test_made` is None; `none` -- the
    template never tests this lift (row), so it is 0.0 and the max cannot move.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    test: TestStatus
    p_test_made: float | None
    # The block's planning max for this lift, from its own snapshot -- not
    # necessarily the athlete's newest recorded max, and for a restarted block
    # the reduced one.
    current_1rm_kg: Decimal
    new_1rm: KgPercentiles


class SimStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int
    seed: int
    # The one field two identical simulations may disagree on.
    wall_time_s: float
    runs_finished: int
    runs_restart: int
    runs_unplannable: int


class SimulationResult(BaseModel):
    """Ranges and the assumptions behind them. Never a single predicted number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_end_date: dt.date
    # Over the runs that reached the end of the block. None when none did.
    finish_date: DatePercentiles | None
    per_lift: dict[str, LiftOutlook]
    missed_sessions: CountPercentiles
    # A descriptive mean sitting beside `missed_sessions`, kept for spec 10 §5E.
    missed_sessions_mean: float
    # Share of runs the policy ended with a restart (two empty weeks), and share
    # it could not re-plan at all. Both are outcomes, not failures of the tool.
    p_restart: float
    p_unplannable: float
    assumptions: list[str]
    stats: SimStats


class GainQuantiles(BaseModel):
    """One lift's calibrated relative 1RM gain between consecutive meets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    n: int = Field(ge=0)
    # True when the numbers are another lift's scaled, not measured (press, row).
    derived: bool = False

    def knots(self) -> tuple[float, ...]:
        return (self.p10, self.p25, self.p50, self.p75, self.p90)


class Gains(BaseModel):
    """`calibration/gains.json`: where the gain distributions came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    generated_on: dt.date
    lifts: dict[str, GainQuantiles]


def load_gains(path: Path = GAINS_PATH) -> Gains | None:
    """The calibration file, or None if there is not one.

    Absence is a supported state, not an error (spec 10 §13): the simulator
    falls back to a flat range and says so. A file that exists but is malformed
    *is* an error -- silently ignoring it would present a fallback as if it were
    the athlete's calibration.
    """
    if not path.exists():
        return None
    return Gains.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --- Preparation: everything that does not change between runs --------------


@dataclass(frozen=True)
class _Prepared:
    state: BlockState
    ctx: ValidationContext
    origin: dt.date
    planned_end: dt.date
    # (days from origin, load) of sessions already done. Their fitness and
    # fatigue still count, decayed to the origin.
    history: tuple[tuple[int, float], ...]
    # Performance a clean run reaches just before each pending seq, and the
    # scale readiness is measured against.
    perf_clean: dict[int, float]
    perf_norm: float
    test_status: dict[str, TestStatus]
    # Session load by lifts tuple. Content-addressed, so sharing it across runs
    # cannot make one run depend on another.
    load_cache: dict[tuple[DraftLift, ...], float] = field(default_factory=dict)


_PENDING = ("planned", "moved")


def _load_of(lifts: tuple[DraftLift, ...], cache: dict[tuple[DraftLift, ...], float]) -> float:
    """`sum(sets x reps x pct)`, exactly, then to float once (spec 10 §5B)."""
    hit = cache.get(lifts)
    if hit is None:
        total = sum((Fraction(lift.sets * lift.reps) * lift.pct for lift in lifts), Fraction(0))
        hit = cache[lifts] = float(total)
    return hit


def _decay(value: float, days: float, tau: float) -> float:
    return value * math.exp(-max(days, 0.0) / tau)


def _initial_fitness_fatigue(
    history: Sequence[tuple[int, float]], cfg: SimConfig
) -> tuple[float, float]:
    fitness = fatigue = 0.0
    for day, load in history:
        elapsed = -day  # negative day = in the past; a "future" done session counts as just done
        fitness += _decay(load, elapsed, cfg.tau_fitness)
        fatigue += _decay(load, elapsed, cfg.tau_fatigue)
    return fitness, fatigue


def _prepare(state: BlockState, ctx: ValidationContext, cfg: SimConfig) -> _Prepared:
    origin = ctx.today
    cache: dict[tuple[DraftLift, ...], float] = {}

    performed_on = {log.seq: log.performed_on for log in state.logs}
    history = tuple(
        (
            (performed_on.get(session.seq, session.date) - origin).days,
            _load_of(session.lifts, cache),
        )
        for session in state.plan
        if state.status_of[session.seq] == "done"
    )

    # A clean run: every pending session performed on its planned date.
    fitness, fatigue = _initial_fitness_fatigue(history, cfg)
    clock = 0
    perf_clean: dict[int, float] = {}
    for session in state.plan:
        if state.status_of[session.seq] not in _PENDING:
            continue
        day = max((session.date - origin).days, 0)
        fitness = _decay(fitness, day - clock, cfg.tau_fitness)
        fatigue = _decay(fatigue, day - clock, cfg.tau_fatigue)
        clock = day
        perf_clean[session.seq] = cfg.k_fitness * fitness - cfg.k_fatigue * fatigue
        load = _load_of(session.lifts, cache)
        fitness += load
        fatigue += load
    perf_norm = max((abs(value) for value in perf_clean.values()), default=0.0) or 1.0

    test_status: dict[str, TestStatus] = {}
    for lift in state.lifts:
        test_seqs = [
            session.seq
            for session in state.plan
            if any(entry.lift_code == lift and entry.is_test for entry in session.lifts)
        ]
        if not test_seqs:
            test_status[lift] = "none"
        elif all(state.status_of[seq] not in _PENDING for seq in test_seqs):
            test_status[lift] = "behind"
        else:
            test_status[lift] = "pending"

    return _Prepared(
        state=state,
        ctx=ctx,
        origin=origin,
        planned_end=max(session.date for session in state.plan),
        history=history,
        perf_clean=perf_clean,
        perf_norm=perf_norm,
        test_status=test_status,
        load_cache=cache,
    )


def _windows_of(
    scenario: Sequence[ForcedEvent], state: BlockState, origin: dt.date
) -> list[tuple[int, int]]:
    """Forced events as inclusive (first day, last day) offsets from the origin."""
    windows: list[tuple[int, int]] = []
    # The block's own length, not the template's: a block need not run as long as
    # the template it came from (ADR-0011).
    weeks = max(session.week for session in state.plan)
    for event in scenario:
        if event.start_week is not None:
            if event.start_week > weeks:
                raise ScenarioError(
                    f"start_week {event.start_week} is past the block's {weeks} weeks"
                )
            first = state.start_date + dt.timedelta(days=7 * (event.start_week - 1))
        else:
            assert event.start_date is not None
            first = event.start_date
        start = (first - origin).days
        end = start + event.days - 1
        if end < 0:
            raise ScenarioError(
                f"a {event.kind} of {event.days} days from {first} is already over "
                f"as of {origin}, so it cannot change anything"
            )
        windows.append((start, end))
    return windows


# --- One run -----------------------------------------------------------------


@dataclass
class _Outcome:
    status: RunStatus
    finish: dt.date | None = None
    missed: int = 0
    made: dict[str, bool] = field(default_factory=dict)
    new_1rm: dict[str, Decimal] = field(default_factory=dict)
    reason: str | None = None


def _sigmoid(x: float) -> float:
    x = max(-50.0, min(50.0, x))
    return 1.0 / (1.0 + math.exp(-x))


class _Run:
    """One simulated future of the block. Built, run once, thrown away."""

    def __init__(
        self,
        prep: _Prepared,
        forced: Sequence[tuple[int, int]],
        cfg: SimConfig,
        policy: MissedPolicyConfig,
        gains: Gains | None,
        rng: np.random.Generator,
    ) -> None:
        self.prep = prep
        self.cfg = cfg
        self.policy = policy
        self.gains = gains
        self.rng = rng
        self.env = simpy.Environment()
        self.state = prep.state
        self.status = dict(prep.state.status_of)
        self.windows: list[tuple[int, int]] = list(forced)
        self.fitness, self.fatigue = _initial_fitness_fatigue(prep.history, cfg)
        self.clock = 0
        self.missed_seqs: set[int] = set()
        self.performed_last: dt.date | None = None
        self.replans = 0
        self.result: _Outcome | None = None
        self.made: dict[str, bool] = {}
        self.new_1rm: dict[str, Decimal] = {}

    # -- processes --

    def run(self) -> _Outcome:
        self.env.process(self._illness())
        self.env.process(self._travel())
        athlete = self.env.process(self._athlete())
        self.env.run(until=athlete)
        assert self.result is not None
        return self.result

    def _illness(self):
        """Open an illness window, wait for it to start, then draw the next.

        Each window is appended *before* the clock reaches its first day --
        drawn as soon as the previous one starts -- so the athlete, who looks
        windows up on a session's date, never races the process that creates
        them.
        """
        mean = self.cfg.illness_mean_interval_days
        if not math.isfinite(mean):
            return
        low, high = self.cfg.illness_days_range
        previous_end = 0
        while True:
            gap = max(1, math.ceil(self.rng.exponential(mean)))
            start = previous_end + gap
            end = start + int(self.rng.integers(low, high + 1)) - 1
            self.windows.append((start, end))
            yield self.env.timeout(max(0, start - self.env.now))
            previous_end = end

    def _travel(self):
        """A weekly coin flip, decided one week ahead for the same reason."""
        probability = self.cfg.travel_prob_per_week
        if probability <= 0:
            return
        self._maybe_trip(0)
        week = 0
        while True:
            self._maybe_trip(week + 1)
            yield self.env.timeout(7)
            week += 1

    def _maybe_trip(self, week: int) -> None:
        if self.rng.random() < self.cfg.travel_prob_per_week:
            low, high = self.cfg.travel_days_range
            start = 7 * week + int(self.rng.integers(0, 7))
            self.windows.append((start, start + int(self.rng.integers(low, high + 1)) - 1))

    def _athlete(self):
        # Planned, dated in the past and not done: the athlete means to make them
        # up, and the policy decides how. One call may not clear them all -- a
        # fold, for instance, handles only the earliest -- so ask again until
        # none is left, as the real planner would after each approval.
        while overdue := self._overdue():
            first = min(session.date for session in overdue)
            self._replan(
                [session.seq for session in overdue],
                (first - self.prep.origin).days,
                -1,
            )
            if self.result is not None:
                return

        while True:
            pending = [s for s in self.state.plan if self.status[s.seq] in _PENDING]
            if not pending:
                self.result = self._finish()
                return
            session = pending[0]
            day = max((session.date - self.prep.origin).days, int(self.env.now))
            if day > self.env.now:
                yield self.env.timeout(day - self.env.now)

            covering = self._covering(day)
            if covering is None:
                self._perform(session, day)
                continue

            start, end = covering
            for _ in range(_MAX_RECHECKS):
                yield self.env.timeout(max(0, end + 1 - self.env.now))
                # Windows drawn while the athlete was away may extend the absence.
                again = self._covering(end + 1)
                if again is None:
                    break
                end = max(end, again[1])
            else:
                self.result = _Outcome("unplannable", reason="the absence did not end")
                return
            # Everything pending dated up to the window's end, not only what falls
            # inside it: a session left stale by an earlier re-plan is just as
            # missed, and an event naming no session is not a valid event.
            missed = [
                s.seq
                for s in self.state.plan
                if self.status[s.seq] in _PENDING and (s.date - self.prep.origin).days <= end
            ]
            self._replan(missed, min(start, self._earliest(missed)), end)
            if self.result is not None:
                return

    def _overdue(self) -> list[DraftSession]:
        return [
            session
            for session in self.state.plan
            if self.status[session.seq] in _PENDING and session.date < self.prep.origin
        ]

    def _earliest(self, seqs: list[int]) -> int:
        """Days from the origin of the earliest of these sessions."""
        by_seq = self.state.by_seq
        return min((by_seq[seq].date - self.prep.origin).days for seq in seqs)

    # -- the athlete's day --

    def _covering(self, day: int) -> tuple[int, int] | None:
        """The unavailable stretch containing `day`, extended to wherever it ends.

        Adjacent windows chain: an illness that ends on Tuesday and a trip that
        starts on Wednesday are one absence. Extended forward only -- anything
        before `day` was either handled on its own session's date or fell
        between sessions.
        """
        hit = [window for window in self.windows if window[0] <= day <= window[1]]
        if not hit:
            return None
        start = min(window[0] for window in hit)
        end = max(window[1] for window in hit)
        extended = True
        while extended:
            extended = False
            for first, last in self.windows:
                if first <= end + 1 and last > end:
                    end = last
                    extended = True
        return start, end

    def _perform(self, session: DraftSession, day: int) -> None:
        cfg = self.cfg
        elapsed = day - self.clock
        self.fitness = _decay(self.fitness, elapsed, cfg.tau_fitness)
        self.fatigue = _decay(self.fatigue, elapsed, cfg.tau_fatigue)
        self.clock = day
        performance = cfg.k_fitness * self.fitness - cfg.k_fatigue * self.fatigue
        reference = self.prep.perf_clean.get(session.seq, performance)
        readiness = 1.0 + cfg.readiness_scale * (performance - reference) / self.prep.perf_norm

        for lift in session.lifts:
            if lift.is_test:
                self._attempt(lift.lift_code, readiness)

        load = _load_of(session.lifts, self.prep.load_cache)
        self.fitness += load
        self.fatigue += load
        self.status[session.seq] = "done"
        self.performed_last = self.prep.origin + dt.timedelta(days=day)

    def _attempt(self, lift: str, readiness: float) -> None:
        """The test single: made or missed, and what the max becomes."""
        shortfall = (self.cfg.test_demand - readiness) / self.cfg.fail_softness
        made = bool(self.rng.random() >= _sigmoid(shortfall))
        self.made[lift] = made
        if not made:
            return
        # A made test is a lift above the old max, so a draw below zero is
        # clamped: the max never falls on a successful test (ADR-0005 rule 6).
        gain = max(0.0, self._gain(lift)) * min(max(readiness, 0.0), 1.0)
        old = self.prep.state.one_rm_snapshot[lift]
        self.new_1rm[lift] = rep_max.quantize_kg(Fraction(old) * (1 + Fraction(gain)))

    def _gain(self, lift: str) -> float:
        draw = float(self.rng.random())
        entry = self.gains.lifts.get(lift) if self.gains is not None else None
        if entry is None:
            low, high = self.cfg.fallback_gain_range
            return low + draw * (high - low)
        return float(np.interp(draw, _QUANTILE_POINTS, entry.knots()))

    # -- re-planning --

    def _replan(self, missed: list[int], first_day: int, last_day: int) -> None:
        if not missed:
            raise ValueError("a re-plan needs at least one missed session")
        self.replans += 1
        self.missed_seqs.update(missed)
        if self.replans > _MAX_REPLANS_PER_RUN:
            self.result = _Outcome("unplannable", reason="the policy did not settle")
            return
        origin = self.prep.origin
        event = MissedEvent(
            block_id=self.state.block_id,
            missed_seqs=tuple(sorted(missed)),
            unavailable_from=origin + dt.timedelta(days=first_day),
            unavailable_until=origin + dt.timedelta(days=last_day),
            today=origin + dt.timedelta(days=last_day + 1),
        )
        state = self.state.model_copy(update={"status_of": dict(self.status)})
        # A session an earlier re-plan parked off its weekday has been approved
        # there (approval marks it `moved`), so the weekday rule must not
        # re-litigate it -- the same waiver `services.validation_context` gives
        # a real block through `already_moved`.
        moved = {seq for seq, status in self.status.items() if status == "moved"}
        ctx = self.prep.ctx.model_copy(
            update={
                "today": event.today,
                "approved_off_weekday": set(self.prep.ctx.approved_off_weekday) | moved,
            }
        )
        outcome = missed_policy.propose(event, state, ctx, self.policy, fast=True)

        if outcome.tier == "restart":
            # A restart builds a new block from a database row, which a pure
            # simulation does not have. It is where this run ends.
            self.result = _Outcome("restart", reason="two empty weeks")
            return
        if outcome.failure is not None:
            self.result = _Outcome("unplannable", reason=outcome.failure.reason)
            return
        self._apply(outcome.proposals[0].payload.plan)

    def _apply(self, proposed: tuple[DraftSession, ...]) -> None:
        """Take a proposal the way `services._apply_plan` would, in memory.

        A re-dated session becomes `moved`; a session the proposal no longer
        mentions was folded away and is `skipped`, but it stays in the plan with
        its old date, exactly as the database keeps its row. Dropping it instead
        would make later re-plans see a calendar the real planner never has.
        """
        old = self.state.by_seq
        new = {session.seq: session for session in proposed}
        for seq, before in old.items():
            if self.status[seq] in ("done", "missed", "skipped"):
                continue
            if seq not in new:
                self.status[seq] = "skipped"
            elif new[seq].date != before.date:
                self.status[seq] = "moved"
        merged = tuple(sorted({**old, **new}.values(), key=lambda session: session.seq))
        self.state = self.state.model_copy(update={"plan": merged, "status_of": dict(self.status)})

    def _finish(self) -> _Outcome:
        return _Outcome("finished", finish=self.performed_last or self.prep.planned_end)

    def outcome_of(self, base: _Outcome) -> _Outcome:
        base.missed = len(self.missed_seqs)
        base.made = self.made
        base.new_1rm = self.new_1rm
        return base


def _simulate_once(
    prep: _Prepared,
    forced: Sequence[tuple[int, int]],
    cfg: SimConfig,
    policy: MissedPolicyConfig,
    gains: Gains | None,
    rng: np.random.Generator,
) -> _Outcome:
    run = _Run(prep, forced, cfg, policy, gains, rng)
    return run.outcome_of(run.run())


# --- Aggregation -------------------------------------------------------------


def _rank(ordered: Sequence, percent: int):
    """Nearest-rank percentile of an already-sorted sequence.

    Integer arithmetic: `ceil(0.1 * 1000)` is 101 in floating point, which would
    quietly shift every percentile by one run. Returns an observed value, so a
    reported date is one a run actually produced.
    """
    index = max(0, -(-percent * len(ordered) // 100) - 1)
    return ordered[index]


def _triple(ordered: Sequence) -> tuple:
    return _rank(ordered, 10), _rank(ordered, 50), _rank(ordered, 90)


def _assumptions(
    cfg: SimConfig,
    policy: MissedPolicyConfig,
    gains: Gains | None,
    scenario: Sequence[ForcedEvent],
    prep: _Prepared,
    outcomes: Sequence[_Outcome],
) -> list[str]:
    lines = [
        "This is a decision aid, not a prediction of your body. Every constant below is a "
        "starting value that has not been fitted to you.",
        f"{cfg.runs} simulated futures of the rest of the block, starting {prep.origin}.",
        "Illness: gaps between illnesses average "
        + (
            f"{cfg.illness_mean_interval_days:g} days (exponential)"
            if math.isfinite(cfg.illness_mean_interval_days)
            else "infinity, so illness is switched off"
        )
        + f"; each lasts {cfg.illness_days_range[0]}-{cfg.illness_days_range[1]} days.",
        f"Travel: {cfg.travel_prob_per_week:g} chance per week of a "
        f"{cfg.travel_days_range[0]}-{cfg.travel_days_range[1]} day trip.",
        f"Fitness-fatigue (Banister): time constants {cfg.tau_fitness:g} / {cfg.tau_fatigue:g} "
        f"days, gains {cfg.k_fitness:g} / {cfg.k_fatigue:g}. Readiness is measured against a "
        "clean run of your plan, so it is 1.0 where the plan expected you to be.",
        f"A test single asks for {cfg.test_demand:g} of full readiness; the chance of missing "
        f"it is a sigmoid of the shortfall, width {cfg.fail_softness:g}, and each 100% "
        f"shortfall in performance costs {cfg.readiness_scale:g} of readiness. Only test "
        "singles are sampled; a missed ordinary set has no consequence in this model.",
        f"After each absence the missed-session policy re-plans (fast mode, first option), "
        f"with {policy.restart_after_empty_weeks} empty weeks ending a run as a restart.",
        "Sessions already done count towards fitness at their planned load; sessions you "
        "have not done and that are already dated in the past are treated as missed on day 1.",
        "A made test raises the 1RM by a sampled gain scaled by readiness (capped at 1.0, "
        "never below zero); a missed test, a restart or an unplannable run leaves it as it is.",
    ]
    if gains is None:
        low, high = cfg.fallback_gain_range
        lines.append(
            f"No calibration file was found, so gains are drawn flat between {low:.0%} and "
            f"{high:.0%}. That range is a placeholder, not data."
        )
    else:
        lines.append(
            f"Gains follow the calibrated distribution from {gains.source} "
            f"(generated {gains.generated_on}), interpolated between its p10 and p90."
        )
        derived = sorted(name for name, entry in gains.lifts.items() if entry.derived)
        if derived:
            lines.append(
                f"{', '.join(derived)} have no meet data; their gains are a scaled copy of "
                "bench's, not measurements."
            )
    for name, status in prep.test_status.items():
        if status == "none":
            lines.append(f"{name} is never tested in this template, so its max cannot move.")
        elif status == "behind":
            lines.append(
                f"{name}'s test single is no longer ahead of you (done or written off) and is "
                "not re-sampled; a test already taken is in your recorded maxes, which this "
                "simulation does not re-read, so its range is the block's planning max."
            )
    for event in scenario:
        when = f"week {event.start_week}" if event.start_week else str(event.start_date)
        lines.append(f"Forced in every run: {event.kind}, {event.days} days from {when}.")
    restarts = sum(o.status == "restart" for o in outcomes)
    unplannable = [o for o in outcomes if o.status == "unplannable"]
    if restarts:
        lines.append(
            f"{restarts} of {len(outcomes)} runs ended in a restart. They have no finish date, "
            "so the finish-date range covers the remaining runs; the 1RM ranges and test "
            "chances cover all runs, counting a restart as a test not made."
        )
    if unplannable:
        reasons = sorted({o.reason or "unknown" for o in unplannable})
        lines.append(
            f"{len(unplannable)} runs could not be re-planned ({'; '.join(reasons)}). They are "
            "excluded from the finish-date range and counted as tests not made."
        )
    return lines


def simulate(
    state: BlockState,
    ctx: ValidationContext,
    *,
    scenario: Sequence[ForcedEvent] = (),
    cfg: SimConfig = DEFAULT_SIM_CONFIG,
    seed: int = 0,
    gains: Gains | None = None,
    policy: MissedPolicyConfig = DEFAULT_MISSED_POLICY,
) -> SimulationResult:
    """Play out the rest of `state` `cfg.runs` times and summarise the spread.

    `ctx.today` is where the simulation starts: it must be built once by the
    caller (ADR-0006), and each re-plan re-copies it with its own day. The same
    `seed` gives the same result in every field but `stats.wall_time_s`.

    Raises `ValueError` for a block that is not running or a forced event that
    falls outside the block; the service layer turns those into the API's
    errors.
    """
    started = time.perf_counter()
    if seed < 0:
        raise ValueError(f"seed must not be negative, got {seed}")
    if state.status not in ("active", "paused"):
        raise ValueError(f"block {state.block_id} is {state.status}, not running")

    prep = _prepare(state, ctx, cfg)
    forced = _windows_of(scenario, state, prep.origin)
    children = np.random.SeedSequence(seed).spawn(cfg.runs)
    outcomes = [
        _simulate_once(prep, forced, cfg, policy, gains, np.random.default_rng(child))
        for child in children
    ]

    finished = sorted(o.finish for o in outcomes if o.status == "finished" and o.finish)
    finish_date = (
        DatePercentiles(**dict(zip(("p10", "p50", "p90"), _triple(finished), strict=True)))
        if finished
        else None
    )

    per_lift: dict[str, LiftOutlook] = {}
    for lift in state.lifts:
        status = prep.test_status[lift]
        current = state.one_rm_snapshot[lift]
        landed = sorted(o.new_1rm.get(lift, current) for o in outcomes)
        made = sum(o.made.get(lift, False) for o in outcomes)
        per_lift[lift] = LiftOutlook(
            test=status,
            p_test_made={"pending": made / len(outcomes), "none": 0.0, "behind": None}[status],
            current_1rm_kg=current,
            new_1rm=KgPercentiles(**dict(zip(("p10", "p50", "p90"), _triple(landed), strict=True))),
        )

    missed = sorted(o.missed for o in outcomes)
    return SimulationResult(
        planned_end_date=prep.planned_end,
        finish_date=finish_date,
        per_lift=per_lift,
        missed_sessions=CountPercentiles(
            **dict(zip(("p10", "p50", "p90"), _triple(missed), strict=True))
        ),
        missed_sessions_mean=sum(missed) / len(missed),
        p_restart=sum(o.status == "restart" for o in outcomes) / len(outcomes),
        p_unplannable=sum(o.status == "unplannable" for o in outcomes) / len(outcomes),
        assumptions=_assumptions(cfg, policy, gains, scenario, prep, outcomes),
        stats=SimStats(
            runs=cfg.runs,
            seed=seed,
            wall_time_s=round(time.perf_counter() - started, 3),
            runs_finished=len(finished),
            runs_restart=sum(o.status == "restart" for o in outcomes),
            runs_unplannable=sum(o.status == "unplannable" for o in outcomes),
        ),
    )


__all__ = [
    "DEFAULT_SIM_CONFIG",
    "GAINS_PATH",
    "CountPercentiles",
    "DatePercentiles",
    "ForcedEvent",
    "GainQuantiles",
    "Gains",
    "KgPercentiles",
    "LiftOutlook",
    "ScenarioError",
    "SimConfig",
    "SimStats",
    "SimulationResult",
    "load_gains",
    "simulate",
]
