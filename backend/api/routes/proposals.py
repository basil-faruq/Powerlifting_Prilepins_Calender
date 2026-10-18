"""The approval gate.

`POST /proposals/{id}/approve` is still the only path in this API that writes
a schedule (CLAUDE.md non-negotiable #2). What changed in spec 15 is where
the writing happens: the route no longer calls `approve_proposal` itself, it
asks the block's workflow to, so that the plan's state machine lives in one
place and survives a restart.

The service function is untouched -- ADR-0007's Action Required said re-host,
not reimplement -- and the status codes are untouched too: a stale proposal
is still a 409, an invalid one still a 422, because `temporal_client` rebuilds
the domain error on the way back out of the activity.

Three ways an approval can be routed, and the middle one is the interesting
case:

* a `new_block` with no workflow yet **starts** one, which then commits the
  proposal as its first act;
* a `new_block` carrying `supersedes_block_id` -- spec 08's restart -- is
  approved against the workflow of the block it replaces, which retires that
  block and continues as new into the replacement. Starting a second
  execution would strand the first one waiting for an answer that never came;
* everything else goes to its own block's workflow.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from api import temporal_client
from api.deps import SessionDep
from api.schemas import ProposalEdit, ProposalOut, ProposalReject
from domains.strength import enums, models, services
from domains.strength.workflows import contracts
from planner_core.errors import AlreadyDecided

router = APIRouter(prefix="/proposals", tags=["proposals"])


@router.get("", response_model=list[ProposalOut])
def list_proposals(
    session: SessionDep,
    athlete_id: uuid.UUID | None = Query(default=None),
    status: enums.ProposalStatus | None = Query(default="pending"),
) -> list[ProposalOut]:
    """Proposals, newest first.

    Filtered by athlete through the block, since `proposal` has no
    `athlete_id`: the block it changes is what ties it to a person.
    """
    query = sa.select(models.Proposal)
    if athlete_id is not None:
        query = query.join(models.Block, models.Block.id == models.Proposal.block_id).where(
            models.Block.athlete_id == athlete_id
        )
    if status is not None:
        query = query.where(models.Proposal.status == status)
    rows = session.scalars(query.order_by(models.Proposal.created_at.desc()))
    return [ProposalOut.of(row) for row in rows]


@router.get("/{proposal_id}", response_model=ProposalOut)
def read_proposal(proposal_id: uuid.UUID, session: SessionDep) -> ProposalOut:
    return ProposalOut.of(services.get_proposal(session, proposal_id))


@router.post("/{proposal_id}/approve", response_model=ProposalOut)
async def approve(proposal_id: uuid.UUID, session: SessionDep) -> ProposalOut:
    """Accept the proposal and write its plan, through the block's workflow."""
    target = await run_in_threadpool(_resolve, session, proposal_id)

    if target.starts_a_workflow:
        await temporal_client.start_block_workflow(target.block_id, first_proposal_id=proposal_id)
    else:
        await temporal_client.update(
            target.workflow_id,
            contracts.APPROVE,
            proposal_id,
            result_type=contracts.CommitOutcome,
        )
    return await run_in_threadpool(_read_back, session, proposal_id)


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
async def reject(proposal_id: uuid.UUID, body: ProposalReject, session: SessionDep) -> ProposalOut:
    """Decline the proposal.

    A `new_block` nobody has started a workflow for is rejected directly:
    there is no execution to tell, and spec §3 says as much. Everything else
    goes through the workflow, which needs to know its options are gone.
    """
    target = await run_in_threadpool(_resolve, session, proposal_id)

    if target.starts_a_workflow:
        return await run_in_threadpool(_reject_directly, session, proposal_id, body.reason)
    await temporal_client.update(
        target.workflow_id,
        contracts.REJECT,
        proposal_id,
        body.reason,
        result_type=contracts.CommitOutcome,
    )
    return await run_in_threadpool(_read_back, session, proposal_id)


