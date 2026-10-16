"""Chunking, lift detection, pain screening, retrieval and reindex.

The pure half runs anywhere. The retrieval half needs real PostgreSQL with
pgvector -- there is no way to check a cosine ordering, a JSONB containment
filter or an HNSW-backed query without one -- and skips cleanly when Postgres
is absent, like the rest of this suite.

No test calls Ollama. `FakeEmbedder` has real geometry (see `tests/fakes.py`),
which is what lets the ordering assertions mean something.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlmodel import Session, select

from domains.strength import models, notes, services
from domains.strength.schemas import LoggedLift
from planner_core.errors import ModelNotPulled, OllamaUnreachable
from tests import golden
from tests.fakes import BrokenEmbedder, FakeEmbedder

# A Monday: `block.start_date` has a CHECK that it is one.
MONDAY = dt.date(2026, 3, 2)

# --- Chunking (pure) ------------------------------------------------------


def test_a_short_note_is_one_chunk():
    text = "Left knee felt tight at the bottom of squats"

    assert notes.chunk_note(text) == [text]


def test_a_note_at_the_boundary_is_still_one_chunk():
    text = "a" * notes.SINGLE_CHUNK_MAX

    assert notes.chunk_note(text) == [text]


def test_a_note_past_the_boundary_is_split():
    sentence = "The squats felt heavy and slow today for no obvious reason. "
    text = sentence * 20

    chunks = notes.chunk_note(text)

    assert len(chunks) > 1
    # Nothing is lost: every sentence survives somewhere.
    assert all("squats felt heavy" in chunk for chunk in chunks)


def test_a_split_note_overlaps_by_one_sentence():
    """So a fact stated across a boundary is wholly inside one chunk."""
    sentences = [f"Sentence number {index} about the training session." for index in range(30)]
    text = " ".join(sentences)

    chunks = notes.chunk_note(text)

    assert len(chunks) > 1
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        tail = earlier.rsplit(". ", 1)[-1].rstrip(".")
        assert tail in later


def test_no_chunk_is_repeated():
    """Overlap must not store the same text twice and return a note twice."""
    text = " ".join(f"Short note {index}." for index in range(40))

    chunks = notes.chunk_note(text)

    assert len(chunks) == len(set(chunks))


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_an_empty_note_is_no_chunks(text):
    assert notes.chunk_note(text) == []


def test_whitespace_around_a_note_is_stripped():
    assert notes.chunk_note("  knee felt fine  ") == ["knee felt fine"]


# --- Lift detection (pure) -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("squats felt smooth", ["squat"]),
        # "bench press" is the bench lift, not the bench lift *and* the
        # overhead press. See `test_bench_press_is_not_the_overhead_press`.
        ("bench press day", ["bench"]),
        ("bench and press both moved", ["bench", "press"]),
        ("deadlifts were slow", ["deadlift"]),
        ("dead lift felt fast", ["deadlift"]),
        ("dl was heavy", ["deadlift"]),
        ("pulls felt strong", ["deadlift"]),
        ("ohp is my weak point", ["press"]),
        ("overhead work hurt", ["press"]),
        ("rows felt easy", ["row"]),
        ("SQUAT in caps still counts", ["squat"]),
        ("squat and bench both moved", ["squat", "bench"]),
    ],
)
def test_a_lift_is_recognised_by_keyword(text, expected):
    assert notes.detect_lifts(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "did some pullovers",
        "impressive session",
        "narrow stance felt better",
        "shoulders were crowded in the rack",
    ],
)
def test_a_word_that_merely_contains_a_lift_name_is_not_a_match(text):
    """`pullover` is not a deadlift, `impressive` is not a press and `narrow`
    is not a row. Word boundaries are the whole reason the patterns are not
    plain substrings. (`rowing` and `pressed` *are* matches, and should be.)"""
    assert notes.detect_lifts(text) == []


@pytest.mark.parametrize(
    ("text", "expected"), [("rowing felt easy", ["row"]), ("pressed overhead", ["press"])]
)
def test_an_inflected_lift_name_is_still_a_match(text, expected):
    assert notes.detect_lifts(text) == expected


def test_a_note_naming_no_lift_takes_the_sessions_lifts():
    """What makes "felt strong today" useful rather than attached to nothing."""
    assert notes.detect_lifts("felt strong today", ["bench", "row"]) == ["bench", "row"]


def test_the_detected_lifts_are_in_a_stable_order():
    """Stored order must not depend on which word came first in the sentence."""
    assert notes.detect_lifts("rows then bench then squats") == ["squat", "bench", "row"]


def test_an_unknown_lift_in_the_session_fallback_is_dropped():
    assert notes.detect_lifts("felt fine", ["bananas"]) == []


# --- Pain screening (pure) -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sharp pain in lower back on deadlift",
        "knee really hurts today",
        "injured my shoulder",
        "tweaked something in the hip",
        "felt a strain in the hamstring",
        "a pinch in the elbow",
        "slight twinge in the knee",
        "SHARP PAIN, stopped the session",
    ],
)
def test_a_pain_note_is_recognised(text):
    assert notes.is_pain_text(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "no pain today",
        "didn't hurt at all",
        "did not hurt",
        "knee felt tight but no pain",
        "without pain for the first time",
        "never had pain in that knee",
        "no sharp pain anywhere",
        "zero pain",
    ],
)
def test_a_negated_pain_word_is_not_a_pain_note(text):
    """A safety rule that reads "no pain today" as pain stops being trusted,
    and a rule nobody trusts gets switched off."""
    assert notes.is_pain_text(text) is False


def test_a_real_report_after_a_negation_still_counts():
    """ "no sleep, knee pain" means the knee hurt. The negation window is two
    tokens precisely so it cannot cancel a later, separate report."""
    assert notes.is_pain_text("no sleep at all, and knee pain on squats") is True


def test_a_note_with_no_pain_words_is_not_a_pain_note():
    assert notes.is_pain_text("slept well, bar speed was good") is False


# --- Indexing and retrieval (needs Postgres) ------------------------------


@pytest.fixture
def athlete(db_session: Session) -> models.Athlete:
    row = golden.athlete()
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def template(db_session: Session) -> models.BlockTemplate:
    """`block.template_id` is NOT NULL, so a block needs one even here."""
    row = golden.template()
    db_session.add(row)
    db_session.flush()
    return row


def _planned(block_id, *, week=1, slot=1, seq=1, date=None, status="planned"):
    """`original_date` is NOT NULL: a session remembers where it was first put."""
    when = date or MONDAY
    return models.PlannedSession(
        block_id=block_id,
        week=week,
        slot=slot,
        seq=seq,
        date=when,
        original_date=when,
        status=status,
    )


def _block(athlete_id, template_id) -> models.Block:
    """A one-week block starting on a Monday (`ck_block_start_date_is_monday`)."""
    return models.Block(
        athlete_id=athlete_id,
        template_id=template_id,
        number=1,
        start_date=MONDAY,
        status="active",
        lifts=["squat"],
        one_rm_snapshot={"squat": "125"},
    )


def _chunk(
    athlete_id: uuid.UUID,
    text: str,
    *,
    noted_on: dt.date,
    lift_codes: list[str] | None = None,
    is_synthetic: bool = False,
    embedder: FakeEmbedder | None = None,
) -> models.NoteChunk:
    embedder = embedder or FakeEmbedder()
    return models.NoteChunk(
        athlete_id=athlete_id,
        lift_codes=lift_codes if lift_codes is not None else notes.detect_lifts(text),
        text=text,
        noted_on=noted_on,
        embedding=embedder.embed_documents([text])[0],
        is_synthetic=is_synthetic,
    )


def test_a_query_finds_the_note_it_is_about(db_session: Session, athlete):
    """Spec 12 §12's own example, which is the point of the whole module."""
    today = dt.date(2026, 3, 2)
    db_session.add(
        _chunk(athlete.id, "left knee felt tight at the bottom of squats", noted_on=today)
    )
    db_session.add(_chunk(athlete.id, "slept five hours and the bar felt slow", noted_on=today))
    db_session.add(_chunk(athlete.id, "rows felt easy today", noted_on=today))
    db_session.flush()

    hits = notes.search_notes(db_session, athlete.id, "how has my knee been?", FakeEmbedder())

    assert hits
    assert "knee" in hits[0].text


