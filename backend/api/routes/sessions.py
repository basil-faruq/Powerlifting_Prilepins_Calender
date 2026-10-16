"""Reading one session, and logging what happened in it."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from api.deps import EmbedderDep, SessionDep
from api.schemas import PlannedLiftOut, SessionDetail, SessionLogIn, SessionLogOut
from domains.strength import services

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/{session_id}", response_model=SessionDetail)
def read_session(session_id: uuid.UUID, session: SessionDep) -> SessionDetail:
    row = services.get_session_row(session, session_id)
    log = services.log_of(session, session_id)
    return SessionDetail(
        id=row.id,
        block_id=row.block_id,
        week=row.week,
        slot=row.slot,
        seq=row.seq,
        date=row.date,
        original_date=row.original_date,
        status=row.status,
        lifts=[PlannedLiftOut.of(lift) for lift in services.lifts_of(session, session_id)],
        log=SessionLogOut.of(log) if log is not None else None,
    )


@router.post("/{session_id}/log", response_model=SessionLogOut, status_code=status.HTTP_201_CREATED)
def log_session(
    session_id: uuid.UUID,
    body: SessionLogIn,
    session: SessionDep,
    embedder: EmbedderDep,
) -> SessionLogOut:
    """Record what happened, and bank a made test single as a new 1RM.

    The status change to `done` is not a plan change, so it does not go through
    the approval gate: spec 06 §11's rule is about *dates and skips*, the
    things a solver proposes. What the athlete did is a fact they are
    reporting, not a proposal to be approved.

    A second log for the same session is a 409 -- `session_log` is one row per
    session, and overwriting history silently is worse than refusing.

    A note on the log is indexed for retrieval, but this route still answers
    201 if that fails: the log is the athlete's record and the embedding is
    built on top of it. `POST /athletes/{id}/notes/reindex` catches up.
    """
    log = services.log_session(
        session,
        session_id,
        body.performed_on,
        # `SessionLogIn.entries` is already `list[LoggedLift]`, the domain's own
        # shape for `session_log.entries`, so there is nothing to translate.
        body.entries,
        body.note,
        embedder=embedder,
    )
    session.commit()
    session.refresh(log)
    return SessionLogOut.of(log)
