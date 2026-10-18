"""Previewing, proposing and reading a block.

The one thing no route here does is write a schedule. `POST /blocks` creates a
`draft` block with no sessions and a `pending` proposal; the sessions appear
when a human approves it (CLAUDE.md non-negotiable #2), which is
`POST /proposals/{id}/approve` in `proposals.py`.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Query, status
from starlette.concurrency import run_in_threadpool

from api import temporal_client
from api.deps import RunnerDep, SessionDep
from api.schemas import (
    BlockCreate,
    BlockCreated,
    BlockOut,
    BlockPreviewOut,
    BlockStateOut,
    PauseIn,
    PlannedLiftOut,
    ProposalOut,
    ResumeIn,
    SessionOut,
    ShapeCreate,
    ShapedPreviewOut,
    SimulateCreate,
    SimulationOut,
    UnavailableIn,
)
from domains.strength import services
from domains.strength.workflows import contracts
from planner_core.errors import BlockNotRunning, NotFound, TemporalUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(tags=["blocks"])


@router.post("/blocks/preview", response_model=BlockPreviewOut)
def preview_block(body: BlockCreate, session: SessionDep) -> BlockPreviewOut:
    """Generate and validate a block, writing nothing.

    Returns the verdict whether or not it passed: a preview whose violations
    are hidden behind a 422 would leave the athlete unable to see the plan that
    caused them. `POST /blocks` is the one that refuses.
    """
    draft, result = services.build_block_draft(
        session,
        body.athlete_id,
        body.template_id,
        body.lifts,
        body.start_date,
    )
    maxes = services.current_one_rms(session, body.athlete_id)
    return BlockPreviewOut(
        draft=draft,
        validation=result,
        has_synthetic_one_rms=any(
            row.is_synthetic for lift, row in maxes.items() if lift in draft.lifts
        ),
    )


@router.post("/blocks/shape-preview", response_model=ShapedPreviewOut)
async def shape_preview_block(body: ShapeCreate, runner: RunnerDep) -> ShapedPreviewOut:
    """Shape a block the template does not cover, and validate it.

    Writes nothing, like `/blocks/preview`. Unlike it, an infeasible request
    is a 422 rather than a verdict: there is no plan to show the violations
    of, because the solver never produced one -- and that 422 survives the
    trip through the activity, because the refusal is rebuilt on the way
    back (`planner_core.temporal_errors`).

    The CP-SAT solve runs in a Temporal activity now rather than on
    FastAPI's threadpool, which is what ADR-0011 deferred to this spec.
    """
    raw = await runner.shape_preview(
        contracts.ShapeRequest(
            athlete_id=body.athlete_id,
            template_id=body.template_id,
            lifts=tuple(body.lifts),
            start_date=body.start_date,
            weeks=body.weeks,
            test_week=dict(body.test_week) if body.test_week else None,
        )
    )
    return ShapedPreviewOut.model_validate(raw)


@router.post("/blocks", response_model=BlockCreated, status_code=status.HTTP_201_CREATED)
def create_block(body: BlockCreate, session: SessionDep) -> BlockCreated:
    """Propose a block. The block lands as `draft`, with no sessions."""
    proposal = services.create_block_proposal(
        session,
        body.athlete_id,
        body.template_id,
        body.lifts,
        body.start_date,
    )
    session.commit()
    session.refresh(proposal)
    return BlockCreated(block_id=proposal.block_id, proposal=ProposalOut.of(proposal))


@router.get("/blocks/{block_id}", response_model=BlockOut)
def read_block(block_id: uuid.UUID, session: SessionDep) -> BlockOut:
    """The block and its 1RM snapshot -- the maxes it was computed from."""
    return BlockOut.of(services.get_block(session, block_id))


@router.get("/athletes/{athlete_id}/blocks/current", response_model=BlockOut)
def current_block(athlete_id: uuid.UUID, session: SessionDep) -> BlockOut:
    """The block under way, or 404.

    `active` or `paused`: a paused block is still the current one, since it is
    what training resumes into. A `draft` is not -- it has no sessions and may
    never be approved.
    """
    services.get_athlete(session, athlete_id)
    block = services.running_block(session, athlete_id)
    if block is None:
        raise NotFound("current block", athlete_id)
    return BlockOut.of(block)


@router.post("/blocks/{block_id}/simulate", response_model=SimulationOut)
async def simulate_block(
    block_id: uuid.UUID, body: SimulateCreate, runner: RunnerDep
) -> SimulationOut:
    """Play out the rest of a running block and return ranges, not a forecast.

    Writes nothing, and so needs no proposal and no approval: it answers a
    question and changes no schedule. A block that is not `active` or `paused`
    is a 409, and a forced event the block cannot contain is a 422 -- both
    still, because the refusal is rebuilt after crossing the activity
    boundary.

    A thousand runs take several seconds, which is why ADR-0012 deferred the
    placement to this spec: on FastAPI's threadpool that time is taken from
    every other request, and a restart loses the work.
    """
    raw = await runner.simulate(
        contracts.SimulateRequest(
            block_id=block_id,
            scenario=tuple(body.scenario),
            runs=body.runs,
            seed=body.seed,
        )
    )
    return SimulationOut.model_validate(raw)


@router.get("/blocks/{block_id}/sessions", response_model=list[SessionOut])
def block_sessions(
    block_id: uuid.UUID,
    session: SessionDep,
    from_: dt.date | None = Query(default=None, alias="from"),
    to: dt.date | None = Query(default=None),
) -> list[SessionOut]:
    """The block's sessions, in order. Empty until the block is approved."""
    services.get_block(session, block_id)
    rows = services.sessions_of(session, block_id)
    return [
        SessionOut(
            id=row.id,
            block_id=row.block_id,
            week=row.week,
            slot=row.slot,
            seq=row.seq,
            date=row.date,
            original_date=row.original_date,
            status=row.status,
            lifts=[PlannedLiftOut.of(lift) for lift in services.lifts_of(session, row.id)],
        )
        for row in rows
        if (from_ is None or row.date >= from_) and (to is None or row.date <= to)
    ]