def test_hits_come_back_closest_first(db_session: Session, athlete):
    today = dt.date(2026, 3, 2)
    db_session.add(_chunk(athlete.id, "knee pain on squats", noted_on=today))
    db_session.add(_chunk(athlete.id, "bench felt good", noted_on=today))
    db_session.flush()

    hits = notes.search_notes(db_session, athlete.id, "knee", FakeEmbedder(), k=5)

    assert [hit.distance for hit in hits] == sorted(hit.distance for hit in hits)


def test_the_lift_filter_uses_the_stored_codes(db_session: Session, athlete):
    today = dt.date(2026, 3, 2)
    db_session.add(_chunk(athlete.id, "squats felt smooth", noted_on=today))
    db_session.add(_chunk(athlete.id, "rows felt easy", noted_on=today))
    db_session.flush()

    hits = notes.search_notes(db_session, athlete.id, "felt", FakeEmbedder(), lift="row")

    assert [hit.text for hit in hits] == ["rows felt easy"]


def test_the_since_filter_excludes_older_notes(db_session: Session, athlete):
    db_session.add(_chunk(athlete.id, "old note about squats", noted_on=dt.date(2026, 1, 1)))
    db_session.add(_chunk(athlete.id, "new note about squats", noted_on=dt.date(2026, 3, 1)))
    db_session.flush()

    hits = notes.search_notes(
        db_session, athlete.id, "squats", FakeEmbedder(), since=dt.date(2026, 2, 1)
    )

    assert [hit.text for hit in hits] == ["new note about squats"]


