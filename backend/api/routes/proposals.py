"""The approval gate.

`POST /proposals/{id}/approve` is the only path in this API that writes a
schedule (CLAUDE.md non-negotiable #2). Everything else here reads proposals,
declines them, or amends one before the human decides.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Query

from api.deps import SessionDep
from api.schemas import ProposalEdit, ProposalOut, ProposalReject
from domains.strength import enums, models, services

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
def approve(proposal_id: uuid.UUID, session: SessionDep) -> ProposalOut:
    """Accept the proposal and write its plan.

    Spec 15 replaces this body with a Temporal signal; the service function it
    calls stays exactly as it is, which is why it takes a session rather than
    opening one.
    """
    proposal = services.approve_proposal(session, proposal_id)
    session.commit()
    session.refresh(proposal)
    return ProposalOut.of(proposal)


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
def reject(proposal_id: uuid.UUID, body: ProposalReject, session: SessionDep) -> ProposalOut:
    proposal = services.reject_proposal(session, proposal_id, body.reason)
    session.commit()
    session.refresh(proposal)
    return ProposalOut.of(proposal)


@router.patch("/{proposal_id}", response_model=ProposalOut)
def edit(proposal_id: uuid.UUID, body: ProposalEdit, session: SessionDep) -> ProposalOut:
    """Amend a pending proposal by hand, and re-validate it.

    Still `pending` afterwards: an edit is a counter-offer, not an approval.
    """
    proposal = services.edit_proposal(session, proposal_id, body.changes)
    session.commit()
    session.refresh(proposal)
    return ProposalOut.of(proposal)
