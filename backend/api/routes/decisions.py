"""The decision trail, with a link to each turn's trace.

Spec 14 §3. Read-only: a decision is a record of something that already
happened, so there is nothing here to approve or undo.

**The join is what makes the athlete filter possible.** `decision` has no
`athlete_id` column and this pair of specs takes no migration (D1), so the
athlete and the input message come from the turn's own `chat_message` row --
`role = 'user'`, which is also what reduces a turn's two messages to one row.
`chat_message.decision_id` is already indexed.

A workflow decision (spec 15) has no chat message, so its `athlete_id` and
`message` are null and `?athlete_id=` does not match it. That is the intended
consequence rather than an oversight: reach those with `?source=workflow`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query

from api.deps import SessionDep
from api.schemas import DecisionOut
from domains.strength import enums, models
from planner_core.errors import NotFound

router = APIRouter(prefix="/decisions", tags=["decisions"])

# The turn's own question. `role = 'user'` is in the join condition rather than
# the WHERE clause so a decision with no chat message -- a workflow one --
# still comes back, with nulls.
_USER_MESSAGE = sa.outerjoin(
    models.Decision,
    models.ChatMessage,
    sa.and_(
        models.ChatMessage.decision_id == models.Decision.id,
        models.ChatMessage.role == "user",
    ),
)


@router.get("", response_model=list[DecisionOut])
def list_decisions(
    session: SessionDep,
    athlete_id: Annotated[uuid.UUID | None, Query()] = None,
    route: Annotated[enums.DecisionRoute | None, Query()] = None,
    outcome: Annotated[
        enums.DecisionOutcome | None,
        Query(description="Omit for every decision. A null outcome means a proposal is pending."),
    ] = None,
    source: Annotated[enums.ProposalSource | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DecisionOut]:
    """Decisions newest first, with a `trace_url` where tracing was on.

    `trace_url` is null when tracing is off, when the project id is unset, or
    for a turn taken before it was switched on -- all three are normal.
    """
    statement = (
        sa.select(models.Decision, models.ChatMessage.athlete_id, models.ChatMessage.content)
        .select_from(_USER_MESSAGE)
        .order_by(models.Decision.created_at.desc(), models.Decision.id)
        .limit(limit)
    )
    if athlete_id is not None:
        statement = statement.where(models.ChatMessage.athlete_id == athlete_id)
    if route is not None:
        statement = statement.where(models.Decision.route == route)
    if outcome is not None:
        statement = statement.where(models.Decision.outcome == outcome)
    if source is not None:
        statement = statement.where(models.Decision.source == source)

    return [
        DecisionOut.of(decision, athlete_id=joined_athlete, message=message)
        for decision, joined_athlete, message in session.execute(statement).all()
    ]


@router.get("/{decision_id}", response_model=DecisionOut)
def get_decision(decision_id: uuid.UUID, session: SessionDep) -> DecisionOut:
    """One decision, with its ordered tool calls and its trace link."""
    row = session.execute(
        sa.select(models.Decision, models.ChatMessage.athlete_id, models.ChatMessage.content)
        .select_from(_USER_MESSAGE)
        .where(models.Decision.id == decision_id)
    ).first()
    if row is None:
        raise NotFound("decision", decision_id)

    decision, athlete_id, message = row
    return DecisionOut.of(decision, athlete_id=athlete_id, message=message)
