"""What to do when training was missed. Spec 08, as amended.

Four structural outcomes, decided by what the calendar still allows rather
than by counting days:

* `in_week_move` -- the session fits somewhere else this week. Move it there
  and change nothing else.
* `week_shift` -- it does not fit. The session takes the same weekday next
  week carrying its own sets, reps and kilograms, and everything after it
  moves back seven days. The block ends a week later.
* `fold` -- a later session was already trained, so the missed one cannot be
  placed anywhere without breaking `seq` order. Its lifts join the next
  session that trains them, and that session keeps its own numbers.
* `restart` -- two consecutive weeks with nothing completed, or an injury.
  The block is replaced rather than repaired.

**Why structural and not a day count.** Spec 08 originally classified by how
long the athlete was away (2 days, 10 days, 21 days). The thresholds were
guesses, and they answered the wrong question: what matters is not how long
the gap was but whether the week can still absorb it. An athlete away three
days over a weekend has lost nothing; one away three days across their only
two training slots has lost a week. `unavailable_days` already removes the
days the athlete could not train, so a long absence yields no feasible day on
its own and falls through to `week_shift` without a threshold being consulted.

**What this module may not do.** It never writes. `propose` returns proposals
and the caller decides; approval is still the one write path (CLAUDE.md
non-negotiable #2). It has no clock -- `today` arrives on the `MissedEvent` --
no database and no randomness, so spec 10's simulator can run it a thousand
times and spec 15 can call it from an activity.

Restart is the one tier that cannot finish here: building a block needs the
athlete, the template and its 77 entries. The arithmetic that decides the
new maxes stays pure in `restart_one_rms`, and `services.propose_for_missed`
supplies the draft.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domains.strength import calendar_solver, enums, rep_max
from domains.strength.calendar_solver import ScheduleOption, SolverStats
from domains.strength.schemas import (
    BlockDraft,
    BlockState,
    DraftSession,
    SessionChange,
    StrengthPayload,
)
from domains.strength.validator import (
    DEFAULT_CONFIG,
    ValidationContext,
    ValidatorConfig,
    rest_days_needed,
    validate,
)
from planner_core.proposal import Proposal, ProposalSource, ValidationResult, Violation

logger = logging.getLogger(__name__)

DAYS_PER_WEEK = 7

Tier = Literal["in_week_move", "week_shift", "fold", "restart"]

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class MissedPolicyConfig(BaseModel):
    """Thresholds and solver knobs. Every number here is a starting value.

    None of these is derived from anything. They are the author's first guess
    at reasonable behaviour, recorded in one place so they can be tuned
    without hunting through the code, and labelled so the UI can say plainly
    that they are guesses (CLAUDE.md guardrail).

    Seven is not here. A week is structural, not a tunable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    restart_after_empty_weeks: int = Field(
        default=2,
        ge=1,
        description="Starting value, not derived. Consecutive elapsed training "
        "weeks with nothing completed before the block is restarted rather "
        "than repaired.",
    )
    restart_one_rm_factor: Decimal = Field(
        default=Decimal("0.95"),
        gt=0,
        le=1,
        description="Starting value, not derived. What a restart assumes was "
        "lost over the weeks away. Applies to the planning maxes of the new "
        "block only; it never changes a recorded 1RM.",
    )
    restart_estimate_window_weeks: int = Field(
        default=4,
        ge=1,
        description="Starting value, not derived. How far back, in template "
        "weeks, a restart looks for a logged set to estimate from.",
    )
    solver_time_limit_s: float = Field(default=5.0, gt=0)
    solver_n_options: int = Field(default=3, ge=1, le=3)
    solver_seed: int = 0
    solver_num_workers: int = Field(default=8, ge=1)


DEFAULT_MISSED_POLICY = MissedPolicyConfig()


