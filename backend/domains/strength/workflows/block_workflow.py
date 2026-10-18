"""One durable execution per block, alive for the whole eleven weeks.

It sleeps on a timer until each session's day has passed, notices one that
went unlogged, asks spec 08 what to do about it, waits for the athlete to
approve a repair, and rolls into the next block when the maxes are retested.

**Nothing here touches the world.** A workflow recovers by replaying its own
history, so every line has to produce the same result the second time
(CLAUDE.md non-negotiable #6). No database, no solver, no `datetime.now()`,
no randomness, no `zoneinfo` -- all of it lives in `activities.py`, and AST
tests in `tests/test_block_workflow.py` enforce the boundary rather than
leaving it to prose.

That is also why activities are called by string name. Importing
`activities.py` would drag SQLAlchemy and CP-SAT in through `services`, so
`contracts.py` is the only project module imported here, under
`imports_passed_through` because re-importing Pydantic per run in the sandbox
is both slow and fragile.

**Approve, reject and edit are updates, not signals.** Spec 15 §3 made all
seven events signals, but `POST /proposals/{id}/approve` has to answer with
the decided proposal -- that is the existing contract and spec 16 generates
its frontend types from it. A signal cannot tell the caller what happened.
The other four really are fire-and-forget: the database is already written
and the workflow is only being told to look.

Two things the spec does not mention and the workflow would be broken
without:

* **A rejected reschedule must not loop.** The session stays `planned` with a
  date in the past -- that is ADR-0010's rule and the thing that lets it be
  re-placed -- so the next pass would propose again immediately, and again,
  filling the history and the decision log with the same question. Hence the
  cooldown.
* **A commit that activates a *different* block is a rollover.** One rule
  covers both the next block and spec 08's restart, because
  `approve_proposal` has already retired the superseded one by then.
"""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from domains.strength.workflows import contracts

# Deterministic refusals: the proposal really was already decided, the plan
# really is invalid. Asking again cannot change the answer, and a retried
# `AlreadyDecided` would be reported instead of whatever actually happened.
_NEVER_RETRY = [
    "AlreadyDecided",
    "BlockNotRunning",
    "NotFound",
    "PlanInvalid",
    "ProposalStale",
    "WeekdayLocked",
]

_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    non_retryable_error_types=_NEVER_RETRY,
)

# A bookkeeping write, a read, and the slow ones. The solver gets the 60 s
# spec 15 §5A asks for: `MissedPolicyConfig.solver_time_limit_s` is 5 s, but
# an in-week move solves and then validates up to three options.
_QUICK = timedelta(seconds=15)
_READ = timedelta(seconds=30)
_SLOW = timedelta(seconds=60)


