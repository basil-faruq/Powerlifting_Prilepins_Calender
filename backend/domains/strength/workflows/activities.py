"""Every side effect the block workflow has. Nothing decides anything here.

The division of labour with `block_workflow.py` is the whole of spec 15's
design: the workflow decides and waits, these touch the world. A workflow
replays its history to recover, so anything that would not come out the same
twice -- a query, a solve, a clock -- has to live on this side of the line
(CLAUDE.md non-negotiable #6).

**Nothing here is reimplemented.** `approve_proposal` is still the single
function that writes a schedule (CLAUDE.md non-negotiable #2); this module
only calls it from somewhere new. That was arranged in advance: the services
take a session and flush without committing (ADR-0007 rule 2) precisely so the
same function could serve an HTTP request and an activity without knowing
which it is in. The one thing that changes is who owns the transaction, and
`unit_of_work` is that owner.

Three rules the whole file obeys:

**One activity, one transaction, one unit of retry.** If an activity fails
halfway, nothing it wrote survives, so the retry starts where the first
attempt did.

**A refusal is not a failure.** `AlreadyDecided`, `ProposalStale`,
`PlanInvalid` and friends are deterministic answers -- retrying them burns
attempts and buries the real cause -- so `_activity` marks them
non-retryable and `planner_core.temporal_errors` carries them back to the
route intact.

**The clock arrives as an argument.** Every request carries `as_of`, which is
the workflow's own `workflow.now()`. A retry at 00:01 must compute the same
`today` as the first attempt at 23:59, because ADR-0006 says the validator's
verdict depends on it.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TypeVar
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlmodel import Session
from temporalio import activity

from domains.strength import enums, generator, missed_policy, models, notes, services
from domains.strength.agent import recorder, tools
from domains.strength.schemas import BlockDraft, MeasuredOneRm
from domains.strength.workflows import contracts
from planner_core import temporal_errors, tracing
from planner_core.db import engine, unit_of_work
from planner_core.errors import LlmUnavailable, NotFound, PlannerError

logger = logging.getLogger(__name__)

# `WeekdayLocked` is a strength-domain refusal -- a weekday map is not an idea
# `planner_core` has -- so it registers itself rather than being listed in the
# engine. Without this a locked map would be retried three times and then
# reported as an infrastructure failure instead of the 409 it is.
temporal_errors.register_refusal(services.WeekdayLocked)

SessionFactory = Callable[[], AbstractContextManager[Session]]

R = TypeVar("R")


def _production_sessions() -> SessionFactory:
    return lambda: unit_of_work(engine)


def _activity(name: str) -> Callable[[Callable[..., R]], Callable[..., R]]:
    """Register an activity, and translate the errors it is allowed to have.

    A `PlannerError` here is the system working: the proposal really was
    already approved, the plan really is invalid. It travels as an
    `ApplicationError` so the route that is waiting gets the same status it
    would have got in-process, and is marked non-retryable so Temporal does
    not ask three more times and then report the wrong cause.

    Anything else is a bug, and is left alone so it looks like one.
    """

    def decorate(fn: Callable[..., R]) -> Callable[..., R]:
        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> R:
            try:
                return fn(*args, **kwargs)
            except PlannerError as exc:
                raise temporal_errors.to_application_error(exc) from exc

        return activity.defn(name=name)(wrapper)

    return decorate


@contextmanager
def _recording() -> Iterator[recorder.Recorder]:
    """A fresh tool-call recorder for this activity.

    `recorder` keeps the active collector in a contextvar, which the chat path
    sets once per turn in `handle_message`. That does not reach here: Temporal
    runs sync activities on a thread pool, which does not inherit the
    submitting context. So an activity that wants `decision.tool_calls` filled
    opens its own.
    """
    with recorder.recording() as rec:
        yield rec


class StrengthActivities:
    """The activities, bound to a way of getting a database session.

    A class rather than module functions so the session factory can be
    injected. In production it is `unit_of_work` over the real engine; in
    tests it is one over the test's own open connection, so everything an
    activity writes is inside the transaction the fixture rolls back. The
    alternative -- reaching for the global engine and monkeypatching it -- is
    how a test suite starts writing to the developer's own database.
    """

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        embedder_factory: Callable[[], object] | None = None,
    ) -> None:
        self._sessions = session_factory or _production_sessions()
        self._embedder_factory = embedder_factory

    def all(self) -> list[Callable[..., object]]:
        """Everything to hand `Worker(activities=...)`.

        Paired with `contracts.ACTIVITY_NAMES` by a test in both directions,
        which is what buys back the type safety lost to naming activities by
        string.
        """
        return [
            self.bind_workflow,
            self.load_block_state,
            self.commit_proposal,
            self.reject_proposal,
            self.apply_edit,
            self.set_block_status,
            self.run_missed_policy,
            self.finish_block,
            self.create_next_block,
            self.simulate_block,
            self.shape_preview,
            self.index_note,
        ]

    # --- Binding ----------------------------------------------------------

    @_activity(contracts.BIND_WORKFLOW)
    def bind_workflow(self, req: contracts.BindRequest) -> None:
        """Record which execution owns this block.

        `block.workflow_id` has existed since spec 02 with nothing writing it
        (ADR-0007 left the question to spec 15). It holds the **chain** id:
        `continue_as_new` keeps the workflow id, so a run started as `block-A`
        that rolls into B is still `block-A`, and B's column says so. That is
        the right answer -- it names the execution that owns B -- and it is
        why the API reads the column rather than deriving the id from the
        block.

        Idempotent by comparison, because an activity can be retried.
        """
        with self._sessions() as session:
            block = services.get_block(session, req.block_id)
            if block.workflow_id != req.workflow_id:
                block.workflow_id = req.workflow_id
                session.add(block)
                session.flush()

    @_activity(contracts.SET_BLOCK_STATUS)
    def set_block_status(self, req: contracts.SetStatusRequest) -> None:
        """Move the block between `active` and `paused`.

        Both already exist in `BlockStatus` and both are treated as running
        (ADR-0010 modification 9) -- a paused block is the one training
        resumes into. Nothing wrote `paused` before this.
        """
        with self._sessions() as session:
            block = services.get_block(session, req.block_id)
            block.status = req.status
            session.add(block)
            session.flush()

    # --- Reading ----------------------------------------------------------

    @_activity(contracts.LOAD_BLOCK_STATE)
    def load_block_state(self, req: contracts.BlockRef) -> contracts.WorkflowBlockState:
        """The block, reduced to what the workflow is allowed to know.

        Thin on purpose. The workflow decides *when* to act, never *what* the
        plan should be, so it gets dates and statuses and no kilograms.

        The deadline is computed here rather than in the workflow because it
        needs the athlete's timezone, and `zoneinfo` is exactly the kind of
        import that has no business inside a replay. It is derived from stored
        data alone -- the session's date, the athlete's zone, a constant -- so
        every replay produces the same instant.
        """
        with self._sessions() as session:
            block = services.get_block(session, req.block_id)
            athlete = services.get_athlete(session, block.athlete_id)
            rows = services.sessions_of(session, req.block_id)
            zone = ZoneInfo(athlete.timezone)
            return contracts.WorkflowBlockState(
                block_id=block.id,
                athlete_id=block.athlete_id,
                status=block.status,
                start_date=block.start_date,
                sessions=tuple(
                    contracts.WorkflowSession(
                        id=row.id,
                        seq=row.seq,
                        week=row.week,
                        slot=row.slot,
                        date=row.date,
                        status=row.status,
                        deadline_utc=_deadline(row.date, zone),
                    )
                    for row in rows
                ),
            )

    # --- Deciding ---------------------------------------------------------

    @_activity(contracts.COMMIT_PROPOSAL)
    def commit_proposal(self, req: contracts.DecideRequest) -> contracts.CommitOutcome:
        """Approve a proposal: the one write path, called from here instead.

        `services.approve_proposal` is unchanged and must stay that way
        (ADR-0007's Action Required says re-host, not reimplement). It
        re-validates against the current state, so an option computed last
        Tuesday is still checked against today before anything moves.

        An unknown or already-decided proposal is **ignored, not failed**.
        Spec §13 asks for that, and the reason is concrete: the athlete can
        approve a chat-proposed reschedule while the workflow sits waiting on
        a session, so an id this workflow never issued may still be real. The
        activity is the thing that judges, and a workflow that died over one
        would strand the whole block.
        """
        with self._sessions() as session:
            found = self._lookup(session, req.proposal_id)
            if found is not None:
                return found

            with tracing.traced(
                "approval",
                parent=req.parent_traceparent,
                proposal_id=req.proposal_id,
            ) as span:
                proposal = services.approve_proposal(session, req.proposal_id)
                payload = services.read_payload(proposal)
                span.set_many(kind=proposal.kind, outcome="approved")
                self._record_outcome(session, req, proposal, "approved")
                return contracts.CommitOutcome(
                    proposal_id=proposal.id,
                    status="approved",
                    kind=proposal.kind,
                    block_id=proposal.block_id,
                    supersedes_block_id=getattr(payload, "supersedes_block_id", None),
                )

    @_activity(contracts.REJECT_PROPOSAL)
    def reject_proposal(self, req: contracts.DecideRequest) -> contracts.CommitOutcome:
        with self._sessions() as session:
            found = self._lookup(session, req.proposal_id)
            if found is not None:
                return found

            with tracing.traced(
                "approval",
                parent=req.parent_traceparent,
                proposal_id=req.proposal_id,
            ) as span:
                proposal = services.reject_proposal(session, req.proposal_id, req.reason)
                span.set_many(kind=proposal.kind, outcome="rejected")
                self._record_outcome(session, req, proposal, "rejected")
                return contracts.CommitOutcome(
                    proposal_id=proposal.id,
                    status="rejected",
                    kind=proposal.kind,
                    block_id=proposal.block_id,
                    detail=req.reason,
                )

    @_activity(contracts.APPLY_EDIT)
    def apply_edit(self, req: contracts.EditRequest) -> contracts.CommitOutcome:
        """Amend a pending proposal. It stays pending -- an edit is a counter-offer."""
        with self._sessions() as session:
            found = self._lookup(session, req.proposal_id)
            if found is not None:
                return found
            proposal = services.edit_proposal(session, req.proposal_id, list(req.changes))
            return contracts.CommitOutcome(
                proposal_id=proposal.id,
                status="edited",
                kind=proposal.kind,
                block_id=proposal.block_id,
            )

    # --- The missed-session path ------------------------------------------

    @_activity(contracts.RUN_MISSED_POLICY)
    def run_missed_policy(self, req: contracts.MissedRequest) -> contracts.MissedOutcome:
        """Ask spec 08 what to do, and leave the options waiting for approval.

        Note what this does **not** do: it never writes `missed`. ADR-0010
        leaves that question to spec 15 and the answer is no -- a session the
        athlete may still make up stays `planned` with a date in the past,
        which is the only thing that lets the calendar solver re-place it.
        `missed` means written off, and nothing but a human writes it off.

        `services.propose_for_missed` is called rather than
        `missed_policy.propose` directly, so that the `solver.calendar` span
        lands where ADR-0016 put it and nests inside the root span opened
        here.
        """
        with self._sessions() as session, _recording():
            state = services.load_block_state(session, req.block_id)
            athlete = services.get_athlete(session, state.athlete_id)
            today = services.today_at(athlete, req.as_of)

            if req.missed_seqs:
                seqs = req.missed_seqs
            elif req.unavailable_from is not None:
                # The athlete named a window, and it may be entirely ahead of
                # them -- "I am away all next week". Asking what has *gone
                # by* would answer "nothing" and the absence would be
                # silently ignored, so the window decides instead. Same
                # derivation the chat tool uses for the same sentence.
                window_end = req.unavailable_until or req.unavailable_from
                seqs = tools.resolve_seqs(state, (), req.unavailable_from, window_end)
            else:
                seqs = tools.missed_seqs(state, today)

            if not seqs:
                # Nothing to repair: the session was logged while this
                # activity was being scheduled, or the named window holds no
                # training. A real outcome, not an error.
                return contracts.MissedOutcome(nothing_to_do=True)

            dates = {s.seq: s.date for s in state.plan}
            unavailable_from = req.unavailable_from or min(dates[seq] for seq in seqs)
            unavailable_until, today = _clamp_window(unavailable_from, req.unavailable_until, today)

            with tracing.traced(
                "workflow.missed_session",
                block_id=req.block_id,
                run_id=req.run_id,
                seqs=seqs,
                injury=req.injury,
            ) as span:
                event = missed_policy.MissedEvent(
                    block_id=req.block_id,
                    missed_seqs=tuple(seqs),
                    unavailable_from=unavailable_from,
                    unavailable_until=unavailable_until,
                    injury=req.injury,
                    today=today,
                )
                result = services.propose_for_missed(session, event, source="workflow")
                rows = services.save_proposals(session, result) if result.ok else []
                span.set_many(tier=result.tier, num_options=len(rows), ok=result.ok)

                decision = self._write_decision(session, req, result, rows, span.trace_id)
                return contracts.MissedOutcome(
                    tier=result.tier,
                    proposal_ids=tuple(row.id for row in rows),
                    missed_seqs=tuple(seqs),
                    decision_id=decision.id,
                    traceparent=tracing.current_traceparent(),
                    failure=result.failure.reason if result.failure else None,
                )

    # --- Rolling into the next block --------------------------------------

    @_activity(contracts.FINISH_BLOCK)
    def finish_block(self, req: contracts.BlockRef) -> contracts.FinishedBlock:
        """Close the block, and bank the maxes it produced.

        `generator.next_block_one_rms` is pure and already decides what each
        lift's max becomes; this is the half that writes. Three rules, and
        only one of them writes anything:

        * `source is None` -- unchanged. The test was missed or failed, or no
          log beat the old max. Writing a row would invent a measurement
          (CLAUDE.md non-negotiable #9).
        * `source == "tested"` -- **already written.** `log_session` banked it
          the moment the athlete logged the single, from the same predicate,
          so writing it again here would double the row. ADR-0005's Action
          Required says exactly this, and a test holds the two predicates
          together.
        * `source == "estimated"` -- written here. It is the only kind of row
          with nowhere else to come from.

        Idempotent twice over, because an activity can be retried. The block's
        own `done` status is the run marker -- it flips in the same
        transaction as the inserts -- and each row is additionally checked for
        before being written, which covers a crash between attempts and an
        athlete who recorded the same max by hand.

        No unique constraint is added to `one_rep_max` to get this. Two rows
        for one lift on one day are legitimate -- a correction -- and
        `current_one_rms` already tie-breaks on `(set_on DESC, created_at
        DESC)`. A schema change to paper over an activity retry would be the
        tail wagging the dog.
        """
        with self._sessions() as session:
            block = services.get_block(session, req.block_id)
            athlete = services.get_athlete(session, block.athlete_id)
            today = services.today_at(athlete, req.as_of)
            state = services.load_block_state(session, req.block_id)
            news = generator.next_block_one_rms(_draft_of(state), state.logs)
            maxes = {lift: new.kg for lift, new in news.items()}

            if block.status == "done":
                # Already ran. Recomputed above because it is pure and the
                # caller still needs the numbers.
                return contracts.FinishedBlock(
                    block_id=block.id, new_one_rms=maxes, already_done=True
                )

            fresh = [
                MeasuredOneRm(
                    lift_code=new.lift_code,
                    kg=new.kg,
                    set_on=new.set_on,
                    source="estimated",
                )
                for new in news.values()
                if new.source == "estimated"
                and new.set_on is not None
                and not _already_recorded(session, block.athlete_id, new)
            ]
            if fresh:
                services.record_one_rep_maxes(
                    session, block.athlete_id, fresh, block_id=block.id, today=today
                )

            block.status = "done"
            session.add(block)
            session.flush()
            return contracts.FinishedBlock(
                block_id=block.id,
                new_one_rms=maxes,
                written=tuple(entry.lift_code for entry in fresh),
            )

    @_activity(contracts.CREATE_NEXT_BLOCK)
    def create_next_block(self, req: contracts.NextBlockRequest) -> contracts.NextBlockOutcome:
        """Generate the next block from the maxes the last one produced.

        The start date is derived from the plan, not from a clock: the Monday
        after the last session, or the next Monday from today if that is
        already past. Taking the later of the two keeps the validator's
        `past_date` rule quiet, and deriving it from the plan at all is what
        makes a time-skipped test produce a sensible date instead of one
        eleven weeks behind.

        The weekday map needs nothing: approving the previous block locked it
        (`_lock_weekday_map`), and the new draft is generated against it. Same
        lifts, same template, new numbers -- which is the whole idea of a
        block rolling over.
        """
        with self._sessions() as session:
            state = services.load_block_state(session, req.block_id)
            athlete = services.get_athlete(session, state.athlete_id)
            today = services.today_at(athlete, req.as_of)
            last = max(block_session.date for block_session in state.plan)
            start = max(_monday_after(last), _monday_on_or_after(today))

            proposal = services.create_block_proposal(
                session,
                state.athlete_id,
                state.template_id,
                list(state.lifts),
                start,
                one_rms=req.one_rms,
                source="workflow",
                new_one_rms=req.one_rms,
            )
            return contracts.NextBlockOutcome(
                next_block_id=proposal.block_id, proposal_id=proposal.id
            )

    # --- Questions, not decisions -----------------------------------------
    #
    # None of these three is part of a block's state machine. They were
    # deferred here by ADR-0011, ADR-0012 and ADR-0014 for one shared
    # reason: each is slow work that was running on the API's own
    # threadpool, where it competes with every other request and dies with
    # the process. Each gets its own one-activity workflow.

    @_activity(contracts.SIMULATE_BLOCK)
    def simulate_block(self, req: contracts.SimulateRequest) -> dict[str, object]:
        """A thousand Monte-Carlo runs, off the request thread (ADR-0012).

        Returns JSON rather than the `SimulationResult` itself: the workflow
        that calls this is a pass-through and must not import `simulator`,
        which carries numpy and simpy. The route rebuilds the model.

        No heartbeat, deliberately. `services.simulate_block` is one opaque
        call -- the Monte-Carlo loop is inside the simulator -- so the only
        place a heartbeat could go is after the work, where it says nothing.
        A sync activity cannot be interrupted part-way in any case, so
        `start_to_close_timeout` is the real bound on a runaway solve.
        """
        with self._sessions() as session:
            result, synthetic = services.simulate_block(
                session,
                req.block_id,
                scenario=list(req.scenario),
                runs=req.runs,
                seed=req.seed,
            )
            return {
                **result.model_dump(mode="json"),
                "block_id": str(req.block_id),
                "has_synthetic_one_rms": synthetic,
            }

    @_activity(contracts.SHAPE_PREVIEW)
    def shape_preview(self, req: contracts.ShapeRequest) -> dict[str, object]:
        """CP-SAT for a block the template does not cover (ADR-0011).

        An infeasible request raises `PlanInvalid`, which crosses the wire as
        a refusal and is rebuilt by the client, so the route still answers
        422 with the same body it did when this ran in-process. That round
        trip is the fiddliest thing in this file and has a test of its own.
        """
        with self._sessions() as session:
            draft, result, shaped = services.build_shaped_draft(
                session,
                req.athlete_id,
                req.template_id,
                list(req.lifts),
                req.start_date,
                weeks=req.weeks,
                test_week=req.test_week,
            )
            maxes = services.current_one_rms(session, req.athlete_id)
            return {
                "draft": draft.model_dump(mode="json"),
                "validation": result.model_dump(mode="json"),
                "has_synthetic_one_rms": any(
                    row.is_synthetic for lift, row in maxes.items() if lift in draft.lifts
                ),
                "weeks": shaped.weeks,
                "test_week": dict(shaped.test_week),
                "overreach_from_week": shaped.overreach_from_week,
                "cost": dict(shaped.cost),
                "stats": shaped.stats.model_dump(mode="json"),
            }

    @_activity(contracts.INDEX_NOTE)
    def index_note(self, req: contracts.IndexNoteRequest) -> contracts.IndexNoteOutcome:
        """Embed a logged note after the log is already safe (ADR-0014).

        Idempotent by construction, not by care: `index_log_note` deletes the
        note's chunks before inserting, so a retry replaces rather than
        doubles. That is the same property `reindex` already relied on.

        A model that is down is reported, not raised. The note stays in
        `session_log.note` and `POST /athletes/{id}/notes/reindex` picks it
        up -- which is exactly the backlog ADR-0014 chose instead of a queue
        table, and the reason this is allowed to fail quietly at all.
        """
        if self._embedder_factory is None:
            return contracts.IndexNoteOutcome(skipped="no embedder configured")
        with self._sessions() as session:
            log = session.get(models.SessionLog, req.log_id)
            if log is None:
                raise NotFound("session log", req.log_id)
            try:
                chunks = notes.index_log_note(session, log, self._embedder_factory())
            except LlmUnavailable as exc:
                logger.warning("could not embed note for log %s: %s", req.log_id, exc)
                return contracts.IndexNoteOutcome(skipped=str(exc))
            return contracts.IndexNoteOutcome(chunks=len(chunks))

    # --- Shared helpers ---------------------------------------------------

    def _lookup(self, session: Session, proposal_id: uuid.UUID) -> contracts.CommitOutcome | None:
        """`None` to go ahead; an `ignored` outcome to stop without failing.

        Pre-checked rather than left to `approve_proposal` raising, because
        the two cases want opposite treatment: a proposal that does not exist
        is something to shrug at, and one that exists but is stale is a 409
        the athlete needs to see.
        """
        try:
            proposal = services.get_proposal(session, proposal_id)
        except NotFound:
            logger.warning("workflow was sent an unknown proposal %s; ignoring", proposal_id)
            return contracts.CommitOutcome(
                proposal_id=proposal_id, status="ignored", detail="no such proposal"
            )
        if proposal.status != "pending":
            logger.warning(
                "workflow was sent proposal %s, already %s; ignoring", proposal_id, proposal.status
            )
            return contracts.CommitOutcome(
                proposal_id=proposal_id,
                status="ignored",
                kind=proposal.kind,
                block_id=proposal.block_id,
                detail=f"already {proposal.status}",
            )
        return None

    def _record_outcome(
        self,
        session: Session,
        req: contracts.DecideRequest,
        proposal: models.Proposal,
        outcome: enums.DecisionOutcome,
    ) -> None:
        """Close the decision this proposal came from.

        `decision.outcome` is nullable and null means "a proposal is waiting"
        (ADR-0015). This is the moment it stops waiting, and spec 14 §5E
        assigned the write to spec 15.

        The decision is found by id when the workflow knows it, and by
        `proposal_id` when it does not -- which is the chat-originated case,
        approved through the workflow without the workflow having seen the
        turn that produced it.
        """
        decision = None
        if req.decision_id is not None:
            decision = session.get(models.Decision, req.decision_id)
        if decision is None:
            decision = session.scalars(
                sa.select(models.Decision)
                .where(models.Decision.proposal_id == proposal.id)
                .order_by(models.Decision.created_at.desc())
                .limit(1)
            ).first()
        if decision is None:
            return
        decision.outcome = outcome
        session.add(decision)
        session.flush()

    def _write_decision(
        self,
        session: Session,
        req: contracts.MissedRequest,
        result: missed_policy.PolicyResult,
        rows: list[models.Proposal],
        trace_id: str | None,
    ) -> models.Decision:
        """The audit row for a reschedule nobody asked for in words.

        `source = "workflow"` has been a legal value since spec 02 and nothing
        has ever written it; `GET /decisions?source=workflow` was built in
        spec 14 waiting for this. `run_id` is the Temporal run id, which
        ADR-0015 named as the natural value -- one run, one execution.

        `outcome` is left null: the options exist and the athlete has not
        chosen. It is set by `_record_outcome` when they do. A policy that
        produced nothing is `failed`, because that turn is over.
        """
        decision = models.Decision(
            run_id=req.run_id,
            source="workflow",
            route="reschedule",
            tool_calls=_dump_calls(),
            proposal_id=rows[0].id if rows else None,
            outcome=None if rows else "failed",
            langfuse_trace_id=trace_id,
        )
        session.add(decision)
        session.flush()
        return decision


def _draft_of(state) -> BlockDraft:
    """A `BlockState` seen as the `BlockDraft` the generator reads.

    No adapter is needed between the two: `load_block_state` already builds
    `plan` out of `DraftSession` through `generator.draft_session_of`, and
    `logs` out of `LoggedSession`, precisely so a block read from the database
    is judged by the same code that judges one just generated.
    """
    return BlockDraft(
        athlete_id=state.athlete_id,
        template_id=state.template_id,
        template_weeks=state.template_weeks,
        start_date=state.start_date,
        lifts=state.lifts,
        one_rm_snapshot=state.one_rm_snapshot,
        sessions=state.plan,
    )


def _already_recorded(session: Session, athlete_id: uuid.UUID, new) -> bool:
    """Has this exact max already been written?

    The second half of `finish_block`'s idempotence, for what the status
    marker cannot cover: a crash between the insert and the status flip, or
    an athlete who entered the same number by hand. Checked in Python rather
    than left to the database, because an `IntegrityError` aborts the whole
    transaction (ADR-0002 rule 1) -- the same reason `log_session` pre-checks
    its duplicate log.
    """
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(models.OneRepMax)
        .where(
            models.OneRepMax.athlete_id == athlete_id,
            models.OneRepMax.lift_code == new.lift_code,
            models.OneRepMax.set_on == new.set_on,
            models.OneRepMax.kg == new.kg,
            models.OneRepMax.source == new.source,
        )
    )
    return bool(count)


def _monday_after(day: dt.date) -> dt.date:
    """The first Monday strictly after `day`.

    Blocks start on a Monday and a CHECK constraint on `block.start_date`
    enforces it, so this is not a convenience.
    """
    return day + dt.timedelta(days=7 - day.weekday())


def _monday_on_or_after(day: dt.date) -> dt.date:
    return day if day.weekday() == 0 else _monday_after(day)


def _dump_calls() -> list[dict[str, object]]:
    rec = recorder.current()
    return rec.dump() if rec is not None else []


def _deadline(date: dt.date, zone: ZoneInfo) -> dt.datetime:
    """When the workflow stops waiting for a session dated `date`.

    Midnight *after* that day in the athlete's own timezone, plus a grace
    period -- so a session trained late in the evening still counts, and the
    athlete is not asked about tomorrow's session at one minute past midnight.
    The athlete's zone, not the server's, for the same reason `today_for`
    uses it: whether a session is in the past is a question about their day.
    """
    midnight_after = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0, 0), tzinfo=zone)
    return (midnight_after + dt.timedelta(hours=contracts.GRACE_HOURS)).astimezone(dt.UTC)


def _clamp_window(
    unavailable_from: dt.date,
    unavailable_until: dt.date | None,
    today: dt.date,
) -> tuple[dt.date | None, dt.date]:
    """Make an absence window one `MissedEvent` will accept.

    `MissedEvent` refuses `today < unavailable_from`, which is right for the
    model and awkward for a caller: the athlete can say "I'm away from
    Monday" on Saturday. ADR-0015 settled the shape for the chat tool -- move
    `today` forward rather than weaken the model -- and a workflow reporting
    the same absence has to do the same thing or the event will not build.

    An open-ended window that has not started yet is closed at its own start,
    so "I can't train Monday" means Monday and not the rest of the block.
    """
    if unavailable_from <= today:
        return unavailable_until, today
    return (unavailable_until or unavailable_from), unavailable_from


__all__ = ["StrengthActivities"]