class MissedEvent(BaseModel):
    """Training that did not happen, and the window it did not happen in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: uuid.UUID
    missed_seqs: tuple[int, ...] = Field(min_length=1)
    unavailable_from: dt.date
    # None means "still away as of today", which is why `today` is required:
    # the window has to be closed by something, and it may not be a clock
    # reading taken inside this module.
    unavailable_until: dt.date | None = None
    injury: bool = False
    today: dt.date

    @model_validator(mode="after")
    def _window_is_ordered(self) -> MissedEvent:
        if self.unavailable_until is not None and self.unavailable_until < self.unavailable_from:
            raise ValueError("unavailable_until is before unavailable_from")
        if self.today < self.unavailable_from:
            raise ValueError("today is before the absence began")
        if len(set(self.missed_seqs)) != len(self.missed_seqs):
            raise ValueError("missed_seqs repeats a seq")
        return self

    @property
    def days_out(self) -> int:
        """Kept for reporting. Classification no longer consults it."""
        return ((self.unavailable_until or self.today) - self.unavailable_from).days

    @property
    def unavailable_days(self) -> frozenset[dt.date]:
        """Every day the athlete could not train, inclusive at both ends.

        An open window closes the day before `today`: the athlete is back, so
        today is available. A closed one ends where it says.
        """
        last = self.unavailable_until or self.today - dt.timedelta(days=1)
        span = (last - self.unavailable_from).days
        return frozenset(
            self.unavailable_from + dt.timedelta(days=offset) for offset in range(span + 1)
        )


class PolicyFailed(BaseModel):
    """Nothing valid survived. An answer the agent can show, not a crash.

    Mirrors `calendar_solver.Infeasible`, and for the same reason: "there is
    no good option here" is a real outcome of asking, and turning it into an
    exception would make the caller's happy path the only path with a shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    # One entry per option the validator threw out, naming the rules it broke.
    # Without these a failure reads as "no", with no way to see why.
    dropped: tuple[str, ...] = ()
    stats: SolverStats | None = None