def test_another_athletes_notes_are_never_returned(db_session: Session, athlete):
    other = models.Athlete(name="Someone Else")
    db_session.add(other)
    db_session.flush()
    db_session.add(_chunk(other.id, "knee pain on squats", noted_on=dt.date(2026, 3, 2)))
    db_session.flush()

    hits = notes.search_notes(db_session, athlete.id, "knee", FakeEmbedder())

    assert hits == []


def test_synthetic_notes_are_hidden_unless_asked_for(db_session: Session, athlete):
    """ADR-0004 rule 1: every consumer filters on the flag."""
    today = dt.date(2026, 3, 2)
    db_session.add(
        _chunk(athlete.id, "seeded note about squats", noted_on=today, is_synthetic=True)
    )
    db_session.flush()

    assert notes.search_notes(db_session, athlete.id, "squats", FakeEmbedder()) == []

    hits = notes.search_notes(
        db_session, athlete.id, "squats", FakeEmbedder(), include_synthetic=True
    )
    assert [hit.is_synthetic for hit in hits] == [True]


def test_k_bounds_the_number_of_hits(db_session: Session, athlete):
    today = dt.date(2026, 3, 2)
    for index in range(6):
        db_session.add(_chunk(athlete.id, f"squats felt fine on day {index}", noted_on=today))
    db_session.flush()

    assert len(notes.search_notes(db_session, athlete.id, "squats", FakeEmbedder(), k=3)) == 3


@pytest.mark.parametrize("bad", [0, -1, notes.MAX_K + 1])
def test_a_k_outside_the_bounds_is_refused(bad):
    with pytest.raises(ValueError, match="k must be between"):
        notes.search_notes(None, uuid.uuid4(), "knee", FakeEmbedder(), k=bad)


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_query_is_refused(blank):
    with pytest.raises(ValueError, match="must not be empty"):
        notes.search_notes(None, uuid.uuid4(), blank, FakeEmbedder())


def test_an_empty_query_is_refused_before_anything_is_embedded():
    """Cheap guard first: a blank query should not cost a model round-trip."""
    embedder = FakeEmbedder()

    with pytest.raises(ValueError):
        notes.search_notes(None, uuid.uuid4(), "", embedder)

    assert embedder.queries == []


# --- pain_flags (needs Postgres) -----------------------------------------


def test_a_pain_note_is_grouped_under_its_lift(db_session: Session, athlete):
    db_session.add(
        _chunk(athlete.id, "sharp pain in lower back on deadlift", noted_on=dt.date(2026, 3, 2))
    )
    db_session.flush()

    flagged = notes.pain_flags(db_session, athlete.id, dt.date(2026, 3, 1))

    assert set(flagged) == {"deadlift"}
    assert "sharp pain" in flagged["deadlift"][0].text


def test_a_negated_note_does_not_flag_its_lift(db_session: Session, athlete):
    """The SQL prefilter matches "pain"; `is_pain_text` is what decides."""
    db_session.add(
        _chunk(athlete.id, "squats felt fine, no pain today", noted_on=dt.date(2026, 3, 2))
    )
    db_session.flush()

    assert notes.pain_flags(db_session, athlete.id, dt.date(2026, 3, 1)) == {}


def test_a_pain_note_before_the_window_is_ignored(db_session: Session, athlete):
    db_session.add(_chunk(athlete.id, "knee pain on squats", noted_on=dt.date(2026, 1, 1)))
    db_session.flush()

    assert notes.pain_flags(db_session, athlete.id, dt.date(2026, 3, 1)) == {}


def test_a_synthetic_pain_note_cannot_block_a_real_session(db_session: Session, athlete):
    """The direction that matters: a pain flag's consequence is a refusal."""
    db_session.add(
        _chunk(athlete.id, "knee pain on squats", noted_on=dt.date(2026, 3, 2), is_synthetic=True)
    )
    db_session.flush()

    assert notes.pain_flags(db_session, athlete.id, dt.date(2026, 3, 1)) == {}

    opted_in = notes.pain_flags(db_session, athlete.id, dt.date(2026, 3, 1), include_synthetic=True)
    assert set(opted_in) == {"squat"}


