"""The business logic behind spec 06's routes. Routes stay thin; this is why.

Five entry points, one of which is the only path that writes a schedule:

* `create_block_proposal` -- generate, validate, persist a `draft` block and a
  `pending` proposal. Writes **no** sessions.
* `approve_proposal` -- the human approval gate. The single writer of
  `planned_session` (CLAUDE.md non-negotiable #2).
* `reject_proposal`, `edit_proposal` -- the other two ways a proposal ends, or
  is amended before it ends.
* `log_session` -- what actually happened, and the one place a *tested* 1RM
  enters the database.

Three conventions that hold throughout:

1. **Nothing here commits.** Every function flushes, matching
   `importer.persist_template` and `generator.persist_block_row`: the caller
   owns the transaction, so a route commits once and a test rolls back. Spec
   15 moves these calls into Temporal activities, which own theirs.
2. **No `HTTPException`.** These raise `planner_core.errors`, which
   `api/main.py` maps to status codes. `approve_proposal` has to stay callable
   from an activity where there is no request to fail.
3. **The clock arrives explicitly.** `today_for` reads it once per call, in the
   athlete's timezone, and passes it into `ValidationContext`. The validator
   itself has no clock (ADR-0006), and a stored verdict is therefore true only
   for the day it was computed -- which is exactly why approval re-validates.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import (
    enums,
    generator,
    missed_policy,
    models,
    notes,
    shaping_solver,
    simulator,
)
from domains.strength.schemas import (
    BlockDraft,
    BlockState,
    DraftSession,
    LoggedLift,
    LoggedSession,
    MeasuredOneRm,
    SessionChange,
    StrengthPayload,
    WeekdayChangePayload,
)
from domains.strength.validator import ValidationContext, read_test_weeks, validate
from planner_core.errors import (
    AlreadyDecided,
    BlockNotRunning,
    NotFound,
    PlanInvalid,
    ProposalStale,
)
from planner_core.llm import Embedder, get_embedder
from planner_core.proposal import ValidationResult, Violation
from planner_core.tracing import traced

logger = logging.getLogger(__name__)

# Statuses a proposal may still be acted on from. Everything else is decided.
_OPEN = "pending"

# A block whose schedule a reschedule may still touch. `draft` is excluded
# deliberately: a draft has no sessions to move.
_RUNNING: tuple[enums.BlockStatus, ...] = ("active", "paused")

# A session whose date is now history. A proposal may not re-date these.
# Defined in `enums` so the policy, the solver and this module cannot drift
# on what counts as settled.
_HISTORY: tuple[str, ...] = enums.HISTORY_STATUSES


class WeekdayLocked(Exception):
    """The weekday map cannot be edited directly right now. → 409.

    Raised when a block is running: the sessions are already pinned to the old
    days, so changing the map underneath them would make every remaining
    session `off_weekday` with no plan to match (spec 06 §11). The route for
    that is a `weekday_change` proposal, which carries the re-dated schedule
    with it.

    Lives here rather than in `planner_core.errors` because a weekday map is a
    strength-domain idea; spec 23's jobshop has no notion of one.
    """

    def __init__(self, block_id: uuid.UUID, status: str) -> None:
        self.block_id = block_id
        self.status = status
        super().__init__(
            f"block {block_id} is {status}; change the weekday map through a "
            "weekday_change proposal"
        )


# --- Reading the athlete's state ------------------------------------------


def today_for(athlete: models.Athlete) -> dt.date:
    """Today in the athlete's own timezone.

    Not `date.today()`: the server may be anywhere, and "is this session in the
    past?" is a question about the athlete's day boundary. Europe/Berlin by
    default (`athlete.timezone`), per the global conventions.
    """
    return dt.datetime.now(ZoneInfo(athlete.timezone)).date()


def today_at(athlete: models.Athlete, as_of: dt.datetime) -> dt.date:
    """`today_for`, but from a clock reading taken somewhere else.

    A Temporal activity may not read the clock (spec 15 §10): it can be
    retried, and a second attempt that lands after midnight would compute a
    different `today` and so a different plan from the same inputs. The
    workflow reads `workflow.now()` once -- the only clock in the system that
    replays the same -- and every activity gets that reading.

    The athlete's own timezone still decides the day boundary, exactly as in
    `today_for`. Only the source of "now" changes.
    """
    return as_of.astimezone(ZoneInfo(athlete.timezone)).date()


def get_athlete(session: Session, athlete_id: uuid.UUID) -> models.Athlete:
    athlete = session.get(models.Athlete, athlete_id)
    if athlete is None:
        raise NotFound("athlete", athlete_id)
    return athlete


def get_template(session: Session, template_id: uuid.UUID) -> models.BlockTemplate:
    template = session.get(models.BlockTemplate, template_id)
    if template is None:
        raise NotFound("template", template_id)
    return template


def get_block(session: Session, block_id: uuid.UUID) -> models.Block:
    block = session.get(models.Block, block_id)
    if block is None:
        raise NotFound("block", block_id)
    return block


def template_entries(session: Session, template_id: uuid.UUID) -> list[models.TemplateEntry]:
    return list(
        session.scalars(
            sa.select(models.TemplateEntry)
            .where(models.TemplateEntry.template_id == template_id)
            .order_by(
                models.TemplateEntry.week,
                models.TemplateEntry.slot,
                models.TemplateEntry.lift_code,
            )
        )
    )


def current_one_rms(session: Session, athlete_id: uuid.UUID) -> dict[str, models.OneRepMax]:
    """The newest 1RM row per lift.

    Newest by `(set_on, created_at)`: two maxes tested on the same day are
    ordered by when they were recorded, so logging a test single wins over a
    number typed in that morning.

    Synthetic rows are **included**, which looks like a violation of ADR-0004
    rule 1 and is not: filtering them out would make `make seed-dev-1rms`
    useless, since the planner cannot plan without a max. The flag travels with
    every row this returns, and the API surfaces it so nothing derived from one
    can be mistaken for real (rule 4).
    """
    rows = session.scalars(
        sa.select(models.OneRepMax)
        .where(models.OneRepMax.athlete_id == athlete_id)
        .order_by(
            models.OneRepMax.lift_code,
            models.OneRepMax.set_on.desc(),
            models.OneRepMax.created_at.desc(),
        )
    )
    newest: dict[str, models.OneRepMax] = {}
    for row in rows:
        # Ordered newest-first within each lift, so the first one wins.
        newest.setdefault(row.lift_code, row)
    return newest


def weekday_slots(session: Session, athlete_id: uuid.UUID) -> dict[int, int]:
    """`{slot: weekday}`, the shape the generator and validator both want."""
    rows = session.scalars(
        sa.select(models.WeekdayMap).where(models.WeekdayMap.athlete_id == athlete_id)
    )
    return {row.slot: row.weekday for row in rows}


def weekday_rows(session: Session, athlete_id: uuid.UUID) -> list[models.WeekdayMap]:
    return list(
        session.scalars(
            sa.select(models.WeekdayMap)
            .where(models.WeekdayMap.athlete_id == athlete_id)
            .order_by(models.WeekdayMap.slot)
        )
    )


def validation_context(
    session: Session,
    athlete: models.Athlete,
    template: models.BlockTemplate,
    entries: Iterable[models.TemplateEntry],
    *,
    weekday_map: Mapping[int, int] | None = None,
    changes: Sequence[SessionChange] = (),
    already_moved: Iterable[int] = (),
    test_week: Mapping[str, int] | None = None,
    overreach_from_week: int | None = None,
) -> ValidationContext:
    """Assemble what the rules measure against (ADR-0006 left this to spec 06).

    `pain_lifts` comes from deterministic keyword screening of the athlete's
    notes over the last 7 days (spec 12), never from embedding similarity.
    `last_done_pct` is the companion ADR-0006's "Open for spec 12" requires:
    rule 2 blocks every future session of a painful lift that has *no*
    baseline, so populating one without the other would make a single pain note
    unplannable rather than capped.

    Both are read across the athlete's whole logged history rather than this
    block's, which is what makes the rule work at a block boundary. A new block
    has nothing completed in it by definition, so a block-scoped baseline would
    be empty exactly when a fresh pain note arrived -- and the athlete could
    not plan their next block at all until the note aged out.

    `approved_off_weekday` is derived from the changes themselves: a session a
    proposal explicitly places on a date **is** an off-weekday move the human
    is being asked to approve, so judging it against the home weekday would
    make every reschedule unapprovable and spec 08 impossible. The seqs a
    proposal does not mention are still held to the map, which is the half of
    the rule that matters -- it catches a solver that moved something it was
    not asked to.

    `already_moved` closes the gap ADR-0009 carried forward. A session parked
    off its home weekday by an **earlier**, already-approved reschedule is not
    mentioned by the current proposal, so the paragraph above would hold it to
    the map and fail it -- which would make the *second* missed session of a
    block unplannable, every time. A session whose stored status is `moved` has
    already been approved onto the day it sits on; re-litigating that is not
    the rule's job. Callers pass the seqs from `BlockState.status_of`.

    `test_week` and `overreach_from_week` normally come from the template, and
    for every ordinary block they still do. Spec 09's shaped blocks are the one
    exception: a shaped block declares its own test weeks (the athlete may have
    asked for the press test in week 11 rather than week 9) and, when its week
    count differs from the template's, its own rescaled overreach week. Reading
    either from the template would then judge the block against a shape it was
    deliberately not built to. This narrows ADR-0006 rule 6 rather than
    discarding it: the override is keyword-only and reachable only from the
    shaping path.
    """
    today = today_for(athlete)
    return ValidationContext(
        weekday_map=dict(weekday_map)
        if weekday_map is not None
        else weekday_slots(session, athlete.id),
        overreach_from_week=(
            overreach_from_week if overreach_from_week is not None else template.overreach_from_week
        ),
        test_week=dict(test_week) if test_week is not None else read_test_weeks(entries),
        plate_increment_kg=athlete.plate_increment_kg,
        approved_off_weekday={
            change.seq for change in changes if change.action in {"move", "create"}
        }
        | set(already_moved),
        pain_lifts=notes.pain_lifts(session, athlete.id, today),
        last_done_pct=last_done_pct(session, athlete.id),
        today=today,
    )


def last_done_pct(session: Session, athlete_id: uuid.UUID) -> dict[str, Fraction]:
    """The highest intensity the athlete has actually completed, per lift.

    ADR-0006 defines this as "the highest percentage completed so far" and uses
    it as a ceiling: a lift with a recent pain note may not be planned above
    what it has already done. Highest rather than most recent, because the
    question the rule asks is "has this load been tolerated before?" -- and an
    easy session last week is not evidence against a heavier one the month
    before. (The `pain_progression` message read "most recently completed"; the
    ADR governs, so the message was corrected rather than the definition. It
    was only ever dead text before this spec, because nothing populated the
    field it describes.)

    "Completed" means the session was logged *and* the log records real work on
    that lift. A logged attempt with zero sets is the athlete loading the bar
    and not lifting it, which is the same reason `log_session` refuses to bank
    it as a 1RM.

    The intensity read is the one that was *prescribed*, because that is what
    the validator compares against. What was logged is a weight, and turning a
    weight back into a percentage needs the 1RM of the day -- a second
    inference where this rule wants a fact.
    """
    rows = session.execute(
        sa.select(
            models.PlannedLift.lift_code,
            models.PlannedLift.pct_num,
            models.PlannedLift.pct_den,
            models.PlannedLift.weight_kg,
            models.SessionLog.entries,
        )
        .join(
            models.PlannedSession,
            models.PlannedSession.id == models.PlannedLift.session_id,
        )
        .join(
            models.SessionLog,
            models.SessionLog.planned_session_id == models.PlannedSession.id,
        )
        .join(models.Block, models.Block.id == models.PlannedSession.block_id)
        .where(models.Block.athlete_id == athlete_id)
    ).all()

    out: dict[str, Fraction] = {}
    for lift_code, pct_num, pct_den, weight_kg, entries in rows:
        if not _logged_at_or_above(entries, lift_code, weight_kg):
            continue
        # Single-argument `Fraction(Decimal)`; the two-argument form raises on
        # Decimals, and float would put binary rounding into an exact ratio.
        pct = Fraction(Decimal(pct_num)) / Fraction(Decimal(pct_den))
        if lift_code not in out or pct > out[lift_code]:
            out[lift_code] = pct
    return out


def _logged_at_or_above(entries: object, lift_code: str, weight_kg: Decimal) -> bool:
    """Did the log record a completed set of this lift *at the prescribed load*?

    The weight matters, and leaving it out made the ceiling inflatable by a
    **failed** attempt: a test single prescribed at 115 kg (107%) and logged at
    90 kg credited 107%, which is above everything else in the block -- so the
    pain rule could never fire for that lift again. The athlete missing a lift
    is the opposite of evidence that they tolerated it.

    The comparison is the same one `log_session` already makes to decide
    whether a test single becomes a 1RM: logged kilograms against the
    prescribed kilograms of that row, same units, no inference. An earlier
    version of this docstring claimed the logged weight could not be used
    without knowing the 1RM of the day; that was simply wrong.
    """
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("lift_code") != lift_code:
            continue
        try:
            if int(entry.get("sets_done", 0)) < 1:
                continue
            reps = entry.get("reps_done")
            if isinstance(reps, list | tuple):
                if not any(int(count) >= 1 for count in reps):
                    continue
            elif int(reps or 0) < 1:
                continue
            # Strings in JSONB, exactly as `log_session` wrote them, so this
            # stays Decimal arithmetic rather than float.
            if Decimal(str(entry.get("weight_kg", 0))) >= Decimal(weight_kg):
                return True
        except (TypeError, ValueError, ArithmeticError):
            continue
    return False


# --- Creating a block proposal --------------------------------------------


def build_block_draft(
    session: Session,
    athlete_id: uuid.UUID,
    template_id: uuid.UUID,
    lifts: Sequence[str],
    start_date: dt.date,
    *,
    one_rms: Mapping[str, Decimal] | None = None,
) -> tuple[BlockDraft, ValidationResult]:
    """Generate and validate, writing nothing. What `/blocks/preview` returns.

    `check_weekday_map` runs first and on its own, because its problems are
    about the athlete's *setup* rather than about this plan -- "Day 2 has no
    weekday" is not a fact about week 7's squat, and `generate_block` would
    otherwise collapse all of them into one `ValueError`.

    `one_rms` overrides the maxes this block is computed from, and its only
    legitimate caller is spec 08's restart. Those are **planning** maxes: a
    restart assumes some strength was lost over the weeks away, so the block
    is built against a reduced number. That number lives in
    `block.one_rm_snapshot` and goes nowhere near `one_rep_max` -- it is an
    assumption, not a measurement, and only the athlete's own tested lift may
    change what the register says they can do (ADR-0005 rule 6, CLAUDE.md #9).
    `current_one_rms` is still read, because the synthetic flag belongs to the
    rows and must still reach the UI.
    """
    athlete = get_athlete(session, athlete_id)
    template = get_template(session, template_id)
    entries = template_entries(session, template_id)
    if not entries:
        raise PlanInvalid.of("template", f"template {template.name} has no entries")

    slots = weekday_slots(session, athlete_id)
    problems = generator.check_weekday_map(slots, entries, lifts)
    if problems:
        raise PlanInvalid(
            ValidationResult.of(
                violations=tuple(
                    Violation(rule="weekday_map", message=problem) for problem in problems
                )
            )
        )

    maxes = current_one_rms(session, athlete_id)
    try:
        draft = generator.generate_block(
            athlete,
            template,
            entries,
            dict(one_rms) if one_rms is not None else {lift: row.kg for lift, row in maxes.items()},
            lifts,
            start_date,
            slots,
        )
    except ValueError as exc:
        # The generator's refusals are deterministic and already phrased for a
        # reader -- "no 1RM for press", "start_date must be a Monday". Wrapped
        # so a client has one error body to parse rather than two (ADR-0005).
        raise PlanInvalid.of("generator", str(exc)) from exc

    # Spec 14's `validator` span sits here rather than inside `validate`:
    # the validator is pure and AST-tested (ADR-0006), so it may not import a
    # context manager that reads a clock. Same for the two solver spans below.
    with traced("validator") as span:
        result = validate(
            draft.sessions,
            validation_context(session, athlete, template, entries, weekday_map=slots),
        )
        span.set_many(
            ok=result.ok,
            violation_rules=sorted({violation.rule for violation in result.violations}),
            warning_count=len(result.warnings),
        )
    return draft, result


def build_shaped_draft(
    session: Session,
    athlete_id: uuid.UUID,
    template_id: uuid.UUID,
    lifts: Sequence[str],
    start_date: dt.date,
    *,
    weeks: int | None = None,
    test_week: Mapping[str, int] | None = None,
) -> tuple[BlockDraft, ValidationResult, shaping_solver.ShapedBlock]:
    """Spec 09's shaped block: generate, solve, validate, write nothing.

    Mirrors `build_block_draft` step for step, with the solver between the
    template and the generator. The template is resampled onto `weeks`, the
    solver bends it as little as it can, and the result goes back through
    `generate_block` -- so the dates, the seq numbering and every weight come
    from exactly the same code an ordinary block uses.

    With `weeks` left alone and no test moved, this reproduces the template
    and therefore `build_block_draft`'s own output, which is what the golden
    test pins.
    """
    athlete = get_athlete(session, athlete_id)
    template = get_template(session, template_id)
    entries = template_entries(session, template_id)
    if not entries:
        raise PlanInvalid.of("template", f"template {template.name} has no entries")

    weeks = weeks if weeks is not None else template.weeks
    chosen = [entry for entry in entries if entry.lift_code in set(lifts)]
    if not chosen:
        raise PlanInvalid.of("template", "no template entries for the chosen lifts")

    slots = weekday_slots(session, athlete_id)
    problems = generator.check_weekday_map(slots, chosen, lifts)
    if problems:
        raise PlanInvalid(
            ValidationResult.of(
                violations=tuple(
                    Violation(rule="weekday_map", message=problem) for problem in problems
                )
            )
        )

    maxes = {lift: row.kg for lift, row in current_one_rms(session, athlete_id).items()}
    reference = shaping_solver.resample_reference(chosen, weeks, template_weeks=template.weeks)
    overreach = shaping_solver.rescale_overreach(
        template.overreach_from_week, weeks, template.weeks
    )
    try:
        problem = shaping_solver.problem_from_reference(
            reference, weeks=weeks, overreach_from_week=overreach, test_week=test_week
        )
    except ValueError as exc:
        raise PlanInvalid.of("shaping", str(exc)) from exc

    # The shaped block declares its own test weeks and its own overreach week,
    # so the context is built from the problem rather than the template. This
    # is the narrow exception to ADR-0006 rule 6 -- see `validation_context`.
    ctx = validation_context(
        session,
        athlete,
        template,
        entries,
        weekday_map=slots,
        test_week=problem.test_week,
        overreach_from_week=overreach,
    )
    with traced("solver.shaping") as span:
        shaped = shaping_solver.solve(
            problem,
            check=shaping_solver.validator_check(
                athlete, template, maxes, lifts, start_date, slots, ctx
            ),
        )
        # Read from the solver's own stats, never a clock -- that is what keeps
        # `shaping_solver` free of `datetime.now()` (ADR-0009, ADR-0011).
        if isinstance(shaped, shaping_solver.ShapingInfeasible):
            span.set("status", "INFEASIBLE")
            span.set("reason", shaped.reason)
        else:
            span.set_many(
                status=shaped.status,
                objective=shaped.cost.total,
                wall_time_s=shaped.stats.wall_time_s,
                weeks=shaped.weeks,
            )
    if isinstance(shaped, shaping_solver.ShapingInfeasible):
        raise PlanInvalid.of("shaping", shaped.reason)

    try:
        draft = shaping_solver.to_draft(shaped, athlete, template, maxes, lifts, start_date, slots)
    except ValueError as exc:
        raise PlanInvalid.of("generator", str(exc)) from exc

    return draft, validate(draft.sessions, ctx), shaped


def create_block_proposal(
    session: Session,
    athlete_id: uuid.UUID,
    template_id: uuid.UUID,
    lifts: Sequence[str],
    start_date: dt.date,
    *,
    one_rms: Mapping[str, Decimal] | None = None,
    source: enums.ProposalSource = "chat",
    new_one_rms: Mapping[str, Decimal] | None = None,
) -> models.Proposal:
    """A `draft` block and the `pending` proposal that would make it real.

    The block row exists from here (`proposal.block_id` is NOT NULL) but has no
    sessions: the schedule lives in the payload until a human approves it.
    `GET /blocks/{id}/sessions` is therefore empty, which spec 06 §12 asserts.

    The three keyword arguments are all spec 15's rollover, and all default to
    what every existing caller already did:

    * `one_rms` overrides the planning maxes, through `build_block_draft`'s
      existing seam. The next block is computed from the maxes the last one
      produced, not from whatever `current_one_rms` says at approval time.
    * `source` finally gets a writer other than `"chat"`. The value has been
      legal since spec 02 and written by nothing.
    * `new_one_rms` fills `StrengthPayload.new_one_rms`, which spec 05 defined
      and nothing produced (ADR-0007 "Deliberately not done 6"). It is a record
      of provenance for the trace view, not an instruction: `approve_proposal`
      still deliberately ignores it, because `finish_block` has already written
      the rows with the real dates the athlete lifted on.
    """
    draft, result = build_block_draft(
        session, athlete_id, template_id, lifts, start_date, one_rms=one_rms
    )
    if not result.ok:
        raise PlanInvalid(result)

    block = generator.persist_block_row(session, draft, status="draft")
    payload = StrengthPayload(
        plan=draft.sessions,
        new_one_rms=dict(new_one_rms) if new_one_rms is not None else None,
    )
    proposal = models.Proposal(
        block_id=block.id,
        kind="new_block",
        status="pending",
        payload=payload.model_dump(mode="json"),
        reasons=[
            f"block {block.number} from template {draft.template_id} starting {draft.start_date}",
        ],
        warnings=[warning.model_dump(mode="json") for warning in result.warnings],
        source=source,
    )
    session.add(proposal)
    session.flush()
    return proposal


# --- Proposals ------------------------------------------------------------


def get_proposal(session: Session, proposal_id: uuid.UUID) -> models.Proposal:
    proposal = session.get(models.Proposal, proposal_id)
    if proposal is None:
        raise NotFound("proposal", proposal_id)
    return proposal


def read_payload(proposal: models.Proposal) -> StrengthPayload | WeekdayChangePayload:
    """The stored JSON back as the typed payload its kind implies."""
    if proposal.kind == "weekday_change":
        return WeekdayChangePayload.model_validate(proposal.payload)
    return StrengthPayload.model_validate(proposal.payload)


def sessions_of(session: Session, block_id: uuid.UUID) -> list[models.PlannedSession]:
    return list(
        session.scalars(
            sa.select(models.PlannedSession)
            .where(models.PlannedSession.block_id == block_id)
            .order_by(models.PlannedSession.seq)
        )
    )


def _rewrite_lifts(
    session: Session,
    row: models.PlannedSession,
    planned: DraftSession,
) -> None:
    """Replace a session's lift rows with the plan's version of them.

    Delete-then-insert rather than a diff. A folded session gains a second
    entry for a lift it already trains -- squat twice, at different sets and
    intensities -- so there is no key to match old rows against new ones by,
    and a diff would have to invent one.

    Reached only from `_apply_plan`'s fold branch, so it stays inside the
    single write path (ADR-0007 rule 1).
    """
    for old in lifts_of(session, row.id):
        session.delete(old)
    session.flush()
    for lift in planned.lifts:
        session.add(
            models.PlannedLift(
                session_id=row.id,
                lift_code=lift.lift_code,
                variation=lift.variation,
                sets=lift.sets,
                reps=lift.reps,
                pct_num=lift.pct_num,
                pct_den=lift.pct_den,
                weight_kg=lift.weight_kg,
                rel_intensity=lift.rel_intensity,
                is_test=lift.is_test,
            )
        )
    session.flush()


def load_block_state(session: Session, block_id: uuid.UUID) -> BlockState:
    """A running block as spec 08's policy reads it: plan, statuses and logs.

    Three bulk queries rather than one per session. A block has 33 sessions
    and the per-row version would be 67 round trips to assemble one object
    that is thrown away at the end of the call.

    The lift rows are re-ordered on the way into each `DraftSession`; see
    `generator.draft_session_of` for why reading them back alphabetically
    would otherwise rename every session the policy talks about.
    """
    block = get_block(session, block_id)
    template = get_template(session, block.template_id)
    rows = sessions_of(session, block_id)
    if not rows:
        # A `draft` block: the schedule is still in a pending proposal's
        # payload, so there is nothing to reschedule around.
        raise PlanInvalid.of("block", f"block {block.number} has no sessions yet")

    ids = [row.id for row in rows]
    lifts_by_session: dict[uuid.UUID, list[models.PlannedLift]] = {row.id: [] for row in rows}
    for lift in session.scalars(
        sa.select(models.PlannedLift).where(models.PlannedLift.session_id.in_(ids))
    ):
        lifts_by_session[lift.session_id].append(lift)

    seq_of = {row.id: row.seq for row in rows}
    logs = [
        LoggedSession(
            seq=seq_of[log.planned_session_id],
            performed_on=log.performed_on,
            entries=tuple(LoggedLift.model_validate(entry) for entry in log.entries),
        )
        for log in session.scalars(
            sa.select(models.SessionLog).where(models.SessionLog.planned_session_id.in_(ids))
        )
    ]

    return BlockState(
        block_id=block.id,
        athlete_id=block.athlete_id,
        template_id=block.template_id,
        status=block.status,
        start_date=block.start_date,
        template_weeks=template.weeks,
        lifts=tuple(block.lifts),
        # Written as strings so JSONB could hold them exactly; read back as the
        # Decimals every weight downstream is computed from.
        one_rm_snapshot={lift: Decimal(kg) for lift, kg in block.one_rm_snapshot.items()},
        weekday_map=weekday_slots(session, block.athlete_id),
        plan=tuple(generator.draft_session_of(row, lifts_by_session[row.id]) for row in rows),
        status_of={row.seq: row.status for row in rows},
        logs=tuple(sorted(logs, key=lambda log: log.seq)),
    )


def propose_for_missed(
    session: Session,
    event: missed_policy.MissedEvent,
    *,
    cfg: missed_policy.MissedPolicyConfig = missed_policy.DEFAULT_MISSED_POLICY,
    source: enums.ProposalSource = "chat",
    fast: bool = False,
) -> missed_policy.PolicyResult:
    """Spec 08's policy, with the database work it cannot do itself.

    The policy is pure and stays that way. Everything impure lives here: the
    block is read into a `BlockState`, the validation context is assembled
    from the template, and -- for a restart only -- the replacement block is
    generated and its row written so the proposal has a `block_id` to point
    at (`proposal.block_id` is NOT NULL).

    A restart therefore persists a `draft` block before the athlete has seen
    the suggestion. That is the same bargain `create_block_proposal` already
    makes: the row exists, it has no sessions, and nothing is scheduled until
    approval. `reject_proposal` leaves it a draft.

    Sessions already moved by an earlier approved reschedule have their
    weekday waived -- see `validation_context`'s `already_moved`.
    """
    state = load_block_state(session, event.block_id)
    athlete = get_athlete(session, state.athlete_id)
    template = get_template(session, state.template_id)
    entries = template_entries(session, state.template_id)
    ctx = validation_context(
        session,
        athlete,
        template,
        entries,
        weekday_map=state.weekday_map,
        already_moved=[seq for seq, status in state.status_of.items() if status == "moved"],
    )

    draft: BlockDraft | None = None
    block_id: uuid.UUID | None = None
    if missed_policy.classify(event, state, cfg) == "restart":
        draft, result = build_block_draft(
            session,
            state.athlete_id,
            state.template_id,
            list(state.lifts),
            missed_policy.restart_start_date(event),
            one_rms=missed_policy.restart_one_rms(state, cfg),
        )
        if not result.ok:
            return missed_policy.PolicyResult(
                tier="restart",
                failure=missed_policy.PolicyFailed(
                    reason="the restarted block does not validate",
                    dropped=tuple(sorted({v.rule for v in result.violations})),
                ),
            )
        block_id = generator.persist_block_row(session, draft, status="draft").id

    # Spec 14's `solver.calendar` span. `calendar_solver.solve` is reached only
    # from pure `missed_policy`, so this is its nearest impure call site and
    # `PolicyResult.stats` is how the numbers get here (ADR-0016).
    with traced("solver.calendar", fast=fast, injury=event.injury) as span:
        result = missed_policy.propose(
            event,
            state,
            ctx,
            cfg,
            source=source,
            fast=fast,
            restart_draft=draft,
            restart_block_id=block_id,
        )
        stats = result.stats or (result.failure.stats if result.failure else None)
        span.set_many(
            tier=result.tier,
            num_options=len(result.proposals),
            ok=result.ok,
        )
        if stats is not None:
            span.set_many(
                status=stats.status,
                objective=stats.objective,
                wall_time_s=stats.wall_time_s,
            )
        if result.failure is not None:
            span.set("reason", result.failure.reason)
    return result


def save_proposals(
    session: Session,
    result: missed_policy.PolicyResult,
) -> list[models.Proposal]:
    """Persist a policy result's proposals as rows the athlete can approve.

    Spec 08 left this undone, and nothing noticed because nothing called
    `propose_for_missed` outside its own tests. `missed_policy` is pure, so
    what it returns are frozen `planner_core.proposal.Proposal` value objects
    -- and `approve_proposal` takes a `proposal_id` and looks up a *row*. Until
    these are written, a reschedule produces options with nothing to approve.

    The row keeps the value object's own `id`, so the id the agent puts in
    `decision.tool_calls` is the id the approval route will be given.

    Ordered as the policy ranked them: cheapest first, which is what makes
    "the first one" a defensible default for `decision.proposal_id` when a
    turn produced three options and the column holds one (ADR-0015).
    """
    rows: list[models.Proposal] = []
    for proposal in result.proposals:
        row = models.Proposal(
            id=proposal.id,
            block_id=proposal.block_id,
            kind=proposal.kind,
            status=proposal.status,
            payload=proposal.payload.model_dump(mode="json"),
            cost=dict(proposal.cost),
            reasons=list(proposal.reasons),
            warnings=[warning.model_dump(mode="json") for warning in proposal.warnings],
            source=proposal.source,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def simulate_block(
    session: Session,
    block_id: uuid.UUID,
    *,
    scenario: Sequence[simulator.ForcedEvent] = (),
    runs: int = simulator.DEFAULT_SIM_CONFIG.runs,
    seed: int = 0,
) -> tuple[simulator.SimulationResult, bool]:
    """Spec 10: what the rest of a running block might look like. Writes nothing.

    Returns the result and whether the block was planned from a synthetic max
    (ADR-0004 rule 4, ADR-0007 rule 5): a projection built on a development
    max must say so, and the route carries the flag out. Matched on the value
    the block's snapshot holds, since a snapshot records no provenance of its
    own; the cost is that a real max equal to a synthetic one reads as
    synthetic, which errs towards saying so.

    Everything impure happens here, once, before the simulation starts -- the
    block is read, the validation context is assembled, the calibration file is
    loaded -- so nothing inside the Monte-Carlo loop touches the database
    (spec 10 §11). The block is gated on its stored status first: a `draft`
    would otherwise reach `load_block_state` and come back as a 422 about
    missing sessions, which describes the symptom rather than the cause.
    """
    block = get_block(session, block_id)
    if block.status not in _RUNNING:
        raise BlockNotRunning(block.id, block.status)

    state = load_block_state(session, block_id)
    athlete = get_athlete(session, state.athlete_id)
    template = get_template(session, state.template_id)
    ctx = validation_context(
        session,
        athlete,
        template,
        template_entries(session, state.template_id),
        weekday_map=state.weekday_map,
        already_moved=[seq for seq, status in state.status_of.items() if status == "moved"],
    )
    with traced("simulator.run", runs=runs, seed=seed) as span:
        try:
            result = simulator.simulate(
                state,
                ctx,
                scenario=scenario,
                cfg=simulator.DEFAULT_SIM_CONFIG.model_copy(update={"runs": runs}),
                seed=seed,
                gains=simulator.load_gains(),
            )
        except simulator.ScenarioError as exc:
            raise PlanInvalid.of("scenario", str(exc)) from exc
        # `wall_time_s` is the simulator's own measurement; the span does not
        # time itself, for the reason given on the `validator` span above.
        span.set_many(
            wall_time_s=result.stats.wall_time_s,
            runs_finished=result.stats.runs_finished,
            runs_restart=result.stats.runs_restart,
            runs_unplannable=result.stats.runs_unplannable,
        )

    # The projection starts from the block's frozen snapshot, so the question is
    # whether *those* numbers were development data -- not what the athlete's
    # newest row says today. A real test single logged since, or a dev seed run
    # since, changes the newest row and neither changes what this block rests on.
    synthetic = any(
        row.is_synthetic
        and row.lift_code in state.lifts
        and Decimal(row.kg) == state.one_rm_snapshot[row.lift_code]
        for row in session.scalars(
            sa.select(models.OneRepMax).where(models.OneRepMax.athlete_id == state.athlete_id)
        )
    )
    return result, synthetic


def _check_fresh(
    session: Session,
    proposal: models.Proposal,
    block: models.Block,
    payload: StrengthPayload | WeekdayChangePayload,
) -> None:
    """Has the block moved on since this proposal was computed?

    Spec 06 says "re-validates against the current block state first; stale →
    ProposalStale" without saying what stale means. It means: the premise this
    proposal was built on is no longer true. Distinct from `PlanInvalid` --
    the plan is not wrong, it is answering a question the database has left
    behind, so the answer is to re-propose rather than to edit.
    """
    existing = sessions_of(session, block.id)

    if proposal.kind == "new_block":
        if block.status != "draft":
            raise ProposalStale(f"block {block.id} is {block.status}, no longer draft")
        if existing:
            # Belt and braces: the block already has a schedule, so approving
            # would duplicate it and trip `uq_planned_session_block_id_seq`.
            raise ProposalStale(f"block {block.id} already has {len(existing)} sessions")
        return

    if block.status not in _RUNNING:
        raise ProposalStale(
            f"block {block.id} is {block.status}; a {proposal.kind} needs it "
            f"{' or '.join(_RUNNING)}"
        )

    by_seq = {row.seq: row for row in existing}

    for change in payload.changes:
        if change.action == "move" and change.from_date is not None:
            current = by_seq.get(change.seq)
            if current is None:
                raise ProposalStale(f"session {change.seq} no longer exists")
            if current.date != change.from_date:
                raise ProposalStale(
                    f"session {change.seq} is on {current.date}, not the "
                    f"{change.from_date} this proposal was computed against"
                )

    for planned in payload.plan:
        current = by_seq.get(planned.seq)
        if current is None:
            continue
        if current.status in _HISTORY and current.date != planned.date:
            raise ProposalStale(
                f"session {planned.seq} is already {current.status} on {current.date} "
                f"and cannot be moved to {planned.date}"
            )


def _apply_plan(
    session: Session,
    block: models.Block,
    payload: StrengthPayload | WeekdayChangePayload,
) -> None:
    """Reconcile the block's sessions to `payload.plan`, keyed on `seq`.

    One code path for every action, which is why `move`, `skip`, `create` and
    `repeat_week` need no branches of their own:

    * in the plan and in the database -> the date is updated, and the session
      is `moved` if it changed. `original_date` is left alone, so the original
      plan stays visible.
    * in the plan only -> written fresh. This is what a `create` and a
      `repeat_week`'s new `seq`s look like from here.
    * in the database only, and named by a `skip` -> `skipped`. A session the
      plan simply does not mention is left alone, because a payload is not
      licence to delete history.
    * named by a `fold` -> `skipped`, and the session it folds **into** has
      its lift rows rewritten from the plan. The one action that changes what
      a session contains rather than when it happens, which is why it is the
      one that needs a branch of its own.

    The plan is the authority on dates rather than the changes, per spec 05: a
    `repeat_week` carries no dates of its own, so reading the diff would miss
    the 7-day push it implies for everything after it.
    """
    existing = {row.seq: row for row in sessions_of(session, block.id)}
    folds = {c.seq: c.into_seq for c in payload.changes if c.action == "fold"}
    # A folded session is written off: its work has moved elsewhere, so
    # leaving it `planned` would have the athlete training it twice.
    skipped = {c.seq for c in payload.changes if c.action == "skip"} | set(folds)

    fresh: list[DraftSession] = []
    for planned in payload.plan:
        current = existing.get(planned.seq)
        if current is None:
            fresh.append(planned)
            continue
        if current.status in _HISTORY:
            # Guarded by `_check_fresh`, which rejects a payload that re-dates
            # a trained session. Reaching here means the date matches anyway.
            continue
        if current.date != planned.date:
            current.date = planned.date
            current.status = "moved"
        session.add(current)

    for seq in sorted(skipped):
        current = existing.get(seq)
        if current is not None and current.status not in _HISTORY:
            current.status = "skipped"
            session.add(current)

    planned_by_seq = {planned.seq: planned for planned in payload.plan}
    for into_seq in sorted({seq for seq in folds.values() if seq is not None}):
        receiver = existing.get(into_seq)
        merged = planned_by_seq.get(into_seq)
        if receiver is None or merged is None or receiver.status in _HISTORY:
            continue
        _rewrite_lifts(session, receiver, merged)

    if fresh:
        generator.persist_planned_sessions(session, block, fresh)
    session.flush()


def _lock_weekday_map(session: Session, block: models.Block) -> None:
    """Pin the slots this block uses, from its start date.

    Only the slots it uses: a deadlift-only block leaves Days 1 and 3 free to
    be chosen, which is the same reasoning `check_weekday_map` applies.
    """
    used = {row.slot for row in sessions_of(session, block.id)}
    for row in weekday_rows(session, block.athlete_id):
        if row.slot in used:
            row.locked_from = block.start_date
            session.add(row)
    session.flush()


def write_weekday_map(session: Session, athlete_id: uuid.UUID, slots: Mapping[int, int]) -> None:
    """Replace the athlete's map with `slots`.

    Delete-then-insert with a flush between, rather than updating in place:
    `uq_weekday_map_athlete_id_weekday` is not deferrable, so swapping Day 1
    and Day 2's weekdays by assignment trips the constraint halfway through
    however the updates are ordered.
    """
    locked = {row.slot: row.locked_from for row in weekday_rows(session, athlete_id)}
    session.execute(sa.delete(models.WeekdayMap).where(models.WeekdayMap.athlete_id == athlete_id))
    session.flush()
    for slot, weekday in sorted(slots.items()):
        session.add(
            models.WeekdayMap(
                athlete_id=athlete_id,
                slot=slot,
                weekday=weekday,
                locked_from=locked.get(slot),
            )
        )
    session.flush()


def running_block(session: Session, athlete_id: uuid.UUID) -> models.Block | None:
    """The athlete's block that is under way, if any."""
    return session.scalar(
        sa.select(models.Block).where(
            models.Block.athlete_id == athlete_id,
            models.Block.status.in_(_RUNNING),
        )
    )


def check_weekday_slots(
    session: Session,
    slots: Mapping[int, int],
    *,
    template_id: uuid.UUID | None = None,
    lifts: Sequence[str] | None = None,
) -> list[str]:
    """Problems with pinning these slots to these weekdays; empty means fine.

    Resolves what to judge the map against, which `check_weekday_map` itself
    cannot do because it is pure: absent a template and a lift list, the newest
    template and every lift it programmes. That is the strictest reading, and
    the only one available before any block exists (spec 17's wizard checks the
    map before choosing lifts).
    """
    if template_id is not None:
        template = get_template(session, template_id)
    else:
        template = session.scalar(
            sa.select(models.BlockTemplate).order_by(models.BlockTemplate.created_at.desc())
        )
    if template is None:
        raise NotFound("template", "any (none imported yet)")

    entries = template_entries(session, template.id)
    chosen = (
        [str(lift) for lift in lifts] if lifts else sorted({entry.lift_code for entry in entries})
    )
    return generator.check_weekday_map(slots, entries, chosen)


def set_weekday_map(
    session: Session,
    athlete_id: uuid.UUID,
    slots: Mapping[int, int],
    *,
    template_id: uuid.UUID | None = None,
    lifts: Sequence[str] | None = None,
) -> list[models.WeekdayMap]:
    """Install the athlete's training days, if that is allowed right now.

    Refused while a block is running: its sessions are already pinned to the
    old days, so moving the map underneath them would make every remaining
    session `off_weekday` with no plan to match. That change goes through a
    `weekday_change` proposal instead (spec 06 §11).
    """
    get_athlete(session, athlete_id)
    running = running_block(session, athlete_id)
    if running is not None:
        raise WeekdayLocked(running.id, running.status)

    problems = check_weekday_slots(session, slots, template_id=template_id, lifts=lifts)
    if problems:
        raise PlanInvalid(
            ValidationResult.of(
                violations=tuple(
                    Violation(rule="weekday_map", message=problem) for problem in problems
                )
            )
        )

    write_weekday_map(session, athlete_id, slots)
    return weekday_rows(session, athlete_id)


def record_one_rep_maxes(
    session: Session,
    athlete_id: uuid.UUID,
    entries: Sequence[MeasuredOneRm],
    *,
    block_id: uuid.UUID | None = None,
    today: dt.date | None = None,
) -> dict[str, models.OneRepMax]:
    """Append measured maxes, and return what is current afterwards.

    Appends rather than overwrites, so the history stays intact and
    `current_one_rms` keeps reading the newest row. A future `set_on` is
    refused: a 1RM is a measurement, and one cannot be measured tomorrow.

    Every entry is checked before any is written, so a bad date in the fifth
    lift does not leave the first four recorded.

    `block_id` attributes the rows to the block they came out of, the way
    `log_session` already attributes a tested single. Spec 15's `finish_block`
    is what passes it; a max the athlete types in belongs to no block.

    `today` is for the same caller. A Temporal activity may not read the clock
    (spec 15 §10), and in a time-skipping test the real clock would call every
    logged date a future one and refuse the lot.
    """
    athlete = get_athlete(session, athlete_id)
    if today is None:
        today = today_for(athlete)

    for entry in entries:
        if entry.set_on > today:
            raise PlanInvalid.of(
                "one_rep_max",
                f"{entry.lift_code} is dated {entry.set_on}, which is after today ({today})",
            )

    for entry in entries:
        session.add(
            models.OneRepMax(
                athlete_id=athlete_id,
                lift_code=entry.lift_code,
                kg=entry.kg,
                set_on=entry.set_on,
                source=entry.source,
                block_id=block_id,
            )
        )
    session.flush()
    return current_one_rms(session, athlete_id)


def approve_proposal(session: Session, proposal_id: uuid.UUID) -> models.Proposal:
    """The human approval gate. The only path that writes a schedule.

    Order matters: decided-already, then stale, then re-validate, then write.
    The re-validation is not ceremony -- the calendar rules judge from `today`
    onward (ADR-0006), so a proposal computed last week may have aged into a
    `past_date` or `off_weekday` breach without anything else changing.
    """
    proposal = get_proposal(session, proposal_id)
    if proposal.status != _OPEN:
        raise AlreadyDecided(proposal.status)

    block = get_block(session, proposal.block_id)
    payload = read_payload(proposal)
    _check_fresh(session, proposal, block, payload)

    athlete = get_athlete(session, block.athlete_id)
    template = get_template(session, block.template_id)
    entries = template_entries(session, block.template_id)
    slots = (
        payload.slots
        if isinstance(payload, WeekdayChangePayload)
        else weekday_slots(session, athlete.id)
    )
    result = validate(
        payload.plan,
        validation_context(
            session, athlete, template, entries, weekday_map=slots, changes=payload.changes
        ),
        changes=payload.changes,
    )
    if not result.ok:
        raise PlanInvalid(result)

    if isinstance(payload, WeekdayChangePayload):
        write_weekday_map(session, athlete.id, payload.slots)

    _apply_plan(session, block, payload)

    if proposal.kind == "new_block":
        block.status = "active"
        session.add(block)
        session.flush()
        _lock_weekday_map(session, block)
        _retire_superseded_block(session, block, payload)

    # `payload.new_one_rms` is deliberately **not** written here. It is set only
    # by a rollover proposal, which is spec 15's `finish_block`, and nothing in
    # spec 06 produces one. Writing it would mean inventing a `set_on`: the
    # field is a `dict[str, Decimal]` with no dates, while ADR-0005's `NewOneRm`
    # carries the athlete's own log date for exactly this reason. A max needs
    # the day it was measured (CLAUDE.md non-negotiable #9), so the row is
    # spec 15's to write, from the logs it already has in hand.

    proposal.status = "approved"
    proposal.decided_at = dt.datetime.now(dt.UTC)
    proposal.warnings = [w.model_dump(mode="json") for w in result.warnings]
    session.add(proposal)

    _supersede_siblings(session, proposal)
    session.flush()
    return proposal


def _retire_superseded_block(
    session: Session,
    block: models.Block,
    payload: StrengthPayload | WeekdayChangePayload,
) -> None:
    """Abandon the block a restart replaces, if the payload names one.

    Only spec 08's restart sets `supersedes_block_id`. Without this the
    athlete would end up with two running blocks and `running_block` would
    have to guess between them.

    The block is **named** by the payload rather than inferred from "whatever
    is active": a restart proposed on Monday and approved on Friday must
    retire the block it was actually computed against. If that block has
    already finished or been abandoned in the meantime, nothing is done --
    retiring it twice is not an error, and refusing the approval over it
    would strand the athlete with no block at all.
    """
    superseded_id = getattr(payload, "supersedes_block_id", None)
    if superseded_id is None or superseded_id == block.id:
        return
    superseded = session.get(models.Block, superseded_id)
    if superseded is None or superseded.status not in _RUNNING:
        return
    superseded.status = "abandoned"
    session.add(superseded)
    session.flush()


def _supersede_siblings(session: Session, approved: models.Proposal) -> None:
    """Every other pending proposal for this block is now answering the past."""
    others = session.scalars(
        sa.select(models.Proposal).where(
            models.Proposal.block_id == approved.block_id,
            models.Proposal.id != approved.id,
            models.Proposal.status == _OPEN,
        )
    )
    for other in others:
        other.status = "superseded"
        other.decided_at = dt.datetime.now(dt.UTC)
        session.add(other)


def reject_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    reason: str | None = None,
) -> models.Proposal:
    """Decline a proposal, and abandon the block if it was never real.

    A rejected `new_block` leaves a `draft` block with no sessions and no way
    to reach `active`, so it goes to `abandoned` rather than lingering as a
    thing the athlete might think is coming.
    """
    proposal = get_proposal(session, proposal_id)
    if proposal.status != _OPEN:
        raise AlreadyDecided(proposal.status)

    proposal.status = "rejected"
    proposal.decided_at = dt.datetime.now(dt.UTC)
    proposal.decision_reason = reason
    session.add(proposal)

    if proposal.kind == "new_block":
        block = get_block(session, proposal.block_id)
        if block.status == "draft":
            block.status = "abandoned"
            session.add(block)

    session.flush()
    return proposal


