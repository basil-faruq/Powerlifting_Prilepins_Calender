"""Training notes: retrieved by meaning, screened for pain by keyword.

Two jobs that deliberately do not share a mechanism.

**Retrieval** is the RAG half. A note is chunked, embedded locally and stored in
`note_chunk`; `search_notes` finds it by cosine distance, so "how has my knee
been?" reaches "left knee felt tight at the bottom of squats" without the word
"tight" appearing in the query.

**Screening** is `pain_flags`, and it is deterministic keyword matching with no
embedding anywhere near it. That is CLAUDE.md non-negotiable 3: the hard
filters run before any model is consulted, and a safety rule whose answer
depends on cosine similarity is a safety rule that can be argued with. A
threshold that happens to be 0.31 today is not a reason to let the athlete
squat heavy on a knee that hurt on Tuesday.

The note text itself is the athlete's data. It reaches a prompt as quoted
content and never as instruction (CLAUDE.md), and nothing in this module
interprets it as a command.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from sqlmodel import Session, select

from domains.strength import enums, models
from domains.strength.schemas import NoteHit
from planner_core.llm import Embedder

logger = logging.getLogger(__name__)

# --- Starting values, not derived ----------------------------------------
# Every number below is a guess that has not been measured against anything.
# CLAUDE.md requires them labelled as such in code and UI rather than presented
# as tuned. Changing one changes retrieval quality and nothing about safety.

# A note at or under this length is one chunk. Most are a single sentence.
SINGLE_CHUNK_MAX = 600
# Longer notes are grouped into chunks of about this many characters.
CHUNK_TARGET = 400
# How many days back `pain_flags` looks. Spec 12 §5E says 7; nothing measured it.
PAIN_WINDOW_DAYS = 7
# Default and ceiling for `k`. The ceiling exists so one query cannot ask for
# the whole table.
DEFAULT_K = 5
MAX_K = 20
# How many tokens before a pain word are checked for a negation.
NEGATION_LOOKBACK = 2

# Substrings rather than whole words, so "painful", "hurts", "injured" and
# "tweaked" all match without listing every inflection.
PAIN_STEMS = ("pain", "hurt", "injur", "tweak", "strain", "sharp", "pinch", "twinge")

# A quantifier right after a negation means the pain was *reduced*, not absent:
# "not much pain" is a report of some pain and must still flag the lift.
NEGATION_HEDGES = frozenset({"much", "many", "lot", "lots", "loads"})

# "no pain today" must not read as a pain note. Apostrophes are kept by the
# tokenizer so "didn't" is one token and matches here.
NEGATIONS = frozenset(
    {
        "no",
        "not",
        "never",
        "none",
        "nothing",
        "without",
        "didnt",
        "didn't",
        "dont",
        "don't",
        "doesnt",
        "doesn't",
        "isnt",
        "isn't",
        "wasnt",
        "wasn't",
        "zero",
    }
)

# Word-boundary anchored, so "pullover" is not a deadlift and "impressive" is
# not a press. `dl` needs the boundaries most: it is two letters.
#
# `pull` keeps only its plural. "pulled" and "pulling" were dropped because
# "pulled a muscle during squats" is a pain note about a *squat*, and reading it
# as a deadlift flagged the wrong lift -- the one failure mode this module
# cannot afford. "Pulled 180 today" is the cost, and it is a cheap one: a pain
# note on a deadlift day still picks deadlift up from the session's own lifts.
LIFT_PATTERNS: dict[str, re.Pattern[str]] = {
    "squat": re.compile(r"\bsquat", re.I),
    "bench": re.compile(r"\bbench", re.I),
    "deadlift": re.compile(r"\bdead\s?lift|\bdl\b|\bpulls?\b", re.I),
    "press": re.compile(r"\bpress|\bohp\b|\boverhead\b", re.I),
    "row": re.compile(r"\brow(s|ed|ing)?\b", re.I),
}

# "bench press" is the ordinary English name of the bench lift, and `\bpress`
# matches inside it -- so "sharp pain in my shoulder on bench press" used to
# flag the *overhead* press as well, blocking every press session in the block
# over an injury that happened on the bench. The phrase is folded down to
# "bench" before the patterns run.
_BENCH_PRESS = re.compile(r"\bbench\s*press(es|ed|ing)?\b", re.I)

_TOKEN = re.compile(r"[a-z']+")
# Split after . ! or ? followed by whitespace. Deliberately simple: a note is a
# line or two typed on a phone, not prose with abbreviations in it.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A negation may not reach across one of these. See `is_pain_text`.
_CLAUSE_END = re.compile(r"[,;:.!?]+")


# --- Chunking and lift detection (pure) ----------------------------------


def chunk_note(text: str) -> list[str]:
    """Split a note into the pieces that get embedded, in order.

    One chunk is the common case and the one worth optimising for: a note is
    usually a sentence. Splitting a long one matters because a single vector
    averages everything in it -- a note covering sleep, a sore knee and a PR
    retrieves badly for all three at once.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= SINGLE_CHUNK_MAX:
        return [text]

    sentences = [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]
    if not sentences:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for sentence in sentences:
        if current and length + len(sentence) + 1 > CHUNK_TARGET:
            chunks.append(" ".join(current))
            # One sentence of overlap, so a fact stated across a boundary is
            # still wholly inside one chunk.
            current = [current[-1]]
            length = len(current[0])
        current.append(sentence)
        length += len(sentence) + 1
    if current:
        chunks.append(" ".join(current))

    # Overlap can repeat a chunk when the tail fits entirely in the previous
    # one; storing it twice would return the same note twice.
    return [chunk for index, chunk in enumerate(chunks) if index == 0 or chunk != chunks[index - 1]]