def test_pain_flags_asks_the_embedder_nothing(db_session: Session, athlete):
    """Safety is keyword-based, never similarity-based (non-negotiable 3).

    `pain_flags` does not even take an embedder, and the signature check alone
    would not catch the refactor this is guarding against: a body that reaches
    for `get_embedder()` itself. So the real embedder is made to explode, and
    the call still has to work.
    """
    import inspect

    import planner_core.llm

    assert "embedder" not in inspect.signature(notes.pain_flags).parameters

    def explode():
        raise AssertionError("pain screening must never embed anything")

    original = planner_core.llm.get_embedder
    planner_core.llm.get_embedder = explode
    try:
        db_session.add(
            _chunk(athlete.id, "sharp pain in the knee on squats", noted_on=dt.date(2026, 3, 2))
        )
        db_session.flush()

        flagged = notes.pain_flags(db_session, athlete.id, dt.date(2026, 3, 1))
    finally:
        planner_core.llm.get_embedder = original

    assert set(flagged) == {"squat"}


def test_pain_lifts_reduces_to_the_set_the_validator_wants(db_session: Session, athlete):
    today = dt.date(2026, 3, 10)
    db_session.add(_chunk(athlete.id, "knee pain on squats", noted_on=today))
    db_session.add(_chunk(athlete.id, "bench felt great", noted_on=today))
    db_session.flush()

    assert notes.pain_lifts(db_session, athlete.id, today) == {"squat"}


def test_pain_lifts_uses_the_seven_day_window(db_session: Session, athlete):
    today = dt.date(2026, 3, 10)
    db_session.add(_chunk(athlete.id, "knee pain on squats", noted_on=today - dt.timedelta(days=8)))
    db_session.flush()

    assert notes.pain_lifts(db_session, athlete.id, today) == set()


# --- Indexing a logged note (needs Postgres) ------------------------------


@pytest.fixture
def logged(db_session: Session, athlete, template):
    """A block with one planned session, logged, carrying a note."""
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    planned = _planned(block.id, status="done")
    db_session.add(planned)
    db_session.flush()
    db_session.add(
        models.PlannedLift(
            session_id=planned.id,
            lift_code="squat",
            sets=3,
            reps=5,
            pct_num=70,
            pct_den=100,
            weight_kg=90,
            rel_intensity="0.8000",
        )
    )
    log = models.SessionLog(
        planned_session_id=planned.id,
        performed_on=dt.date(2026, 3, 2),
        entries=[{"lift_code": "squat", "sets_done": 3, "reps_done": 5, "weight_kg": "90.00"}],
        note="Left knee felt tight at the bottom of squats",
    )
    db_session.add(log)
    db_session.flush()
    return {"block": block, "planned": planned, "log": log, "athlete": athlete}


def test_a_logged_note_is_chunked_embedded_and_stored(db_session: Session, logged):
    rows = notes.index_log_note(db_session, logged["log"], FakeEmbedder())

    assert len(rows) == 1
    assert rows[0].lift_codes == ["squat"]
    assert rows[0].noted_on == logged["log"].performed_on
    assert rows[0].athlete_id == logged["athlete"].id
    assert rows[0].is_synthetic is False
    assert len(rows[0].embedding) == 768


def test_indexing_the_same_log_twice_does_not_duplicate(db_session: Session, logged):
    notes.index_log_note(db_session, logged["log"], FakeEmbedder())
    notes.index_log_note(db_session, logged["log"], FakeEmbedder())

    stored = db_session.exec(
        select(models.NoteChunk).where(models.NoteChunk.log_id == logged["log"].id)
    ).all()
    assert len(stored) == 1


def test_a_log_with_no_note_indexes_nothing(db_session: Session, logged):
    logged["log"].note = None
    embedder = FakeEmbedder()

    assert notes.index_log_note(db_session, logged["log"], embedder) == []
    assert embedder.documents == []


def test_a_failed_embed_leaves_no_partial_chunks(db_session: Session, logged):
    """Everything is embedded before anything is written."""
    with pytest.raises(OllamaUnreachable):
        notes.index_log_note(
            db_session, logged["log"], BrokenEmbedder(OllamaUnreachable("http://localhost:11434"))
        )

    assert db_session.exec(select(models.NoteChunk)).all() == []


# --- Reindex (needs Postgres) --------------------------------------------