def edit_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    changes: Sequence[SessionChange],
) -> models.Proposal:
    """Amend a pending proposal's plan, re-validate, and leave it pending.

    The human's edits are applied to the payload's plan and the whole thing is
    re-judged. A failing edit raises rather than storing a rejected plan:
    `Proposal` has nowhere to put violations (ADR-0006), and storing an invalid
    payload would mean an approve that cannot succeed.
    """
    proposal = get_proposal(session, proposal_id)
    if proposal.status != _OPEN:
        raise AlreadyDecided(proposal.status)

    payload = read_payload(proposal)
    plan = _apply_changes(payload.plan, changes)

    block = get_block(session, proposal.block_id)
    athlete = get_athlete(session, block.athlete_id)
    template = get_template(session, block.template_id)
    entries = template_entries(session, block.template_id)
    slots = (
        payload.slots
        if isinstance(payload, WeekdayChangePayload)
        else weekday_slots(session, athlete.id)
    )
    merged = tuple(payload.changes) + tuple(changes)
    result = validate(
        plan,
        validation_context(session, athlete, template, entries, weekday_map=slots, changes=merged),
        changes=merged,
    )
    if not result.ok:
        raise PlanInvalid(result)

    edited = payload.model_copy(update={"plan": plan, "changes": merged})
    proposal.payload = edited.model_dump(mode="json")
    proposal.warnings = [w.model_dump(mode="json") for w in result.warnings]
    proposal.reasons = [*proposal.reasons, f"edited: {len(changes)} change(s) applied by hand"]
    session.add(proposal)
    session.flush()
    return proposal