def detect_lifts(text: str, session_lifts: Iterable[str] = ()) -> list[str]:
    """Which lifts a note is about, by keyword, else the session's own lifts.

    The fallback is what makes "felt strong today" useful: it is about whatever
    was trained, and the alternative is a note attached to nothing. Returned in
    `LIFT_CODES` order so the stored list is stable rather than text-order
    dependent.

    **A pain note takes the union, not the mention.** "Mention wins" is right
    for retrieval and wrong for safety, and the stored codes feed both. On a
    squat day, "knee hurt in the warm ups, bench felt fine" mentions only
    bench, so mention-wins flagged *bench* -- the lift described as fine -- and
    cleared squat, the lift being trained when it hurt. Widening means a lift
    that was fine can be flagged too, which costs a refusal the athlete has to
    work around; missing the lift that hurt costs them the injury. ADR-0006
    already chose that direction by blocking on an absent baseline.
    """
    scrubbed = _BENCH_PRESS.sub("bench", text)
    found = {code for code, pattern in LIFT_PATTERNS.items() if pattern.search(scrubbed)}
    session = {code for code in session_lifts if code in set(enums.LIFT_CODES)}
    if is_pain_text(text):
        found |= session
    elif not found:
        found = session
    return [code for code in enums.LIFT_CODES if code in found]


def is_pain_text(text: str) -> bool:
    """Does this note report pain, ignoring a negation in front of it?

    Pure and separately tested, because it is the whole of the safety decision
    and it should be checkable without a database.

    A negation only reaches within its own clause. The window was two tokens of
    plain text, which let "no energy, pain in the knee" read as *no pain* --
    "no" cancelling a report in the next clause. That is the one direction this
    function must not fail in, since the consequence of a miss is heavy work on
    something that hurt. Punctuation now bounds it, and the window stays narrow
    inside the clause because widening it reintroduces the same bug without a
    comma: "no sleep and my knee has pain" must still flag.
    """
    for clause in _CLAUSE_END.split(text.lower()):
        tokens = _TOKEN.findall(clause)
        for index, token in enumerate(tokens):
            if not any(stem in token for stem in PAIN_STEMS):
                continue
            if _negation_cancels(tokens, index):
                continue
            return True
    return False


def _negation_cancels(tokens: list[str], index: int) -> bool:
    """Is the pain word at `index` negated, within this clause?

    `tokens` is one clause, so there is no way to reach across punctuation.
    """
    start = max(0, index - NEGATION_LOOKBACK)
    for position in range(start, index):
        if tokens[position] not in NEGATIONS:
            continue
        # "not much pain" reduces the report rather than denying it.
        if any(word in NEGATION_HEDGES for word in tokens[position + 1 : position + 3]):
            continue
        return True
    return False


# --- Writing -------------------------------------------------------------