def test_reindex_builds_the_chunks_a_failed_embed_never_wrote(db_session: Session, logged):
    """The repair path, and why no queue table exists: the log is the backlog."""
    assert db_session.exec(select(models.NoteChunk)).all() == []

    count = notes.reindex_notes(db_session, logged["athlete"].id, FakeEmbedder())

    assert count == 1
    assert len(db_session.exec(select(models.NoteChunk)).all()) == 1


def test_reindex_twice_leaves_the_same_rows(db_session: Session, logged):
    """Compared by content, not by id.

    Reindex deletes and re-inserts, so the ids *must* change -- asserting on
    them, or only on the count, would pass against an implementation that
    duplicated or dropped a note's text.
    """

    def contents():
        return sorted(
            (row.text, row.log_id, tuple(row.lift_codes), row.noted_on)
            for row in db_session.exec(select(models.NoteChunk)).all()
        )

    notes.reindex_notes(db_session, logged["athlete"].id, FakeEmbedder())
    first = contents()

    notes.reindex_notes(db_session, logged["athlete"].id, FakeEmbedder())

    assert contents() == first
    assert len(first) == 1


def test_reindex_re_embeds_a_standalone_note_in_place(db_session: Session, athlete):
    """A seeded note has no log to rebuild from, so its row is updated."""
    row = _chunk(
        athlete.id, "seeded note about squats", noted_on=dt.date(2026, 3, 2), is_synthetic=True
    )
    row.embedding = [0.0] * 767 + [1.0]
    db_session.add(row)
    db_session.flush()
    row_id = row.id

    count = notes.reindex_notes(db_session, athlete.id, FakeEmbedder())

    assert count == 1
    refreshed = db_session.get(models.NoteChunk, row_id)
    assert refreshed is not None
    assert refreshed.is_synthetic is True
    assert list(refreshed.embedding) != [0.0] * 767 + [1.0]


# --- log_session keeps the log when embedding fails -----------------------


def test_a_log_survives_the_embedder_being_down(db_session: Session, logged, caplog):
    """A training record must not be lost to a model being switched off."""
    other = _planned(logged["block"].id, slot=2, seq=2, date=dt.date(2026, 3, 4))
    db_session.add(other)
    db_session.flush()

    with caplog.at_level(logging.WARNING, logger="domains.strength.services"):
        log = services.log_session(
            db_session,
            other.id,
            dt.date(2026, 3, 4),
            [LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg="90")],
            note="knee felt tight again",
            embedder=BrokenEmbedder(ModelNotPulled("nomic-embed-text")),
        )

    assert log.id is not None
    assert log.note == "knee felt tight again"
    assert "reindex" in caplog.text
    # No chunk for this log, and the transaction is still usable.
    assert (
        db_session.exec(select(models.NoteChunk).where(models.NoteChunk.log_id == log.id)).all()
        == []
    )


def test_a_log_with_a_note_indexes_it_when_the_embedder_works(db_session: Session, logged):
    other = _planned(logged["block"].id, slot=2, seq=2, date=dt.date(2026, 3, 4))
    db_session.add(other)
    db_session.flush()

    log = services.log_session(
        db_session,
        other.id,
        dt.date(2026, 3, 4),
        [LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg="90")],
        note="squats felt smooth",
        embedder=FakeEmbedder(),
    )

    stored = db_session.exec(
        select(models.NoteChunk).where(models.NoteChunk.log_id == log.id)
    ).all()
    assert [row.text for row in stored] == ["squats felt smooth"]


# --- last_done_pct (needs Postgres) --------------------------------------


def test_last_done_pct_is_the_highest_completed_intensity(db_session: Session, logged):
    """ADR-0006's definition, which the ceiling in `pain_progression` uses."""
    done = _planned(logged["block"].id, slot=2, seq=2, date=dt.date(2026, 3, 4), status="done")
    db_session.add(done)
    db_session.flush()
    db_session.add(
        models.PlannedLift(
            session_id=done.id,
            lift_code="squat",
            sets=1,
            reps=3,
            pct_num=85,
            pct_den=100,
            weight_kg=105,
            rel_intensity="0.9000",
        )
    )
    db_session.add(
        models.SessionLog(
            planned_session_id=done.id,
            performed_on=dt.date(2026, 3, 4),
            entries=[{"lift_code": "squat", "sets_done": 1, "reps_done": 3, "weight_kg": "105.00"}],
        )
    )
    db_session.flush()

    ceiling = services.last_done_pct(db_session, logged["athlete"].id)

    # 85/100 from the second session beats 70/100 from the first.
    assert ceiling["squat"] == pytest.approx(0.85)