class PolicyResult(BaseModel):
    """What the policy decided, and either the options or why there are none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: Tier
    proposals: tuple[Proposal[StrengthPayload], ...] = ()
    failure: PolicyFailed | None = None

    @model_validator(mode="after")
    def _one_or_the_other(self) -> PolicyResult:
        if bool(self.proposals) == (self.failure is not None):
            raise ValueError("a result carries proposals or a failure, never both or neither")
        return self

    @property
    def ok(self) -> bool:
        return bool(self.proposals)


# --- Reading the block ----------------------------------------------------


def _elapsed_weeks(state: BlockState, today: dt.date) -> list[int]:
    """Template weeks whose every session is now in the past, in order.

    A week that is only half over says nothing about whether it was trained,
    so it is not counted either way.
    """
    by_week: dict[int, list[DraftSession]] = {}
    for block_session in state.plan:
        by_week.setdefault(block_session.week, []).append(block_session)
    return sorted(
        week
        for week, sessions in by_week.items()
        if all(block_session.date < today for block_session in sessions)
    )


def _empty_week_run(state: BlockState, today: dt.date) -> int:
    """The longest run of consecutive elapsed weeks with nothing completed.

    "Completed" means a session whose status is `done`. A week in which every
    session was skipped or written off counts as empty, because no training
    happened in it -- which is the fact the restart rule turns on.
    """
    done_weeks = {
        block_session.week
        for block_session in state.plan
        if state.status_of[block_session.seq] == "done"
    }
    longest = run = 0
    previous: int | None = None
    for week in _elapsed_weeks(state, today):
        if week in done_weeks:
            run = 0
        else:
            # Only consecutive template weeks extend a run. A gap means some
            # week in between is still live, and the athlete may yet train it.
            run = run + 1 if previous is not None and week == previous + 1 else 1
            longest = max(longest, run)
        previous = week
    return longest


def _trained_after(state: BlockState, seq: int) -> bool:
    """Did the athlete train a later session than this one?"""
    return any(other > seq and state.status_of[other] == "done" for other in state.status_of)


def _feasible_days(
    state: BlockState,
    event: MissedEvent,
    seq: int,
    *,
    horizon_end: dt.date,
    config: ValidatorConfig,
) -> tuple[dt.date, ...]:
    """Every day this one session could be placed on without breaking a rule.

    The same feasible set CP-SAT searches when every other session is pinned,
    computed in plain Python. Used three ways: to decide whether an in-week
    move is possible at all, to place one in `fast` mode, and as the
    cross-check that the solver and the greedy path agree.

    Bounded by the sessions either side of it, because `seq` order is a hard
    rule: a session cannot be trained before the one numbered below it or
    after the one numbered above it.
    """
    others = [block_session for block_session in state.plan if block_session.seq != seq]
    before = [other.date for other in others if other.seq < seq]
    after = [other.date for other in others if other.seq > seq]

    low = event.today
    if before:
        low = max(low, max(before) + dt.timedelta(days=1))
    high = horizon_end
    if after:
        high = min(high, min(after) - dt.timedelta(days=1))

    taken = {other.date for other in others}
    lifts = [lift.lift_code for lift in state.by_seq[seq].lifts]
    # The widest gap this session owes each neighbour, precomputed so the day
    # loop below stays a comparison rather than a nested search.
    needed = [
        (
            other.date,
            max(
                rest_days_needed(mine, theirs, {}, config)
                for mine in lifts
                for theirs in (lift.lift_code for lift in other.lifts)
            ),
        )
        for other in others
    ]

    days: list[dt.date] = []
    day = low
    while day <= high:
        if (
            day not in event.unavailable_days
            and day not in taken
            and all(abs((day - when).days) >= gap for when, gap in needed)
        ):
            days.append(day)
        day += dt.timedelta(days=1)
    return tuple(days)


# --- Classification -------------------------------------------------------


def classify(
    event: MissedEvent,
    state: BlockState,
    cfg: MissedPolicyConfig = DEFAULT_MISSED_POLICY,
    *,
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> Tier:
    """Which of the four outcomes this absence calls for.

    Order matters and is not arbitrary:

    1. An injury is a medical question, not a scheduling one.
    2. Two empty weeks mean the block's progression is gone; moving sessions
       around cannot recover it.
    3. A session already trained *after* the missed one pins the order, so
       there is nowhere left to put it. Checked before the in-week move,
       because otherwise that search would come back empty and the absence
       would be misread as "no room this week".
    4. Room this week -> move it.
    5. Otherwise the week is lost and the block stretches.
    """
    if state.status not in ("active", "paused"):
        raise ValueError(f"block {state.block_id} is {state.status}, not running")
    unknown = sorted(set(event.missed_seqs) - set(state.status_of))
    if unknown:
        raise ValueError(f"missed_seqs names sessions not in the block: {unknown}")
    trained = sorted(seq for seq in event.missed_seqs if state.status_of[seq] == "done")
    if trained:
        raise ValueError(f"cannot miss a session that is already done: {trained}")

    if event.injury:
        return "restart"
    if _empty_week_run(state, event.today) >= cfg.restart_after_empty_weeks:
        return "restart"

    first = min(event.missed_seqs)
    if _trained_after(state, first):
        return "fold"

    if len(event.missed_seqs) == 1:
        week = state.by_seq[first].week
        horizon = state.week_end(week)
        if event.today <= horizon and _feasible_days(
            state, event, first, horizon_end=horizon, config=config
        ):
            return "in_week_move"
    return "week_shift"


# --- Restart arithmetic (pure; the block itself is built by the caller) ---


def _best_log_estimate(
    state: BlockState,
    lift: str,
    *,
    from_week: int,
) -> Decimal | None:
    """The best 1RM these logs imply for one lift, or None if they imply none.

    Mirrors `generator.next_block_one_rms`'s accessory path: sets with reps
    outside the estimator's range are skipped rather than raising, because a
    log is a record of what happened and the athlete is allowed to have done
    twenty reps.
    """
    weeks = {block_session.seq: block_session.week for block_session in state.plan}
    best: Decimal | None = None
    for log in state.logs:
        if weeks.get(log.seq, 0) < from_week:
            continue
        for entry in log.entries:
            if entry.lift_code != lift or entry.sets_done < 1:
                continue
            reps = max(entry.reps_done) if isinstance(entry.reps_done, tuple) else entry.reps_done
            if not 1 <= reps <= rep_max.MAX_ESTIMATE_REPS:
                continue
            estimate = rep_max.estimate_1rm(entry.weight_kg, reps)
            best = estimate if best is None else max(best, estimate)
    return best


def restart_one_rms(
    state: BlockState,
    cfg: MissedPolicyConfig = DEFAULT_MISSED_POLICY,
) -> dict[str, Decimal]:
    """The planning maxes a restarted block is built against.

    Two rules, both of which matter:

    **The reduction applies whether or not there are logs.** A lift with no
    recent set is not a lift that held its strength; it is a lift with no
    evidence either way, and the restart's assumption -- weeks away cost
    something -- applies to it equally. Reducing only the lifts that happen to
    have been logged would treat two lifts in the same restart differently for
    a reason that has nothing to do with the athlete.

    **A restart never raises a max.** The estimate is capped at the snapshot
    before the factor is applied. A single good set logged in week 3 is not
    evidence of a new maximum, and ADR-0005 rule 6 reserves raising a max to a
    tested single.

    None of this is written to `one_rep_max`. These are the numbers the new
    block is computed from -- an assumption, visible in the proposal and in
    `block.one_rm_snapshot`, and the athlete's recorded maxes are untouched
    (CLAUDE.md non-negotiable #9).
    """
    from_week = max(1, state.template_weeks - cfg.restart_estimate_window_weeks + 1)
    reduced: dict[str, Decimal] = {}
    for lift, snapshot in state.one_rm_snapshot.items():
        estimate = _best_log_estimate(state, lift, from_week=from_week)
        base = min(estimate, snapshot) if estimate is not None else snapshot
        reduced[lift] = rep_max.quantize_kg(Decimal(base) * cfg.restart_one_rm_factor)
    return reduced


def restart_start_date(event: MissedEvent) -> dt.date:
    """The Monday a restarted block begins on.

    The first Monday on or after the athlete is back. `generate_block` requires
    a Monday, and starting mid-week would put week 1 out of step with the
    weekday map for the rest of the block.
    """
    back = event.today
    if event.unavailable_until is not None:
        back = max(back, event.unavailable_until + dt.timedelta(days=1))
    return back + dt.timedelta(days=(-back.weekday()) % DAYS_PER_WEEK)


# --- Plain-language reasons -----------------------------------------------


def _lifts_of(block_session: DraftSession) -> str:
    return "/".join(lift.lift_code for lift in block_session.lifts)


def _day(date: dt.date) -> str:
    return f"{WEEKDAY_NAMES[date.weekday()]} {date.isoformat()}"


def _move_reason(moved: DraftSession, to: dt.date) -> str:
    return f"Moves {_day(moved.date)}'s {_lifts_of(moved)} to {_day(to)}. Nothing else changes."


def _shift_reason(moved: DraftSession, to: dt.date, weeks: int, new_end: dt.date) -> str:
    return (
        f"{_lifts_of(moved)} was due {_day(moved.date)}. It moves to {_day(to)} with the "
        f"same sets, reps and weight, and everything after it moves back "
        f"{weeks} week{'s' if weeks != 1 else ''}. The block now ends {new_end.isoformat()}."
    )


def _fold_reason(folded: DraftSession, into: DraftSession) -> str:
    return (
        f"{_lifts_of(folded)} was missed on {_day(folded.date)} and you have trained since, "
        f"so it cannot be moved without training out of order. Its lifts join "
        f"{_day(into.date)}'s session, which keeps its own numbers."
    )


def _restart_reasons(
    event: MissedEvent,
    one_rms: Mapping[str, Decimal],
    start: dt.date,
) -> tuple[str, ...]:
    listed = ", ".join(f"{lift} {one_rms[lift]} kg" for lift in sorted(one_rms))
    reasons = [
        f"Restarts at week 1 on {start.isoformat()}, on planning maxes of {listed}.",
        "These are planning numbers for the new block, reduced to allow for time off. "
        "Your recorded 1RMs are unchanged -- only a tested single changes those.",
    ]
    if event.injury:
        reasons.append("Get the injury checked by a physio before progressing.")
    return tuple(reasons)


# --- Proposing -------------------------------------------------------------


def _dropped(result_rules: Sequence[str], tier: Tier, seq: int) -> str:
    rules = ", ".join(result_rules)
    logger.warning(
        "missed-policy option dropped by the validator: %s (tier %s, seq %s)", rules, tier, seq
    )
    return rules


def _proposal(
    state: BlockState,
    *,
    kind: str,
    plan: Sequence[DraftSession],
    changes: Sequence[SessionChange],
    reasons: Sequence[str],
    cost: Mapping[str, float],
    warnings: Sequence[Violation],
    source: ProposalSource,
    block_id: uuid.UUID | None = None,
    supersedes: uuid.UUID | None = None,
    new_one_rms: Mapping[str, Decimal] | None = None,
) -> Proposal[StrengthPayload]:
    return Proposal[StrengthPayload](
        block_id=block_id or state.block_id,
        kind=kind,
        payload=StrengthPayload(
            changes=tuple(changes),
            plan=tuple(plan),
            new_one_rms=dict(new_one_rms) if new_one_rms is not None else None,
            supersedes_block_id=supersedes,
        ),
        cost=dict(cost),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        source=source,
    )


def week_shift(
    state: BlockState,
    event: MissedEvent,
) -> tuple[dict[int, dt.date], int]:
    """The `+7k` translation, as a seq-to-date map and the number of weeks.

    Everything from the first missed session onward moves back whole weeks.
    `k` is the smallest number of weeks that puts the missed session on or
    after today and on a day the athlete is available.

    Whole weeks rather than "the next free day" is what makes this safe
    without a solver, and the proof is short. Adding a multiple of seven
    preserves the weekday, so no session lands off its home day. The sessions
    before the shift all fall before the missed session's original date, and
    every shifted one now falls at or after a strictly later date, so the two
    groups cannot collide and `seq` order still holds. Rest gaps within each
    group are untouched, and gaps that straddle the boundary only widen.
    """
    first = min(event.missed_seqs)
    original = state.by_seq[first].date

    weeks = 1
    while True:
        moved_to = original + dt.timedelta(days=DAYS_PER_WEEK * weeks)
        if moved_to >= event.today and moved_to not in event.unavailable_days:
            break
        weeks += 1

    offset = dt.timedelta(days=DAYS_PER_WEEK * weeks)
    return {
        block_session.seq: block_session.date + offset
        for block_session in state.plan
        if block_session.seq >= first
    }, weeks


def _fold_target(state: BlockState, seq: int, today: dt.date) -> DraftSession | None:
    """The next session still to come that trains at least one of these lifts."""
    lifts = {lift.lift_code for lift in state.by_seq[seq].lifts}
    for block_session in state.plan:
        if block_session.seq <= seq or block_session.date < today:
            continue
        if state.status_of[block_session.seq] != "planned":
            continue
        if lifts & {lift.lift_code for lift in block_session.lifts}:
            return block_session
    return None


def propose(
    event: MissedEvent,
    state: BlockState,
    ctx: ValidationContext,
    cfg: MissedPolicyConfig = DEFAULT_MISSED_POLICY,
    *,
    source: ProposalSource = "chat",
    fast: bool = False,
    config: ValidatorConfig = DEFAULT_CONFIG,
    restart_draft: BlockDraft | None = None,
    restart_block_id: uuid.UUID | None = None,
) -> PolicyResult:
    """Ranked options for this absence, every one of them already validated.

    `ctx` arrives built rather than assembled here, because `test_week` must
    come from the template (ADR-0006 rule 6) and a `BlockState` does not carry
    the template's entries. Each option re-copies it with its own moved seqs
    waived -- the same line `calendar_solver.validator_check` uses, so an
    option that passes here passes again at approval.

    `fast` swaps CP-SAT for the greedy placement in `_feasible_days`. It only
    affects `in_week_move`: the other three tiers compute their plan directly,
    so there is nothing to approximate. With one session free and every other
    pinned, greedy minimises the same cost over the same feasible set that
    CP-SAT searches, and both break ties on the earliest date -- so the two
    must agree, and a test asserts they do.
    """
    tier = classify(event, state, cfg, config=config)
    if tier == "restart":
        return _propose_restart(event, state, cfg, source, restart_draft, restart_block_id)
    if tier == "fold":
        return _propose_fold(event, state, ctx, source, config)
    if tier == "in_week_move":
        return _propose_in_week(event, state, ctx, cfg, source, fast, config)
    return _propose_week_shift(event, state, ctx, source, config)


def _validated(
    ctx: ValidationContext,
    plan: Sequence[DraftSession],
    changes: Sequence[SessionChange],
    config: ValidatorConfig,
) -> ValidationResult:
    """Run the gate with this option's own moves waived."""
    moved = {change.seq for change in changes if change.action in {"move", "create"}}
    option_ctx = ctx.model_copy(
        update={"approved_off_weekday": set(ctx.approved_off_weekday) | moved}
    )
    return validate(plan, option_ctx, changes=changes, config=config)


def _propose_week_shift(
    event: MissedEvent,
    state: BlockState,
    ctx: ValidationContext,
    source: ProposalSource,
    config: ValidatorConfig,
) -> PolicyResult:
    assignments, weeks = week_shift(state, event)
    plan = calendar_solver.to_plan(state.plan, assignments)
    changes = calendar_solver.to_changes(state.plan, assignments)
    result = _validated(ctx, plan, changes, config)
    first = min(event.missed_seqs)
    if not result.ok:
        rules = sorted({violation.rule for violation in result.violations})
        return PolicyResult(
            tier="week_shift",
            failure=PolicyFailed(
                reason="shifting the block by a week still breaks the plan",
                dropped=(_dropped(rules, "week_shift", first),),
            ),
        )

    problem = calendar_solver.problem_from_plan(
        state.plan,
        today=event.today,
        weekday_map=state.weekday_map,
        history_seqs=[seq for seq in state.status_of if seq < first],
        horizon_end=max(assignments.values()),
        config=config,
    )
    cost = calendar_solver.cost_of(problem, assignments)
    return PolicyResult(
        tier="week_shift",
        proposals=(
            _proposal(
                state,
                kind="reschedule",
                plan=plan,
                changes=changes,
                reasons=(
                    _shift_reason(
                        state.by_seq[first],
                        assignments[first],
                        weeks,
                        max(assignments.values()),
                    ),
                ),
                cost=cost.model_dump(),
                warnings=result.warnings,
                source=source,
            ),
        ),
    )


def _propose_fold(
    event: MissedEvent,
    state: BlockState,
    ctx: ValidationContext,
    source: ProposalSource,
    config: ValidatorConfig,
) -> PolicyResult:
    first = min(event.missed_seqs)
    target = _fold_target(state, first, event.today)
    if target is None:
        return PolicyResult(
            tier="fold",
            failure=PolicyFailed(
                reason="no later session trains these lifts, so there is nothing to fold into",
            ),
        )

    folded = state.by_seq[first]
    # The receiving session keeps its own numbers and gains the missed ones.
    # The dates do not move at all, which is what makes this expressible as a
    # change to one session's lifts rather than a reschedule.
    #
    # The merged list is re-sorted the way `generate_block` and
    # `draft_session_of` sort one, so a folded session reads in training order
    # rather than "its own lifts, then the salvaged ones". Both squat entries
    # survive: they carry different sets, reps and intensities, and collapsing
    # them would silently drop a session's work.
    lift_order = {lift: index for index, lift in enumerate(enums.LIFT_CODES)}
    merged = target.model_copy(
        update={
            "lifts": tuple(
                sorted(
                    target.lifts + folded.lifts,
                    key=lambda lift: (lift_order[lift.lift_code], lift.variation or ""),
                )
            )
        }
    )
    plan = tuple(
        merged if block_session.seq == target.seq else block_session
        for block_session in state.plan
        if block_session.seq != first
    )
    changes = (
        SessionChange(
            seq=first,
            week=folded.week,
            slot=folded.slot,
            action="fold",
            into_seq=target.seq,
        ),
    )
    result = _validated(ctx, plan, changes, config)
    if not result.ok:
        return PolicyResult(
            tier="fold",
            failure=PolicyFailed(
                reason=f"folding into session {target.seq} breaks the plan",
                dropped=(_dropped(sorted({v.rule for v in result.violations}), "fold", first),),
            ),
        )
    return PolicyResult(
        tier="fold",
        proposals=(
            _proposal(
                state,
                kind="reschedule",
                plan=plan,
                changes=changes,
                reasons=(_fold_reason(folded, target),),
                cost={
                    "total": 0.0,
                    "delay_days": 0.0,
                    "off_weekday_count": 0.0,
                    "end_slip_days": 0.0,
                },
                warnings=result.warnings,
                source=source,
            ),
        ),
    )


def _propose_in_week(
    event: MissedEvent,
    state: BlockState,
    ctx: ValidationContext,
    cfg: MissedPolicyConfig,
    source: ProposalSource,
    fast: bool,
    config: ValidatorConfig,
) -> PolicyResult:
    first = min(event.missed_seqs)
    moving = state.by_seq[first]
    horizon = state.week_end(moving.week)

    problem = calendar_solver.problem_from_plan(
        state.plan,
        today=event.today,
        weekday_map=state.weekday_map,
        # Everything except the missed session is a fact for this question.
        # Pinning rather than bounding with `hard_end_date` is deliberate:
        # that bounds only the last session, while this bounds the one we are
        # actually asking about and hands the solver its neighbours as walls.
        history_seqs=[seq for seq in state.status_of if seq != first],
        allow_flex_days=True,
        unavailable=event.unavailable_days,
        horizon_end=horizon,
        config=config,
    )

    held = {block_session.seq: block_session.date for block_session in state.plan}
    candidates: list[tuple[Mapping[int, dt.date], ScheduleOption | None]]

    if fast:
        days = _feasible_days(state, event, first, horizon_end=horizon, config=config)
        if not days:
            return PolicyResult(
                tier="in_week_move",
                failure=PolicyFailed(reason="no day left this week keeps the rest gaps"),
            )
        # Cheapest day, earliest wins a tie -- the same order CP-SAT settles
        # on, since it minimises this cost and `solve` sorts its options by
        # `(cost, dates)`.
        chosen = min(
            days,
            key=lambda day: (calendar_solver.cost_of(problem, {**held, first: day}).total, day),
        )
        candidates = [({**held, first: chosen}, None)]
    else:
        found = calendar_solver.solve(
            problem,
            n_options=cfg.solver_n_options,
            time_limit_s=cfg.solver_time_limit_s,
            seed=cfg.solver_seed,
            check=calendar_solver.validator_check(state.plan, ctx, config=config),
            num_workers=cfg.solver_num_workers,
        )
        if isinstance(found, calendar_solver.Infeasible):
            return PolicyResult(
                tier="in_week_move",
                failure=PolicyFailed(reason=found.reason, stats=found.stats),
            )
        candidates = [(option.assignments, option) for option in found]

    proposals: list[Proposal[StrengthPayload]] = []
    dropped: list[str] = []
    for assignments, option in candidates:
        plan = calendar_solver.to_plan(state.plan, assignments)
        changes = calendar_solver.to_changes(state.plan, assignments)
        result = _validated(ctx, plan, changes, config)
        if not result.ok:
            dropped.append(
                _dropped(sorted({v.rule for v in result.violations}), "in_week_move", first)
            )
            continue
        cost = option.cost if option is not None else calendar_solver.cost_of(problem, assignments)
        proposals.append(
            _proposal(
                state,
                kind="reschedule",
                plan=plan,
                changes=changes,
                reasons=(_move_reason(moving, assignments[first]),),
                cost=cost.model_dump(),
                warnings=result.warnings,
                source=source,
            )
        )

    if not proposals:
        return PolicyResult(
            tier="in_week_move",
            failure=PolicyFailed(
                reason="every way of moving this session was rejected by the validator",
                dropped=tuple(dropped),
            ),
        )
    return PolicyResult(tier="in_week_move", proposals=tuple(proposals))


def _propose_restart(
    event: MissedEvent,
    state: BlockState,
    cfg: MissedPolicyConfig,
    source: ProposalSource,
    draft: BlockDraft | None,
    block_id: uuid.UUID | None,
) -> PolicyResult:
    """A restart, given the block the caller has already built and persisted.

    The draft arrives from outside because building one needs the athlete row,
    the template and its 77 entries, none of which belong in a pure module --
    and because `proposal.block_id` is NOT NULL, so the new block row has to
    exist before the proposal naming it can.
    """
    if draft is None or block_id is None:
        return PolicyResult(
            tier="restart",
            failure=PolicyFailed(
                reason="a restart needs a new block, which this call was not given",
            ),
        )
    one_rms = draft.one_rm_snapshot
    return PolicyResult(
        tier="restart",
        proposals=(
            _proposal(
                state,
                kind="new_block",
                plan=draft.sessions,
                changes=(),
                reasons=_restart_reasons(event, one_rms, draft.start_date),
                cost={"total": 0.0},
                warnings=(),
                source=source,
                block_id=block_id,
                # The block this replaces, named rather than inferred: a
                # restart proposed on Monday and approved on Friday must
                # retire the block it was computed against.
                supersedes=state.block_id,
            ),
        ),
    )


__all__ = [
    "DEFAULT_MISSED_POLICY",
    "MissedEvent",
    "MissedPolicyConfig",
    "PolicyFailed",
    "PolicyResult",
    "Tier",
    "classify",
    "propose",
    "restart_one_rms",
    "restart_start_date",
    "week_shift",
]