@workflow.defn(name="BlockWorkflow")
class BlockWorkflow:
    """The state machine for one block.

    `ACTIVE → (AWAITING_RESCHEDULE_APPROVAL) → ACTIVE … → DONE`, with `PAUSED`
    and `ERROR` as interruptions either side can leave.
    """

    def __init__(self) -> None:
        self._block_id: uuid.UUID | None = None
        self._state: contracts.WorkflowState = "ACTIVE"
        self._plan: tuple[contracts.WorkflowSession, ...] = ()
        self._pending: list[uuid.UUID] = []
        self._tier: str | None = None
        self._error: str | None = None

        # Set by a signal or an update, cleared by the loop that acts on it.
        self._wake = False
        self._stale = True
        self._paused = False
        self._paused_on: dt.date | None = None
        self._unavailable: tuple[dt.date, dt.date | None, bool] | None = None
        self._rollover: uuid.UUID | None = None
        self._finished = False

        # The thread back to the Langfuse trace and the `decision` row that
        # produced the options now waiting, so approving them lands in the
        # same trace rather than starting a lonely second one.
        self._decision_id: uuid.UUID | None = None
        self._traceparent: str | None = None

        # What stops a rejected reschedule from being re-proposed forever.
        self._asked_about: tuple[int, ...] = ()
        self._quiet_until: dt.datetime | None = None

    # --- Updates: the caller wants an answer ------------------------------

    @workflow.update(name=contracts.APPROVE)
    async def approve(self, proposal_id: uuid.UUID) -> contracts.CommitOutcome:
        outcome = await workflow.execute_activity(
            contracts.COMMIT_PROPOSAL,
            contracts.DecideRequest(
                proposal_id=proposal_id,
                as_of=workflow.now(),
                decision_id=self._decision_id,
                parent_traceparent=self._traceparent,
            ),
            start_to_close_timeout=_SLOW,
            retry_policy=_RETRY,
            result_type=contracts.CommitOutcome,
        )
        self._settle(proposal_id)

        if (
            outcome.status == "approved"
            and outcome.kind == "new_block"
            and outcome.block_id is not None
            and outcome.block_id != self._block_id
        ):
            # The one rule that covers both the rollover and spec 08's
            # restart: committing this made a *different* block active, so
            # this execution now belongs to that block. `approve_proposal`
            # already retired the superseded one inside the activity's
            # transaction, so there is nothing to undo here.
            self._rollover = outcome.block_id
        self._refresh()
        return outcome

    @workflow.update(name=contracts.REJECT)
    async def reject(
        self, proposal_id: uuid.UUID, reason: str | None = None
    ) -> contracts.CommitOutcome:
        outcome = await workflow.execute_activity(
            contracts.REJECT_PROPOSAL,
            contracts.DecideRequest(
                proposal_id=proposal_id,
                as_of=workflow.now(),
                decision_id=self._decision_id,
                parent_traceparent=self._traceparent,
                reason=reason,
            ),
            start_to_close_timeout=_READ,
            retry_policy=_RETRY,
            result_type=contracts.CommitOutcome,
        )
        was_rollover = self._state == "AWAITING_ROLLOVER_APPROVAL"
        self._settle(proposal_id)

        if not self._pending:
            if was_rollover:
                # Declining the next block is how a chain ends on purpose.
                self._finished = True
            else:
                # Every repair refused. The session is still `planned` with a
                # date in the past, so without this the next pass asks the
                # same question immediately and forever.
                self._quiet_until = workflow.now() + timedelta(
                    hours=contracts.MISSED_RETRY_COOLDOWN_HOURS
                )
        self._refresh()
        return outcome

    @workflow.update(name=contracts.EDIT)
    async def edit(
        self, proposal_id: uuid.UUID, changes: tuple[contracts.SessionChange, ...]
    ) -> contracts.CommitOutcome:
        outcome = await workflow.execute_activity(
            contracts.APPLY_EDIT,
            contracts.EditRequest(proposal_id=proposal_id, as_of=workflow.now(), changes=changes),
            start_to_close_timeout=_SLOW,
            retry_policy=_RETRY,
            result_type=contracts.CommitOutcome,
        )
        # Still pending: an edit is a counter-offer, not an approval.
        self._wake = True
        return outcome

    # --- Signals: the world has already changed ---------------------------

    @workflow.signal(name=contracts.SESSION_LOGGED)
    def session_logged(self, session_id: uuid.UUID) -> None:
        """The athlete trained. The row already says so; this only wakes us.

        It carries nothing the database does not have, which is exactly why
        the log route can commit first and signal afterwards -- and why a
        signal lost to a restart costs nothing. The deadline timer is the
        backstop: it reloads and sees `done` either way.
        """
        self._stale = True
        self._wake = True

    @workflow.signal(name=contracts.REPORT_UNAVAILABLE)
    def report_unavailable(
        self, from_date: dt.date, until: dt.date | None = None, injury: bool = False
    ) -> None:
        self._unavailable = (from_date, until, injury)
        self._wake = True

    @workflow.signal(name=contracts.PAUSE)
    def pause(self, reason: str | None = None) -> None:
        self._paused = True
        self._paused_on = workflow.now().date()
        self._wake = True

    @workflow.signal(name=contracts.RESUME)
    def resume(self, on_date: dt.date | None = None) -> None:
        """Pick training back up, treating the gap as an absence.

        A pause is not a separate mechanism: whatever was due while the block
        was paused is exactly an absence, so it goes through the one
        reschedule path rather than a second one that would need its own
        rules and its own bugs.
        """
        back = on_date or workflow.now().date()
        if self._paused_on is not None and back > self._paused_on:
            self._unavailable = (self._paused_on, back - timedelta(days=1), False)
        self._paused = False
        self._paused_on = None
        self._stale = True
        self._wake = True

    # --- Queries ----------------------------------------------------------

    @workflow.query(name=contracts.CURRENT_STATE)
    def current_state(self) -> contracts.WorkflowStateView:
        return contracts.WorkflowStateView(
            state=self._state,
            block_id=self._block_id or uuid.UUID(int=0),
            next_session=self._next_planned(),
            pending_proposal_ids=tuple(self._pending),
            tier=self._tier,  # type: ignore[arg-type]
            error=self._error,
        )

    @workflow.query(name=contracts.NEXT_SESSION)
    def next_session(self) -> contracts.WorkflowSession | None:
        return self._next_planned()

    # --- The run loop -----------------------------------------------------

    @workflow.run
    async def run(
        self, block_id: uuid.UUID, first_proposal_id: uuid.UUID | None = None
    ) -> contracts.WorkflowStateView:
        self._block_id = block_id
        await workflow.execute_activity(
            contracts.BIND_WORKFLOW,
            contracts.BindRequest(block_id=block_id, workflow_id=workflow.info().workflow_id),
            start_to_close_timeout=_QUICK,
            retry_policy=_RETRY,
        )
        if first_proposal_id is not None:
            # The `new_block` approval that started this execution. Committed
            # here rather than by the route, so the single write path stays
            # single even for the very first schedule.
            await self.approve(first_proposal_id)

        while True:
            if self._rollover is not None:
                await self._continue_into(self._rollover)
            if self._finished:
                self._state = "DONE"
                return self.current_state()
            if workflow.info().is_continue_as_new_suggested():
                # The same block, a fresh history. A pathological run -- many
                # reschedules, many edits -- must not grow without bound
                # either (spec 15 §11).
                await self._continue_into(block_id)

            try:
                await self._step()
            except ActivityError as exc:
                # Retries are exhausted or the failure is a refusal. Hold the
                # state rather than dying: the block is eleven weeks of the
                # athlete's work, and a failed activity is usually something
                # a human can fix and then poke (spec 15 §13).
                self._error = _reason(exc)
                self._state = "ERROR"
                await self._sleep_until_woken()
                self._error = None

    async def _step(self) -> None:
        """One pass: reload if needed, then wait, ask or roll over."""
        if self._stale:
            self._stale = False
            await self._reload()

        if self._paused:
            self._state = "PAUSED"
            await workflow.execute_activity(
                contracts.SET_BLOCK_STATUS,
                contracts.SetStatusRequest(block_id=self._require_block(), status="paused"),
                start_to_close_timeout=_QUICK,
                retry_policy=_RETRY,
            )
            await self._sleep_until_woken()
            await workflow.execute_activity(
                contracts.SET_BLOCK_STATUS,
                contracts.SetStatusRequest(block_id=self._require_block(), status="active"),
                start_to_close_timeout=_QUICK,
                retry_policy=_RETRY,
            )
            self._state = "ACTIVE"
            return

        if self._pending:
            # Options are on the table. Nothing moves until the athlete
            # decides, and there is no timeout on that -- a plan the human
            # never approved is not one to apply by default (CLAUDE.md #2).
            await self._sleep_until_woken()
            return

        if self._unavailable is not None:
            window = self._unavailable
            self._unavailable = None
            await self._ask_policy(window)
            return

        upcoming = self._next_planned()
        if upcoming is None:
            await self._roll_over()
            return

        self._state = "ACTIVE"
        remaining = upcoming.deadline_utc - workflow.now()
        if remaining > timedelta(0):
            await self._sleep_until_woken(timeout=remaining)
            return

        overdue = self._overdue()
        if overdue == self._asked_about and self._quiet_until is not None:
            quiet_for = self._quiet_until - workflow.now()
            if quiet_for > timedelta(0):
                # Already asked about exactly these, and the athlete said no.
                await self._sleep_until_woken(timeout=quiet_for)
                return
        await self._ask_policy(None)

    # --- The two things a step can do -------------------------------------

    async def _ask_policy(self, window: tuple[dt.date, dt.date | None, bool] | None) -> None:
        """Ask spec 08 for repair options and put them in front of the athlete.

        Note what never happens: the session is not marked `missed`. ADR-0010
        leaves that question to spec 15 and the answer is no -- `missed` means
        written off, and a session the athlete may still make up has to stay
        `planned` for the calendar solver to be allowed to move it.
        """
        overdue = self._overdue()
        outcome = await workflow.execute_activity(
            contracts.RUN_MISSED_POLICY,
            contracts.MissedRequest(
                block_id=self._require_block(),
                as_of=workflow.now(),
                run_id=workflow.info().run_id,
                unavailable_from=window[0] if window else None,
                unavailable_until=window[1] if window else None,
                injury=window[2] if window else False,
            ),
            start_to_close_timeout=_SLOW,
            retry_policy=_RETRY,
            result_type=contracts.MissedOutcome,
        )
        self._asked_about = overdue

        if outcome.nothing_to_do:
            # Logged while this was being scheduled. Not an error.
            self._stale = True
            return

        self._tier = outcome.tier
        self._decision_id = outcome.decision_id
        self._traceparent = outcome.traceparent

        if outcome.proposal_ids:
            self._pending = list(outcome.proposal_ids)
            self._state = "AWAITING_RESCHEDULE_APPROVAL"
            return

        # The policy ran and nothing survived validation -- a real answer
        # (ADR-0010 returns it rather than raising), but not one to re-ask
        # every time round the loop.
        self._error = outcome.failure
        self._quiet_until = workflow.now() + timedelta(hours=contracts.MISSED_RETRY_COOLDOWN_HOURS)

    async def _roll_over(self) -> None:
        """No sessions left: close the block and offer the next one."""
        finished = await workflow.execute_activity(
            contracts.FINISH_BLOCK,
            contracts.BlockRef(block_id=self._require_block(), as_of=workflow.now()),
            start_to_close_timeout=_SLOW,
            retry_policy=_RETRY,
            result_type=contracts.FinishedBlock,
        )
        nxt = await workflow.execute_activity(
            contracts.CREATE_NEXT_BLOCK,
            contracts.NextBlockRequest(
                block_id=self._require_block(),
                as_of=workflow.now(),
                one_rms=finished.new_one_rms,
            ),
            start_to_close_timeout=_SLOW,
            retry_policy=_RETRY,
            result_type=contracts.NextBlockOutcome,
        )
        self._pending = [nxt.proposal_id]
        self._decision_id = None
        self._traceparent = None
        self._state = "AWAITING_ROLLOVER_APPROVAL"

    # --- Small helpers ----------------------------------------------------

    async def _reload(self) -> None:
        state = await workflow.execute_activity(
            contracts.LOAD_BLOCK_STATE,
            contracts.BlockRef(block_id=self._require_block(), as_of=workflow.now()),
            start_to_close_timeout=_READ,
            retry_policy=_RETRY,
            result_type=contracts.WorkflowBlockState,
        )
        self._plan = state.sessions

    async def _continue_into(self, block_id: uuid.UUID) -> None:
        """Start again with a short history, same workflow id.

        Waiting for in-flight handlers first: an update that has been accepted
        but not answered would otherwise be lost, and its caller is a route
        holding a request open.
        """
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(args=[block_id])

    async def _sleep_until_woken(self, timeout: timedelta | None = None) -> None:
        try:
            await workflow.wait_condition(lambda: self._wake, timeout=timeout)
        except TimeoutError:
            return
        finally:
            self._wake = False

    def _planned(self) -> tuple[contracts.WorkflowSession, ...]:
        return tuple(
            sorted(
                (s for s in self._plan if s.status == "planned"),
                key=lambda s: (s.date, s.seq),
            )
        )

    def _next_planned(self) -> contracts.WorkflowSession | None:
        upcoming = self._planned()
        return upcoming[0] if upcoming else None

    def _overdue(self) -> tuple[int, ...]:
        """Planned sessions whose grace period has run out.

        The workflow's own view, from `workflow.now()` and the deadlines the
        activity computed -- so it replays identically. The activity derives
        its own list against the athlete's calendar date; this one exists
        only to tell "the same question again" from "a new one".
        """
        now = workflow.now()
        return tuple(sorted(s.seq for s in self._planned() if now >= s.deadline_utc))

    def _settle(self, proposal_id: uuid.UUID) -> None:
        self._pending = [p for p in self._pending if p != proposal_id]

    def _refresh(self) -> None:
        if not self._pending:
            self._tier = None
            self._state = "ACTIVE"
        self._stale = True
        self._wake = True

    def _require_block(self) -> uuid.UUID:
        if self._block_id is None:  # pragma: no cover - set before the loop runs
            raise RuntimeError("the workflow has no block")
        return self._block_id