def test_a_logged_attempt_with_no_sets_done_is_not_a_completion(db_session: Session, logged):
    """The athlete loaded the bar and did not lift it. Same rule as a 1RM."""
    logged["log"].entries = [
        {"lift_code": "squat", "sets_done": 0, "reps_done": 0, "weight_kg": "90.00"}
    ]
    db_session.add(logged["log"])
    db_session.flush()

    assert services.last_done_pct(db_session, logged["athlete"].id) == {}


def test_an_unlogged_session_contributes_no_ceiling(db_session: Session, athlete, template):
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    planned = _planned(block.id)
    db_session.add(planned)
    db_session.flush()
    db_session.add(
        models.PlannedLift(
            session_id=planned.id,
            lift_code="squat",
            sets=3,
            reps=5,
            pct_num=70,
            pct_den=100,
            weight_kg=90,
            rel_intensity="0.8000",
        )
    )
    db_session.flush()

    assert services.last_done_pct(db_session, athlete.id) == {}


def test_the_ceiling_is_exact_not_a_rounded_float(db_session: Session, logged):
    """The authored template contains 112.5/105; a float would lose it."""
    from fractions import Fraction

    ceiling = services.last_done_pct(db_session, logged["athlete"].id)

    assert isinstance(ceiling["squat"], Fraction)
    assert ceiling["squat"] == Fraction(70, 100)


# --- The screening accuracy a review found wrong ---------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("knee hurt in the warm ups, bench felt fine", ["squat", "bench"]),
        ("sharp pain in the hip, rows were ok", ["squat", "row"]),
        ("tweaked my back, pressed anyway", ["squat", "press"]),
        ("sharp pain somewhere, no idea where", ["squat"]),
    ],
)
def test_a_pain_note_keeps_the_lift_that_was_being_trained(text, expected):
    """Mention-wins cleared the lift that hurt.

    All of these are pain notes logged on a squat day. Reading only the
    mentioned lift flagged bench, row or press -- in two cases a lift the note
    says was *fine* -- and left squat, the lift under the bar when it hurt,
    unflagged. The union is the safe direction.
    """
    assert notes.detect_lifts(text, ["squat"]) == expected


def test_a_note_that_is_not_about_pain_still_takes_the_mention():
    """Widening is a safety measure, not a retrieval one: "squats felt smooth"
    on a squat-and-bench day is about squats."""
    assert notes.detect_lifts("squats felt smooth", ["squat", "bench"]) == ["squat"]


def test_bench_press_is_not_the_overhead_press():
    """The press pattern matches inside "bench press", so a shoulder tweaked on
    the bench used to flag the overhead press and block every press session."""
    assert notes.detect_lifts("sharp pain in my shoulder on bench press") == ["bench"]
    assert notes.detect_lifts("benched and it hurt") == ["bench"]


def test_pulling_a_muscle_is_not_a_deadlift():
    """ "pulled a muscle during squats" is a pain note about a squat."""
    assert notes.detect_lifts("pulled a muscle during squats") == ["squat"]
    assert notes.detect_lifts("pulls felt strong") == ["deadlift"]


@pytest.mark.parametrize(
    "text",
    [
        "no energy, pain in the knee",
        "not great, pain in the knee",
        "slept badly; sharp pain in the hip",
        "no sleep and my knee has pain",
        "not much pain",
        "not a lot of pain but it is there",
    ],
)
def test_a_negation_cannot_reach_into_the_next_clause(text):
    """The miss that mattered: "no" cancelling a report in a later clause.

    A safety filter may be over-eager; it may not be silently blind, which is
    what this was. "not much pain" is a *reduced* report, not an absent one.
    """
    assert notes.is_pain_text(text) is True


# --- The ceiling a review found inflatable --------------------------------


def _log_entry(lift_code="squat", sets=1, reps=1, weight="100"):
    return {
        "lift_code": lift_code,
        "sets_done": sets,
        "reps_done": reps,
        "weight_kg": weight,
    }


def _done_session(db_session, block, *, seq, pct_num, weight_kg, entries, date=None):
    when = date or (MONDAY + dt.timedelta(days=seq))
    planned = _planned(block.id, slot=1, seq=seq, date=when, status="done")
    db_session.add(planned)
    db_session.flush()
    db_session.add(
        models.PlannedLift(
            session_id=planned.id,
            lift_code="squat",
            sets=1,
            reps=1,
            pct_num=pct_num,
            pct_den=100,
            weight_kg=weight_kg,
            rel_intensity="0.9000",
        )
    )
    db_session.add(
        models.SessionLog(planned_session_id=planned.id, performed_on=when, entries=entries)
    )
    db_session.flush()
    return planned


