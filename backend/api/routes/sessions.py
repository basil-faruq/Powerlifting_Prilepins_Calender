"""Reading one session, and logging what happened in it."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from api import temporal_client
from api.deps import RunnerDep, SessionDep
from api.schemas import PlannedLiftOut, SessionDetail, SessionLogIn, SessionLogOut
from domains.strength import services
from domains.strength.workflows import contracts

logger = logging.getLogger(__name__)

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
async def log_session(
    session_id: uuid.UUID,
    body: SessionLogIn,
    session: SessionDep,
    runner: RunnerDep,
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
    log = await run_in_threadpool(
        services.log_session,
        session,
        session_id,
        body.performed_on,
        # `SessionLogIn.entries` is already `list[LoggedLift]`, the domain's own
        # shape for `session_log.entries`, so there is nothing to translate.
        body.entries,
        body.note,
        # Indexing happens after the commit now, in its own activity
        # (ADR-0014's deferral). Embedding a note took several seconds on
        # the request thread for something the athlete is not waiting for.
        embed=False,
    )
    session.commit()
    session.refresh(log)
    await _index_the_note(runner, log.id)
    await _wake_the_workflow(session, session_id)
    return SessionLogOut.of(log)


async def _index_the_note(runner, log_id: uuid.UUID) -> None:
    """Embed the note, if there is one. Best effort, like the signal below.

    The route still answers 201 when this fails, which is the invariant
    `log_session` has protected since spec 12: a training record must never
    be lost to a model being down. The note stays in `session_log.note` and
    `POST /athletes/{id}/notes/reindex` catches up -- that backlog is why
    ADR-0014 chose no queue table.
    """
    try:
        await runner.index_note(contracts.IndexNoteRequest(log_id=log_id))
    except Exception:  # noqa: BLE001 - a training record outranks its index
        logger.warning("could not index the note on log %s", log_id, exc_info=True)


async def _wake_the_workflow(session, session_id: uuid.UUID) -> None:
    """Tell the block's workflow to look again. Best effort, deliberately.

    The log is already committed, so a failure here cannot be allowed to
    fail the request -- answering 503 after the row exists would be a lie,
    and spec 15 §13's "503 on approve / log routes" is wrong about this half
    for the same reason `log_session` refuses to lose a note to a down
    embedder (ADR-0014).

    Nothing is lost if the signal never arrives. It carries no information
    the database does not already have, and the workflow's own deadline
    timer reloads state and sees `done` either way. That is what makes
    "commit first, signal after" safe rather than merely convenient.
    """
    try:
        planned = await run_in_threadpool(services.get_session_row, session, session_id)
        block = await run_in_threadpool(services.get_block, session, planned.block_id)
        if block.workflow_id is None:
            return
        await temporal_client.signal(block.workflow_id, contracts.SESSION_LOGGED, session_id)
    except Exception:  # noqa: BLE001 - a training record outranks a notification
        logger.warning(
            "could not tell the workflow that session %s was logged", session_id, exc_info=True
        )