def index_log_note(
    session: Session, log: models.SessionLog, embedder: Embedder
) -> list[models.NoteChunk]:
    """Embed a logged session's note and store its chunks.

    Everything is embedded **before** anything is written, so a model that dies
    halfway leaves the previous chunks in place rather than a partial set. The
    delete-then-insert is what makes this safe to run again: `reindex` calls the
    same function, and `note_chunk` has no chunk ordinal to update in place.
    """
    if not log.note or not log.note.strip():
        return []

    athlete_id, session_lifts = _log_context(session, log)
    chunks = chunk_note(log.note)
    if not chunks:
        return []
    lift_codes = detect_lifts(log.note, session_lifts)
    vectors = embedder.embed_documents(chunks)

    _delete_chunks_for_log(session, log.id)
    rows = [
        models.NoteChunk(
            athlete_id=athlete_id,
            log_id=log.id,
            lift_codes=list(lift_codes),
            text=chunk,
            noted_on=log.performed_on,
            embedding=vector,
            # A logged note is the athlete writing about their own training.
            is_synthetic=False,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    for row in rows:
        session.add(row)
    # Flush, never commit: the route owns the transaction (ADR-0007 rule 1).
    session.flush()
    return rows


def reindex_notes(session: Session, athlete_id: uuid.UUID, embedder: Embedder) -> int:
    """Re-embed every note this athlete has, and return how many chunks exist.

    This is the repair path, and it is why a failed embed needs no queue table.
    `session_log.note` is the source of truth, so a log with a note and no
    chunks *is* the backlog -- which is also why this deletes and rebuilds
    rather than skipping what is already there: after an embedding-model change
    the existing vectors are the thing that has to go.
    """
    written = 0
    for log in _logs_with_notes(session, athlete_id):
        written += len(index_log_note(session, log, embedder))
    written += _reindex_standalone(session, athlete_id, embedder)
    return written


def _reindex_standalone(session: Session, athlete_id: uuid.UUID, embedder: Embedder) -> int:
    """Re-embed chunks with no log behind them -- the seeded demo notes.

    Updated in place rather than deleted and rebuilt: there is no source row to
    rebuild them *from*, so their text is all there is.
    """
    rows = list(
        session.exec(
            select(models.NoteChunk)
            .where(models.NoteChunk.athlete_id == athlete_id)
            .where(models.NoteChunk.log_id.is_(None))  # type: ignore[union-attr]
            .order_by(models.NoteChunk.noted_on)
        ).all()
    )
    if not rows:
        return 0
    for row, vector in zip(rows, embedder.embed_documents([row.text for row in rows]), strict=True):
        row.embedding = vector
        session.add(row)
    session.flush()
    return len(rows)


# --- Reading -------------------------------------------------------------


def search_notes(
    session: Session,
    athlete_id: uuid.UUID,
    query: str,
    embedder: Embedder,
    *,
    lift: str | None = None,
    since: dt.date | None = None,
    k: int = DEFAULT_K,
    include_synthetic: bool = False,
) -> list[NoteHit]:
    """The nearest notes by meaning, closest first.

    Synthetic notes are excluded by default, in every direction, because
    `is_synthetic` is the authoritative "is this real?" and every consumer
    filters on it (ADR-0004 rule 1). Demo mode opts in explicitly.

    May return fewer than `k`. The HNSW index is searched before the athlete
    and date filters are applied, so a narrow filter over a large table can
    leave fewer neighbours than asked for. Harmless with one athlete; worth
    knowing before this is read as "the k best".
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be between 1 and {MAX_K}, got {k}")

    distance = models.NoteChunk.embedding.cosine_distance(  # type: ignore[union-attr]
        embedder.embed_query(query)
    ).label("distance")
    statement = select(models.NoteChunk, distance).where(models.NoteChunk.athlete_id == athlete_id)
    statement = _apply_filters(
        statement, lift=lift, since=since, include_synthetic=include_synthetic
    )

    rows = session.exec(statement.order_by(sa.asc(distance)).limit(k)).all()
    return [_hit(chunk, float(value)) for chunk, value in rows]


def pain_flags(
    session: Session,
    athlete_id: uuid.UUID,
    since: dt.date,
    *,
    include_synthetic: bool = False,
) -> dict[str, list[NoteHit]]:
    """Lifts with a pain note since `since`, by keyword and nothing else.

    No embedder, no vector, no threshold: `ILIKE` narrows the rows and
    `is_pain_text` decides, so the answer is the same every time and can be
    explained to the athlete in words. Feeds `ValidationContext.pain_lifts`
    (ADR-0006 rule 2) and spec 13's router filter, both of which run before any
    model is asked anything.

    Synthetic notes are excluded by default. A seeded demo note about a sore
    knee must not be able to block a real session -- which is the direction
    that matters, since the consequence of a pain flag is a refusal.
    """
    statement = (
        select(models.NoteChunk)
        .where(models.NoteChunk.athlete_id == athlete_id)
        .where(models.NoteChunk.noted_on >= since)
        .where(sa.or_(*(models.NoteChunk.text.ilike(f"%{stem}%") for stem in PAIN_STEMS)))  # type: ignore[union-attr]
    )
    if not include_synthetic:
        statement = statement.where(models.NoteChunk.is_synthetic.is_(False))  # type: ignore[union-attr]

    out: dict[str, list[NoteHit]] = {}
    for chunk in session.exec(statement.order_by(models.NoteChunk.noted_on)).all():
        if not is_pain_text(chunk.text):
            continue
        hit = _hit(chunk, 0.0)
        for code in chunk.lift_codes:
            out.setdefault(code, []).append(hit)
    return out


def pain_lifts(
    session: Session,
    athlete_id: uuid.UUID,
    today: dt.date,
    *,
    include_synthetic: bool = False,
) -> set[str]:
    """`pain_flags` reduced to what `ValidationContext` wants.

    Filtered to known lift codes: the validator raises on an unknown one in any
    context key (ADR-0006 modification 12), and a note stored before a lift was
    renamed should not become a crash.
    """
    since = today - dt.timedelta(days=PAIN_WINDOW_DAYS)
    flagged = pain_flags(session, athlete_id, since, include_synthetic=include_synthetic)
    return {code for code in flagged if code in set(enums.LIFT_CODES)}


# --- Internals -----------------------------------------------------------


def _apply_filters(statement, *, lift: str | None, since: dt.date | None, include_synthetic: bool):
    if lift is not None:
        # `@>` on the JSONB array, which the spec writes as `?`. Same result for
        # a string element, and this one cannot be confused with a placeholder.
        statement = statement.where(models.NoteChunk.lift_codes.contains([lift]))  # type: ignore[union-attr]
    if since is not None:
        statement = statement.where(models.NoteChunk.noted_on >= since)
    if not include_synthetic:
        statement = statement.where(models.NoteChunk.is_synthetic.is_(False))  # type: ignore[union-attr]
    return statement


def _hit(chunk: models.NoteChunk, distance: float) -> NoteHit:
    return NoteHit(
        text=chunk.text,
        noted_on=chunk.noted_on,
        lift_codes=tuple(chunk.lift_codes),
        distance=distance,
        is_synthetic=chunk.is_synthetic,
    )


def _log_context(session: Session, log: models.SessionLog) -> tuple[uuid.UUID, list[str]]:
    """The athlete the log belongs to, and the lifts its session prescribed.

    Queried here rather than passed in by `services.log_session`, because
    `services` imports this module and the reverse import would be a cycle.
    """
    athlete_id = session.exec(
        select(models.Block.athlete_id)
        .join(models.PlannedSession, models.PlannedSession.block_id == models.Block.id)  # type: ignore[arg-type]
        .where(models.PlannedSession.id == log.planned_session_id)
    ).one()
    lifts = session.exec(
        select(models.PlannedLift.lift_code).where(
            models.PlannedLift.session_id == log.planned_session_id
        )
    ).all()
    return athlete_id, list(dict.fromkeys(lifts))


def _delete_chunks_for_log(session: Session, log_id: uuid.UUID) -> None:
    session.exec(
        sa.delete(models.NoteChunk).where(models.NoteChunk.log_id == log_id)  # type: ignore[arg-type]
    )


def _logs_with_notes(session: Session, athlete_id: uuid.UUID) -> Sequence[models.SessionLog]:
    return session.exec(
        select(models.SessionLog)
        .join(
            models.PlannedSession,
            models.PlannedSession.id == models.SessionLog.planned_session_id,  # type: ignore[arg-type]
        )
        .join(models.Block, models.Block.id == models.PlannedSession.block_id)  # type: ignore[arg-type]
        .where(models.Block.athlete_id == athlete_id)
        .where(models.SessionLog.note.is_not(None))  # type: ignore[union-attr]
        .where(models.SessionLog.note != "")
        .order_by(models.SessionLog.performed_on)
    ).all()
