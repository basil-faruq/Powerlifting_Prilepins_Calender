"""Searching the athlete's training notes, and re-embedding them.

Retrieval is a read: it changes no plan, so it needs no proposal and no
approval gate. The reindex is a write, but only of vectors derived from text
that is already stored -- it cannot change a date, a weight or a status, which
is why it is a plain POST rather than something `approve_proposal` has to
mediate (ADR-0007 rule 2 governs the *schedule*, and this touches none of it).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import StringConstraints

from api.deps import EmbedderDep, SessionDep
from api.schemas import NoteHitOut, ReindexOut
from domains.strength import enums, notes, services

router = APIRouter(prefix="/athletes", tags=["notes"])

# Stripped *before* the length check, so "   " is as empty as "" and both come
# back as FastAPI's own 422 rather than reaching the service and raising.
#
# This must be used as `Annotated[SearchQuery, Query(...)]`, never as
# `q: SearchQuery = Query(...)`. The second form silently drops the constraint
# entirely -- a blank query then sails past validation into the service, which
# raises, which is a 500. Verified both ways; the difference is invisible at
# the call site, which is why it is written down here.
SearchQuery = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@router.get("/{athlete_id}/notes/search", response_model=list[NoteHitOut])
def search_notes(
    athlete_id: uuid.UUID,
    session: SessionDep,
    embedder: EmbedderDep,
    q: Annotated[SearchQuery, Query(description="What to look for, in the athlete's own words.")],
    lift: Annotated[enums.LiftCode | None, Query()] = None,
    since: Annotated[dt.date | None, Query()] = None,
    k: Annotated[int, Query(ge=1, le=notes.MAX_K)] = notes.DEFAULT_K,
    include_synthetic: Annotated[
        bool,
        Query(description="Include seeded demo notes. They are flagged `is_synthetic`."),
    ] = False,
) -> list[NoteHitOut]:
    """The notes closest in meaning to `q`, nearest first.

    The constraint on `q` and the bound on `k` are what make spec 12 §13's two
    422s come out of FastAPI's own validation rather than a hand-rolled check.
    `q` is stripped first, so a query of spaces is refused the same way an
    empty one is -- it reached the service and became a 500 until it was not.

    May return fewer than `k` results -- see `notes.search_notes`. A 503 means
    Ollama could not embed the query, not that there are no matching notes.
    """
    # Checked so an unknown athlete is a 404 rather than an empty list, which
    # would read as "no notes" and send the caller looking in the wrong place.
    services.get_athlete(session, athlete_id)
    hits = notes.search_notes(
        session,
        athlete_id,
        q,
        embedder,
        lift=lift,
        since=since,
        k=k,
        include_synthetic=include_synthetic,
    )
    return [NoteHitOut.of(hit) for hit in hits]


@router.post("/{athlete_id}/notes/reindex", response_model=ReindexOut)
def reindex_notes(
    athlete_id: uuid.UUID,
    session: SessionDep,
    embedder: EmbedderDep,
) -> ReindexOut:
    """Re-embed every note this athlete has, and say how many chunks there are.

    Two jobs, one path. It is the catch-up for a note whose embedding failed
    while Ollama was down -- `session_log.note` is the source of truth, so no
    queue table is needed -- and it is the required step after changing the
    embedding model, because vectors from two different models are not
    comparable and a half-migrated table returns confident nonsense.

    Idempotent: chunks are deleted and rebuilt per log, so running it twice
    leaves the same rows rather than duplicating them.
    """
    services.get_athlete(session, athlete_id)
    count = notes.reindex_notes(session, athlete_id, embedder)
    session.commit()
    return ReindexOut(reindexed=count)
