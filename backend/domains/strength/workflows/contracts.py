"""What the workflow and its activities say to each other. Nothing else.

This module exists to be **importable from inside Temporal's workflow
sandbox**, and that is a stronger constraint than it sounds. A workflow must
replay deterministically, so `block_workflow.py` may not reach SQLAlchemy,
CP-SAT, numpy, simpy or pydantic-ai -- not directly and not through anything it
imports. Importing `activities.py` would pull in `services`, and `services`
pulls in all of them.

So the workflow names its activities by string and speaks to them only in the
types below. Those strings cost the type checker its grip on the call sites,
which is bought back by `tests/test_block_workflow.py`: one test asserts every
constant here is a registered activity, another that every registered activity
has a constant. A purity test walks this file's imports.

Only two project modules are safe to import here, and only because each is
itself nothing but `Literal`s and Pydantic models: `enums` and `schemas`.
Check before adding a third.

Everything crossing the boundary is a frozen Pydantic model, carried by
`temporalio.contrib.pydantic.pydantic_data_converter`. That converter is not
optional: without it a `Decimal` becomes a float somewhere over the wire and
CLAUDE.md non-negotiable #4 -- exact arithmetic end to end -- is broken in the
one place nobody looks.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domains.strength import enums
from domains.strength.schemas import SessionChange

# --- Activity names -------------------------------------------------------
# The workflow calls activities by these strings rather than by importing the
# functions. One constant per activity, and a test each way.

BIND_WORKFLOW = "bind_workflow"
LOAD_BLOCK_STATE = "load_block_state"
COMMIT_PROPOSAL = "commit_proposal"
REJECT_PROPOSAL = "reject_proposal"
APPLY_EDIT = "apply_edit"
RUN_MISSED_POLICY = "run_missed_policy"
FINISH_BLOCK = "finish_block"
CREATE_NEXT_BLOCK = "create_next_block"
SET_BLOCK_STATUS = "set_block_status"

ACTIVITY_NAMES: tuple[str, ...] = (
    BIND_WORKFLOW,
    LOAD_BLOCK_STATE,
    COMMIT_PROPOSAL,
    REJECT_PROPOSAL,
    APPLY_EDIT,
    RUN_MISSED_POLICY,
    FINISH_BLOCK,
    CREATE_NEXT_BLOCK,
    SET_BLOCK_STATUS,
)

# --- Signal, update and query names --------------------------------------
# `approve`, `reject` and `edit` are **updates**, not signals: their routes
# have to answer with the decided `ProposalOut`, and a signal cannot tell the
# caller what happened. The other four are genuinely fire-and-forget -- the
# database is already written and the workflow is only being told to look.

APPROVE = "approve"
REJECT = "reject"
EDIT = "edit"
SESSION_LOGGED = "session_logged"
REPORT_UNAVAILABLE = "report_unavailable"
PAUSE = "pause"
RESUME = "resume"
CURRENT_STATE = "current_state"
NEXT_SESSION = "next_session"

# --- Tunables -------------------------------------------------------------

# How long after a session's own day the workflow waits before treating it as
# not done. A **starting value**, not a derived one (CLAUDE.md): it says "a
# late evening session still counts, tomorrow lunchtime does not", and the
# right number is whatever stops the athlete being nagged. The day boundary is
# the *athlete's*, not the server's -- see `WorkflowSession.deadline_utc`.
GRACE_HOURS = 12

# After the athlete rejects every repair option, how long before the policy is
# asked again about the same sessions. Also a **starting value**. Without it
# the loop is unbounded: a rejected session is still `planned` with a date in
# the past, so the next pass would propose again immediately, and again,
# filling the workflow history and the decision log with the same question.
MISSED_RETRY_COOLDOWN_HOURS = 24


def workflow_id_for(block_id: uuid.UUID) -> str:
    """The id a block's workflow is *started* under.

    Only the first block of a chain is named this way. `continue_as_new` keeps
    the workflow id, so a run that began as `block-A` and rolled into B is
    still `block-A` -- and B's `block.workflow_id` column says so. Read the
    column when it is set; this is the fallback for a block never started.
    """
    return f"block-{block_id}"


# --- State ----------------------------------------------------------------

WorkflowState = Literal[
    "ACTIVE",
    "AWAITING_RESCHEDULE_APPROVAL",
    "AWAITING_ROLLOVER_APPROVAL",
    "PAUSED",
    "ERROR",
    "DONE",
]


class Contract(BaseModel):
    """Frozen, closed, and the same on both sides of the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Requests -------------------------------------------------------------


class BindRequest(Contract):
    block_id: uuid.UUID
    workflow_id: str


class BlockRef(Contract):
    """Any activity that only needs to know which block, and when.

    `as_of` is always `workflow.now()`. It is how the athlete's own "today"
    reaches an activity without the activity reading a clock: a retry computes
    the same date as the first attempt, and a time-skipped test gets the date
    it skipped to rather than the real one.
    """

    block_id: uuid.UUID
    as_of: dt.datetime


class SetStatusRequest(Contract):
    block_id: uuid.UUID
    status: enums.BlockStatus


