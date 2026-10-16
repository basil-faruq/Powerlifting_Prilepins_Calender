"""Previewing, proposing and reading a block.

The one thing no route here does is write a schedule. `POST /blocks` creates a
`draft` block with no sessions and a `pending` proposal; the sessions appear
when a human approves it (CLAUDE.md non-negotiable #2), which is
`POST /proposals/{id}/approve` in `proposals.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Query, status

from api.deps import SessionDep
from api.schemas import (
    BlockCreate,
    BlockCreated,
    BlockOut,
    BlockPreviewOut,
    PlannedLiftOut,
    ProposalOut,
    SessionOut,
    ShapeCreate,
    ShapedPreviewOut,
    SimulateCreate,
    SimulationOut,
)
from domains.strength import services
from planner_core.errors import NotFound

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
def shape_preview_block(body: ShapeCreate, session: SessionDep) -> ShapedPreviewOut:
    """Shape a block the template does not cover, and validate it.

    Writes nothing, like `/blocks/preview`. Unlike it, an infeasible request
    is a 422 rather than a verdict: there is no plan to show the violations
    of, because the solver never produced one.

    Runs CP-SAT on FastAPI's threadpool, which a plain `def` route gets for
    free. That is fine for one athlete locally; under spec 15 the same call
    belongs in a Temporal activity.
    """
    draft, result, shaped = services.build_shaped_draft(
        session,
        body.athlete_id,
        body.template_id,
        body.lifts,
        body.start_date,
        weeks=body.weeks,
        test_week=body.test_week,
    )
    maxes = services.current_one_rms(session, body.athlete_id)
    return ShapedPreviewOut(
        draft=draft,
        validation=result,
        has_synthetic_one_rms=any(
            row.is_synthetic for lift, row in maxes.items() if lift in draft.lifts
        ),
        weeks=shaped.weeks,
        test_week=shaped.test_week,
        overreach_from_week=shaped.overreach_from_week,
        cost=shaped.cost,
        stats=shaped.stats,
    )


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
def simulate_block(block_id: uuid.UUID, body: SimulateCreate, session: SessionDep) -> SimulationOut:
    """Play out the rest of a running block and return ranges, not a forecast.

    Writes nothing, and so needs no proposal and no approval: it answers a
    question and changes no schedule. A block that is not `active` or `paused`
    is a 409, and a forced event the block cannot contain is a 422.

    Runs the Monte-Carlo loop on FastAPI's threadpool, which a plain `def`
    route gets for free. A thousand runs take several seconds; fine for one
    athlete locally, and under spec 15 the same call belongs in a Temporal
    activity.
    """
    result, synthetic = services.simulate_block(
        session, block_id, scenario=body.scenario, runs=body.runs, seed=body.seed
    )
    return SimulationOut(
        **{name: getattr(result, name) for name in type(result).model_fields},
        block_id=block_id,
        has_synthetic_one_rms=synthetic,
    )


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