def test_a_missed_single_does_not_become_the_ceiling(db_session: Session, athlete, template):
    """A failed attempt is the opposite of evidence that the load was tolerated.

    107% prescribed at 115 kg, logged at 90 kg. Crediting 107% would put the
    ceiling above everything else in the block, so `pain_progression` could
    never fire for squat again.
    """
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    _done_session(
        db_session,
        block,
        seq=1,
        pct_num=107,
        weight_kg=115,
        entries=[_log_entry(weight="90")],
    )

    assert services.last_done_pct(db_session, athlete.id) == {}


def test_a_single_made_at_or_above_the_prescription_does_count(
    db_session: Session, athlete, template
):
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    _done_session(
        db_session,
        block,
        seq=1,
        pct_num=107,
        weight_kg=115,
        entries=[_log_entry(weight="117.5")],
    )

    assert services.last_done_pct(db_session, athlete.id) == {"squat": Fraction(107, 100)}


def test_the_ceiling_is_the_highest_not_the_most_recent(db_session: Session, athlete, template):
    """ADR-0006 says "highest completed so far".

    The heavier session is deliberately the *earlier* one here, so a
    "most recent" implementation would return 60% and fail.
    """
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    _done_session(
        db_session, block, seq=1, pct_num=90, weight_kg=112, entries=[_log_entry(weight="112")]
    )
    _done_session(
        db_session, block, seq=2, pct_num=60, weight_kg=75, entries=[_log_entry(weight="75")]
    )

    assert services.last_done_pct(db_session, athlete.id) == {"squat": Fraction(9, 10)}


# --- The wiring into the validator (ADR-0006's "Open for spec 12") --------

WEEKDAYS = {1: 0, 2: 2, 3: 4}


def test_a_pain_note_reaches_the_validation_context(db_session: Session, athlete, template):
    """The whole reason ADR-0006 left this open, and it had no test."""
    today = services.today_for(athlete)
    db_session.add(_chunk(athlete.id, "sharp pain in the knee on squats", noted_on=today))
    db_session.flush()

    ctx = services.validation_context(db_session, athlete, template, [], weekday_map=WEEKDAYS)

    assert ctx.pain_lifts == {"squat"}


def test_a_negated_note_does_not_reach_the_validation_context(
    db_session: Session, athlete, template
):
    today = services.today_for(athlete)
    db_session.add(_chunk(athlete.id, "squats felt fine, no pain at all", noted_on=today))
    db_session.flush()

    ctx = services.validation_context(db_session, athlete, template, [], weekday_map=WEEKDAYS)

    assert ctx.pain_lifts == set()


def test_a_completed_session_reaches_the_context_as_a_ceiling(
    db_session: Session, athlete, template
):
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    _done_session(
        db_session, block, seq=1, pct_num=70, weight_kg=90, entries=[_log_entry(weight="90")]
    )

    ctx = services.validation_context(db_session, athlete, template, [], weekday_map=WEEKDAYS)

    assert ctx.last_done_pct == {"squat": Fraction(70, 100)}


def test_a_painful_lift_is_refused_above_its_ceiling_and_allowed_at_it(
    db_session: Session, athlete, template
):
    """End to end: a real note in the database refusing a real plan.

    Both directions in one test, because the rule is only correct if it also
    lets held loads through -- a filter that refuses everything is not a safety
    feature.
    """
    from domains.strength.schemas import DraftLift, DraftSession
    from domains.strength.validator import validate

    today = services.today_for(athlete)
    block = _block(athlete.id, template.id)
    db_session.add(block)
    db_session.flush()
    _done_session(
        db_session,
        block,
        seq=1,
        pct_num=70,
        weight_kg=90,
        entries=[_log_entry(weight="90")],
        date=today - dt.timedelta(days=2),
    )
    db_session.add(_chunk(athlete.id, "sharp pain in the knee on squats", noted_on=today))
    db_session.flush()

    ctx = services.validation_context(db_session, athlete, template, [], weekday_map=WEEKDAYS)

    def plan(pct_num: int, weight: str):
        return [
            DraftSession(
                seq=9,
                week=2,
                slot=1,
                date=today + dt.timedelta(days=7),
                lifts=[
                    DraftLift(
                        lift_code="squat",
                        sets=3,
                        reps=5,
                        pct_num=Decimal(pct_num),
                        pct_den=Decimal(100),
                        weight_kg=Decimal(weight),
                        rel_intensity=Decimal("0.8000"),
                    )
                ],
            )
        ]

    heavier = validate(plan(80, "103"), ctx)
    assert not heavier.ok
    assert any(v.rule == "pain_progression" for v in heavier.violations)
    assert "highest completed intensity" in " ".join(v.message for v in heavier.violations)

    at_the_ceiling = validate(plan(70, "90"), ctx)
    assert not any(v.rule == "pain_progression" for v in at_the_ceiling.violations)