class DecideRequest(Contract):
    """Approve or reject one proposal.

    `decision_id` and `parent_traceparent` are the thread back to the chat turn
    or the missed-session run that produced this proposal, so the `approval`
    span lands inside that trace instead of starting a lonely new one. Both are
    absent when the proposal came from somewhere the workflow did not see.
    """

    proposal_id: uuid.UUID
    as_of: dt.datetime
    decision_id: uuid.UUID | None = None
    parent_traceparent: str | None = None
    reason: str | None = None


class EditRequest(Contract):
    proposal_id: uuid.UUID
    as_of: dt.datetime
    changes: tuple[SessionChange, ...] = Field(min_length=1)


class MissedRequest(Contract):
    """Ask spec 08 what to do about sessions that did not happen.

    `missed_seqs` empty means "work it out": the activity derives them as the
    sessions still `planned` with a date in the past. The workflow does not
    compute that itself, because deciding what counts as missed reads the
    database.

    `unavailable_from` empty means the same -- the earliest missed session's
    own date. Both are filled in when the athlete said so explicitly, through
    `report_unavailable` or a pause.
    """

    block_id: uuid.UUID
    as_of: dt.datetime
    run_id: str
    missed_seqs: tuple[int, ...] = ()
    unavailable_from: dt.date | None = None
    unavailable_until: dt.date | None = None
    injury: bool = False


class NextBlockRequest(Contract):
    block_id: uuid.UUID
    as_of: dt.datetime
    one_rms: dict[str, Decimal]


# --- Results --------------------------------------------------------------


class WorkflowSession(Contract):
    """One session, as the workflow needs to see it.

    Deliberately thin. The workflow never reads a weight or a rep count; it
    only needs to know which session is next and when to stop waiting for it.
    """

    id: uuid.UUID
    seq: int = Field(ge=1)
    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    date: dt.date
    status: enums.PlannedSessionStatus
    # Midnight after `date` in the athlete's timezone, plus `GRACE_HOURS`,
    # converted to UTC. Computed in the activity from stored data alone, so it
    # is identical on every replay -- and so `zoneinfo` never has to exist
    # inside the sandbox.
    deadline_utc: dt.datetime


class WorkflowBlockState(Contract):
    block_id: uuid.UUID
    athlete_id: uuid.UUID
    status: enums.BlockStatus
    start_date: dt.date
    sessions: tuple[WorkflowSession, ...]

    @property
    def planned(self) -> tuple[WorkflowSession, ...]:
        """Still ahead, in the order they should happen.

        A session whose day has passed unlogged is in here, with a date in the
        past. That is the point: `missed` means written off (ADR-0010), so a
        session the athlete may still make up stays `planned` and the calendar
        solver is allowed to re-place it.
        """
        return tuple(
            sorted(
                (s for s in self.sessions if s.status == "planned"),
                key=lambda s: (s.date, s.seq),
            )
        )


class CommitOutcome(Contract):
    """What became of a decision the workflow was asked to take.

    `status = "ignored"` is not a failure. A proposal the workflow has never
    heard of may still be a real one -- the athlete can approve a reschedule
    from chat while the workflow sits waiting on a session -- so the activity
    is the thing that judges, and an id that turns out to be unknown or already
    decided leaves the workflow running and unchanged (spec 15 §13).
    """

    proposal_id: uuid.UUID
    status: Literal["approved", "rejected", "edited", "ignored"]
    kind: enums.ProposalKind | None = None
    block_id: uuid.UUID | None = None
    supersedes_block_id: uuid.UUID | None = None
    detail: str | None = None


class MissedOutcome(Contract):
    """The repair options now waiting for the athlete, if any.

    Three outcomes, not two. `nothing_to_do` means the question was stale --
    the session was logged while the activity was being scheduled. A `failure`
    means the policy ran and nothing survived validation, which is a real
    answer the athlete should see (ADR-0010 returns it rather than raising).
    """

    nothing_to_do: bool = False
    tier: enums.MissedTier | None = None
    proposal_ids: tuple[uuid.UUID, ...] = ()
    missed_seqs: tuple[int, ...] = ()
    decision_id: uuid.UUID | None = None
    traceparent: str | None = None
    failure: str | None = None


class FinishedBlock(Contract):
    """The block is over, and these are the maxes the next one starts from.

    `written` names only the lifts this activity inserted a row for. A tested
    single was already banked by `log_session` at the moment it was logged
    (ADR-0005), so writing it again here would double it; an unchanged max is
    not written at all. `already_done` says the activity had run before and
    this attempt wrote nothing.
    """

    block_id: uuid.UUID
    new_one_rms: dict[str, Decimal]
    written: tuple[str, ...] = ()
    already_done: bool = False


class NextBlockOutcome(Contract):
    next_block_id: uuid.UUID
    proposal_id: uuid.UUID


# --- Queries --------------------------------------------------------------


class WorkflowStateView(Contract):
    """The answer to `GET /blocks/{id}/state`.

    `state = "none"` is for a block whose workflow was never started -- a draft
    awaiting its first approval. That is a 200, not a 404: it is a real and
    expected condition, and making the UI branch on an error to render "not
    started yet" would be a poor trade.
    """

    state: WorkflowState | Literal["none"]
    block_id: uuid.UUID
    next_session: WorkflowSession | None = None
    pending_proposal_ids: tuple[uuid.UUID, ...] = ()
    tier: enums.MissedTier | None = None
    error: str | None = None
