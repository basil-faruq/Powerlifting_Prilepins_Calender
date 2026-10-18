"""The chat endpoint, and reading a conversation back.

Spec 13 §3. Thin, like every route here: `handle_message` does the work and
this owns the transaction.

Two things are load-bearing and neither is obvious.

**The commit happens before the 503.** `handle_message` catches
`LlmUnavailable` and hands it back on `ChatTurn.failure` instead of raising,
because a service may not commit (ADR-0007 rule 2) and an exception thrown
from inside it would roll back the `outcome = "failed"` row on its way to the
handler. Spec 13's DoD requires every turn to leave a decision row, so the
route commits first and re-raises after.

**`athlete_id` is required on the history route.**
`chat_message.conversation_id` is not a foreign key and there is no
conversation table (spec 02 §N), so any UUID is a syntactically valid
conversation id. Without the parameter, a guess reads back whatever that
conversation holds. Spec 13 does not mention it; single-athlete today is a
recorded decision, not a reason to leave the hole open.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import ChatModelsDep, EmbedderDep, SessionDep
from api.schemas import ChatCreate, ChatMessageOut, ChatResponse
from domains.strength import services
from domains.strength.agent import agent, conversation_messages

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatCreate,
    session: SessionDep,
    embedder: EmbedderDep,
    chat_models: ChatModelsDep,
) -> ChatResponse:
    """One turn: route it, run the tool, explain the result.

    The reply's numbers all came from a solver, the simulator, the generator or
    the database -- the explainer checks each one against the object it was
    given and falls back to a template built from that object if any is
    missing (CLAUDE.md non-negotiable 1).

    Any proposals come back `pending`. Approving is `POST
    /proposals/{id}/approve` and nothing else; the agent cannot do it.

    A 404 means the athlete or the conversation is unknown. A 503 means the
    language model is unreachable -- and the decision row recording that is
    already committed when it goes out.
    """
    turn = agent.handle_message(
        session,
        athlete_id=body.athlete_id,
        message=body.message,
        models_=chat_models,
        embedder=embedder,
        conversation_id=body.conversation_id,
        template_id=body.template_id,
    )
    session.commit()

    if turn.failure is not None:
        # Committed above, so the `outcome = "failed"` row survives the 503
        # that `api/main.py` turns this into.
        raise turn.failure

    proposals = [services.get_proposal(session, pid) for pid in turn.proposal_ids]
    return ChatResponse.of(turn, proposals)


@router.get("/conversations/{conversation_id}/messages", response_model=list[ChatMessageOut])
def conversation_history(
    conversation_id: uuid.UUID,
    session: SessionDep,
    athlete_id: Annotated[
        uuid.UUID, Query(description="Whose conversation this is. Required; see the module note.")
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ChatMessageOut]:
    """One conversation's messages, oldest first.

    Within a turn the question sorts before the reply even though both share a
    `created_at` -- Postgres' `now()` is the transaction start time, so the
    role breaks the tie.
    """
    services.get_athlete(session, athlete_id)
    rows = conversation_messages(session, conversation_id, athlete_id, limit=limit)
    return [ChatMessageOut.of(row) for row in rows]