def _reason(exc: ActivityError) -> str:
    """The message a human should see in `/blocks/{id}/state`."""
    cause = exc.cause
    return str(cause) if cause is not None else str(exc)


# --- The three that are not a block's state machine -----------------------
#
# ADR-0011, ADR-0012 and ADR-0014 each deferred a placement to spec 15, and
# all three are the same shape: slow work that was running on the API's own
# threadpool, where it competes with every request and dies with the
# process. None of them is a step in a block's life, so each is its own
# one-activity workflow rather than a branch of `BlockWorkflow`.
#
# They return `dict` deliberately. A `SimulationResult` cannot be named here
# -- `simulator` carries numpy and simpy -- and a pass-through has nothing
# to say about its payload anyway. The route rebuilds the model at the edge,
# which is where the type belongs.


@workflow.defn(name="SimulateWorkflow")
class SimulateWorkflow:
    """A what-if over the rest of a block. Writes nothing."""

    @workflow.run
    async def run(self, req: contracts.SimulateRequest) -> dict:
        return await workflow.execute_activity(
            contracts.SIMULATE_BLOCK,
            req,
            # The bound on a runaway solve. A sync activity cannot be
            # interrupted part-way, so there is nothing a heartbeat could
            # usefully report in between.
            start_to_close_timeout=timedelta(seconds=180),
            retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=_NEVER_RETRY),
            result_type=dict,
        )


@workflow.defn(name="ShapePreviewWorkflow")
class ShapePreviewWorkflow:
    """A block the template does not cover. Writes nothing."""

    @workflow.run
    async def run(self, req: contracts.ShapeRequest) -> dict:
        return await workflow.execute_activity(
            contracts.SHAPE_PREVIEW,
            req,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=_NEVER_RETRY),
            result_type=dict,
        )


@workflow.defn(name="IndexNoteWorkflow")
class IndexNoteWorkflow:
    """Embed a logged note, after the log itself is already safe.

    Retried more willingly than its neighbours: the athlete is not waiting,
    and a model that was down a minute ago may not be now.
    """

    @workflow.run
    async def run(self, req: contracts.IndexNoteRequest) -> contracts.IndexNoteOutcome:
        return await workflow.execute_activity(
            contracts.INDEX_NOTE,
            req,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=_NEVER_RETRY),
            result_type=contracts.IndexNoteOutcome,
        )