# --- The block's own state machine (spec 15) ------------------------------
#
# These four are the API's half of `BlockWorkflow`. They are `async def` with
# the blocking reads on a thread, which is the shape `health.py` already
# uses -- the workflow client is async and the services are not.


def _workflow_id(session, block_id: uuid.UUID) -> str | None:
    """The execution that owns this block, or `None` if it has never run.

    Reads the stored column rather than deriving the id: `continue_as_new`
    keeps the workflow id, so a block reached by rolling over belongs to an
    execution named after the first block of its chain.

    Blocking, hence `def` and a threadpool at the call sites -- the routes
    are async because the workflow client is, not because the database is.
    """
    block = services.get_block(session, block_id)
    return block.workflow_id


@router.get("/blocks/{block_id}/state", response_model=BlockStateOut)
async def block_state(block_id: uuid.UUID, session: SessionDep) -> BlockStateOut:
    """Where the plan has got to, straight from the workflow.

    A draft that was never approved answers 200 with `state = "none"`. It is
    an expected condition, not an error, and a 404 would make the UI branch
    on a failure to render "not started yet".
    """
    workflow_id = await run_in_threadpool(_workflow_id, session, block_id)
    if workflow_id is None:
        return BlockStateOut.none(block_id)
    view = await temporal_client.query(
        workflow_id, contracts.CURRENT_STATE, result_type=contracts.WorkflowStateView
    )
    return BlockStateOut.of(view)


@router.post(
    "/blocks/{block_id}/unavailable",
    response_model=BlockStateOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def report_unavailable(
    block_id: uuid.UUID, body: UnavailableIn, session: SessionDep
) -> BlockStateOut:
    """ "I cannot train between these dates." The workflow asks spec 08 what to do.

    202, not 200: the repair options are computed by the policy a moment
    later and arrive as pending proposals, so the answer here is "heard",
    not "done". Poll `/state`, or read `GET /proposals`.
    """
    return await _tell(
        session, block_id, contracts.REPORT_UNAVAILABLE, body.from_date, body.until, body.injury
    )


@router.post(
    "/blocks/{block_id}/pause", response_model=BlockStateOut, status_code=status.HTTP_202_ACCEPTED
)
async def pause_block(block_id: uuid.UUID, body: PauseIn, session: SessionDep) -> BlockStateOut:
    """Stop the clock. The block stays `paused`, which still counts as running."""
    return await _tell(session, block_id, contracts.PAUSE, body.reason)


@router.post(
    "/blocks/{block_id}/resume", response_model=BlockStateOut, status_code=status.HTTP_202_ACCEPTED
)
async def resume_block(block_id: uuid.UUID, body: ResumeIn, session: SessionDep) -> BlockStateOut:
    """Pick training back up.

    Whatever fell due while the block was paused is treated as an absence and
    goes through the one reschedule path, rather than a second mechanism with
    its own rules and its own bugs.
    """
    return await _tell(session, block_id, contracts.RESUME, body.on_date)


async def _tell(session, block_id: uuid.UUID, signal_name: str, *args) -> BlockStateOut:
    """Send a signal, then report where that left things.

    A block with no workflow is a 409 rather than a silent success: telling
    an execution that does not exist that you are ill achieves nothing, and
    the athlete should be told their block was never started.
    """
    workflow_id = await run_in_threadpool(_workflow_id, session, block_id)
    if workflow_id is None:
        raise BlockNotRunning(block_id, "not started")

    # The signal is the operation, and it is durable the moment the server
    # accepts it -- a worker does not have to be running for that.
    await temporal_client.signal(workflow_id, signal_name, *args)

    # The state is a convenience on top. Reading it back *does* need a live
    # worker, so a failure here must not be reported as a failure of the
    # signal: the athlete would be told their illness was not recorded when
    # it was, and would quite reasonably send it again.
    try:
        view = await temporal_client.query(
            workflow_id, contracts.CURRENT_STATE, result_type=contracts.WorkflowStateView
        )
    except TemporalUnavailable:
        logger.warning(
            "sent %s to %s but could not read the state back; is the worker running?",
            signal_name,
            workflow_id,
        )
        return BlockStateOut.unknown(block_id)
    return BlockStateOut.of(view)