# --- Invariants the docstrings claim and nothing proved -------------------


def test_a_failed_re_embed_leaves_the_previous_chunks_intact(db_session: Session, logged):
    """`index_log_note` embeds before it deletes. This is why.

    Otherwise re-indexing a note while Ollama is down would destroy the vectors
    that were already working -- a repair path that loses data when it fails is
    worse than no repair path.
    """
    notes.index_log_note(db_session, logged["log"], FakeEmbedder())
    before = [row.text for row in db_session.exec(select(models.NoteChunk)).all()]
    assert before

    with pytest.raises(OllamaUnreachable):
        notes.index_log_note(
            db_session, logged["log"], BrokenEmbedder(OllamaUnreachable("http://localhost:11434"))
        )

    after = [row.text for row in db_session.exec(select(models.NoteChunk)).all()]
    assert after == before


def test_the_transaction_still_commits_after_a_swallowed_embed_failure(db_session: Session, logged):
    """The savepoint has to leave the outer transaction usable, not just unbroken.

    Asserting in-session state only would pass even if the connection were left
    in a state that refuses to commit -- which is exactly what an un-rolled-back
    savepoint would do, and it is the route's `commit()` that would discover it.
    """
    other = _planned(logged["block"].id, slot=2, seq=2, date=dt.date(2026, 3, 4))
    db_session.add(other)
    db_session.flush()

    log = services.log_session(
        db_session,
        other.id,
        dt.date(2026, 3, 4),
        [LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg="90")],
        note="knee felt tight again",
        embedder=BrokenEmbedder(ModelNotPulled("nomic-embed-text")),
    )

    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(models.SessionLog, log.id)
    assert saved is not None
    assert saved.note == "knee felt tight again"
    assert db_session.get(models.PlannedSession, other.id).status == "done"


def test_a_broken_embedder_that_raises_anything_still_keeps_the_log(db_session: Session, logged):
    """The invariant is "a log is never lost to indexing it", for *all* failures.

    A tuple of anticipated exception types read as the whole set and was not: a
    wrong-width vector from an `Embedder` that does not self-check reaches the
    database and raises a SQLAlchemy error, which used to escape.
    """
    other = _planned(logged["block"].id, slot=3, seq=3, date=dt.date(2026, 3, 6))
    db_session.add(other)
    db_session.flush()

    class WrongWidth:
        def embed_documents(self, texts):
            return [[0.1] * 7 for _ in texts]

        def embed_query(self, text):
            return [0.1] * 7

    log = services.log_session(
        db_session,
        other.id,
        dt.date(2026, 3, 6),
        [LoggedLift(lift_code="squat", sets_done=3, reps_done=5, weight_kg="90")],
        note="shoulder felt odd",
        embedder=WrongWidth(),
    )

    db_session.commit()
    db_session.expire_all()

    assert db_session.get(models.SessionLog, log.id) is not None
    assert (
        db_session.exec(select(models.NoteChunk).where(models.NoteChunk.log_id == log.id)).all()
        == []
    )


def test_reindex_counts_logged_and_standalone_notes_together(db_session: Session, logged):
    """`_reindex_standalone` was only ever exercised with a single row."""
    for index in range(3):
        db_session.add(
            _chunk(
                logged["athlete"].id,
                f"seeded note number {index} about squats",
                noted_on=dt.date(2026, 3, 2),
                is_synthetic=True,
            )
        )
    db_session.flush()

    count = notes.reindex_notes(db_session, logged["athlete"].id, FakeEmbedder())

    # One from the log, three standalone.
    assert count == 4


def test_the_nearest_hit_is_the_right_one_not_merely_sorted(db_session: Session, athlete):
    """Sortedness is true of any ordering; this pins which note comes first."""
    today = dt.date(2026, 3, 2)
    db_session.add(_chunk(athlete.id, "deadlift grip gave out before the back did", noted_on=today))
    db_session.add(_chunk(athlete.id, "bench press touch point more consistent", noted_on=today))
    db_session.add(_chunk(athlete.id, "slept five hours and felt flat", noted_on=today))
    db_session.flush()

    hits = notes.search_notes(db_session, athlete.id, "how is my grip holding up?", FakeEmbedder())

    assert "grip" in hits[0].text