@router.patch("/{proposal_id}", response_model=ProposalOut)
async def edit(proposal_id: uuid.UUID, body: ProposalEdit, session: SessionDep) -> ProposalOut:
    """Amend a pending proposal by hand, and re-validate it.

    Still `pending` afterwards: an edit is a counter-offer, not an approval.
    """
    target = await run_in_threadpool(_resolve, session, proposal_id)

    if target.starts_a_workflow:
        return await run_in_threadpool(_edit_directly, session, proposal_id, body.changes)
    await temporal_client.update(
        target.workflow_id,
        contracts.EDIT,
        proposal_id,
        tuple(body.changes),
        result_type=contracts.CommitOutcome,
    )
    return await run_in_threadpool(_read_back, session, proposal_id)


# --- Working out where a decision should go -------------------------------


class _Target:
    """Which execution owns this decision, if any.

    `starts_a_workflow` means there is nothing running to tell -- a `draft`
    block awaiting its first approval. Approving it starts the execution;
    rejecting or editing it is a plain database write, because an execution
    that was never started cannot be sent anything.
    """

    __slots__ = ("block_id", "starts_a_workflow", "workflow_id")

    def __init__(self, block_id: uuid.UUID, workflow_id: str, starts_a_workflow: bool) -> None:
        self.block_id = block_id
        self.workflow_id = workflow_id
        self.starts_a_workflow = starts_a_workflow


def _resolve(session, proposal_id: uuid.UUID) -> _Target:
    """Read the proposal and decide which workflow should hear about it.

    Blocking, so the caller puts it on a thread. It raises `NotFound` exactly
    as before, which `api/main.py` still turns into a 404.
    """
    proposal = services.get_proposal(session, proposal_id)
    if proposal.status != "pending":
        # The route is strict where the workflow is lenient, and both are
        # right. A human clicking approve on a page that went stale should
        # be told (409, spec 06 §12); a workflow handed an id it does not
        # recognise should shrug and keep running (spec 15 §13), because the
        # alternative is stranding eleven weeks of training over a stray
        # message.
        raise AlreadyDecided(proposal.status)
    block = services.get_block(session, proposal.block_id)
    payload = services.read_payload(proposal)
    superseded_id = getattr(payload, "supersedes_block_id", None)

    if superseded_id is not None:
        # Spec 08's restart. The execution that must hear about this is the
        # one running the block being replaced: it retires that block and
        # continues as new into this one. Telling the replacement's own
        # workflow is impossible -- it does not have one yet -- and starting
        # it would leave the old execution waiting forever.
        superseded = services.get_block(session, superseded_id)
        return _Target(
            block_id=block.id,
            workflow_id=temporal_client.workflow_id_for(superseded.id, superseded.workflow_id),
            starts_a_workflow=False,
        )

    if block.workflow_id is None:
        return _Target(
            block_id=block.id,
            workflow_id=temporal_client.workflow_id_for(block.id),
            starts_a_workflow=True,
        )
    return _Target(
        block_id=block.id,
        workflow_id=block.workflow_id,
        starts_a_workflow=False,
    )


def _read_back(session, proposal_id: uuid.UUID) -> ProposalOut:
    """What the proposal became, read fresh.

    The activity wrote it on its own connection and its own transaction, so
    this session's identity map is a version behind.
    """
    session.expire_all()
    return ProposalOut.of(services.get_proposal(session, proposal_id))


def _reject_directly(session, proposal_id: uuid.UUID, reason: str | None) -> ProposalOut:
    proposal = services.reject_proposal(session, proposal_id, reason)
    session.commit()
    session.refresh(proposal)
    return ProposalOut.of(proposal)


def _edit_directly(session, proposal_id: uuid.UUID, changes) -> ProposalOut:
    proposal = services.edit_proposal(session, proposal_id, changes)
    session.commit()
    session.refresh(proposal)
    return ProposalOut.of(proposal)