def _apply_changes(
    plan: Sequence[DraftSession],
    changes: Sequence[SessionChange],
) -> tuple[DraftSession, ...]:
    """Re-date `plan` by `seq` according to `changes`.

    Only `move` is applied here. A `create` or `repeat_week` would have to
    invent sets, reps and weights, which is the solver's job (spec 07) and
    never a hand edit's -- an edit that needs new sessions is a new proposal.
    """
    by_seq = {s.seq: s for s in plan}
    for change in changes:
        if change.action != "move":
            raise PlanInvalid.of(
                "edit",
                f"a hand edit cannot {change.action} a session; that needs a new proposal",
                refs=(f"seq:{change.seq}",),
            )
        current = by_seq.get(change.seq)
        if current is None:
            raise PlanInvalid.of(
                "edit",
                f"this plan has no session {change.seq}",
                refs=(f"seq:{change.seq}",),
            )
        by_seq[change.seq] = current.model_copy(update={"date": change.to_date})
    return tuple(sorted(by_seq.values(), key=lambda s: s.seq))


# --- Logging --------------------------------------------------------------


def get_session_row(session: Session, session_id: uuid.UUID) -> models.PlannedSession:
    planned = session.get(models.PlannedSession, session_id)
    if planned is None:
        raise NotFound("session", session_id)
    return planned


def log_of(session: Session, session_id: uuid.UUID) -> models.SessionLog | None:
    return session.scalar(
        sa.select(models.SessionLog).where(models.SessionLog.planned_session_id == session_id)
    )


def lifts_of(session: Session, session_id: uuid.UUID) -> list[models.PlannedLift]:
    return list(
        session.scalars(
            sa.select(models.PlannedLift)
            .where(models.PlannedLift.session_id == session_id)
            .order_by(models.PlannedLift.lift_code)
        )
    )


def log_session(
    session: Session,
    session_id: uuid.UUID,
    performed_on: dt.date,
    entries: Sequence[LoggedLift],
    note: str | None = None,
    embedder: Embedder | None = None,
    *,
    embed: bool = True,
) -> models.SessionLog:
    """Record what happened, and bank a made test single as a new 1RM.

    The test rule is ADR-0005 modification 5: "completed at the prescribed
    weight" means **at or above**, and the new max is the weight actually
    logged -- going heavier than planned counts in full. A test that was missed
    or failed records nothing, because a test that did not happen is not
    evidence of anything (CLAUDE.md non-negotiable #9).

    `source = "tested"` and `is_synthetic` left false: this is the one path by
    which a real, athlete-measured max enters the database through the API.

    A note is indexed for retrieval afterwards, and **failing to index it never
    fails the log**. The log is what the athlete did; the embedding is a
    convenience built on top of it. Ollama being down, or a model returning the
    wrong width, must not be able to lose a training record -- so the note stays
    in `session_log.note` and `POST /athletes/{id}/notes/reindex` picks it up
    later. That is also why no queue table exists: a log with a note and no
    chunks is the backlog.

    `embed=False` moves the indexing out entirely, which is what the log
    route now does: it commits the record and then dispatches an activity
    (ADR-0014's deferral to spec 15). The default is unchanged, so every
    other caller -- and every test written against them -- behaves as
    before.
    """
    planned = get_session_row(session, session_id)
    if log_of(session, session_id) is not None:
        # Pre-checked rather than left to `uq_session_log_planned_session_id`:
        # an IntegrityError aborts the whole transaction (ADR-0002 rule 1).
        raise AlreadyDecided("logged")

    log = models.SessionLog(
        planned_session_id=session_id,
        performed_on=performed_on,
        # `mode="json"` because this is a JSONB column: a `Decimal` has no JSON
        # adapter, and a float would put binary rounding into the weight the
        # athlete actually lifted. Strings round-trip exactly, and `LoggedLift`
        # reads them straight back as `Decimal`.
        entries=[entry.model_dump(mode="json") for entry in entries],
        note=note,
    )
    session.add(log)
    planned.status = "done"
    session.add(planned)
    session.flush()

    block = get_block(session, planned.block_id)
    prescribed = {lift.lift_code: lift for lift in lifts_of(session, session_id) if lift.is_test}
    for entry in entries:
        test = prescribed.get(entry.lift_code)
        if test is None:
            # Not a test single, however heavy the day went.
            continue
        if entry.weight_kg < test.weight_kg or not _completed(entry):
            continue
        session.add(
            models.OneRepMax(
                athlete_id=block.athlete_id,
                lift_code=entry.lift_code,
                kg=entry.weight_kg,
                set_on=performed_on,
                source="tested",
                block_id=block.id,
            )
        )
    session.flush()
    if embed:
        _index_note(session, log, embedder)
    return log


def _index_note(session: Session, log: models.SessionLog, embedder: Embedder | None) -> None:
    """Index the log's note, or log a warning and leave it for `reindex`.

    Wrapped in a savepoint so a failure part-way cannot poison the transaction
    the caller is about to commit: the log, the status change and any banked
    1RM all have to survive this.

    The `except` is deliberately everything, which needs justifying. The
    invariant being protected is "a training record is never lost to indexing
    it", and that is a statement about *all* failures, not about the three or
    four that were anticipated. Naming a tuple -- `LlmUnavailable`, `ValueError`
    for a malformed or wrong-width response (ADR-0013), `httpx.HTTPError` --
    read as the full set and was not: a wrong-width vector from an `Embedder`
    that does not self-check (the protocol does not require it) reaches the
    database and raises a SQLAlchemy error, which escaped and took the log with
    it. The savepoint is what makes the broad catch safe, and the traceback is
    logged rather than swallowed, so a real bug here is still visible.
    """
    if not log.note or not log.note.strip():
        return
    try:
        with session.begin_nested():
            notes.index_log_note(session, log, embedder or get_embedder())
    except Exception:  # noqa: BLE001 - see the docstring; the log must survive
        logger.warning(
            "Could not embed the note on session %s. "
            "The log is saved; run POST /athletes/{id}/notes/reindex to index it.",
            log.planned_session_id,
            exc_info=True,
        )


def _completed(entry: LoggedLift) -> bool:
    """Did the single actually happen?

    `sets_done` or `reps_done` of zero is a logged *attempt*: the athlete
    loaded the bar and did not lift it. A failed test must not become a 1RM
    (CLAUDE.md non-negotiable #9).
    """
    if entry.sets_done < 1:
        return False
    if isinstance(entry.reps_done, tuple):
        return any(reps >= 1 for reps in entry.reps_done)
    return entry.reps_done >= 1
