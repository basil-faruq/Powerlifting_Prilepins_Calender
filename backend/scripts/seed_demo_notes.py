"""Seed synthetic training notes so retrieval can be demonstrated locally.

**This writes test data. It is not a way to record how your training went.**

Note retrieval cannot be shown on an empty table, and the athlete's real notes
arrive one session at a time through `POST /sessions/{id}/log` (spec 06). This
fills that gap for a demo and for development, and nothing else.

Every row is flagged `is_synthetic = True`, the authoritative "is this real?"
that every consumer filters on (ADR-0004 rule 1). Two consequences are worth
knowing before trusting anything you see on top of these:

* `search_notes` **excludes** them unless asked for them explicitly, so the API
  returns nothing from this script until `include_synthetic=true`;
* `pain_flags` excludes them too, which is the direction that matters -- a
  seeded note about a sore knee must never block a real session.

Needs Ollama running, because the notes are embedded as they are written.
`--clear` deletes only synthetic chunks and leaves real ones untouched.

    uv run python -m scripts.seed_demo_notes
    uv run python -m scripts.seed_demo_notes --clear
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session, select

from domains.strength import notes as notes_module
from domains.strength.models import Athlete, NoteChunk
from planner_core.db import engine, ping_db
from planner_core.errors import LlmUnavailable
from planner_core.llm import get_embedder
from scripts.seed_athlete import ATHLETE_NAME

# A fixed Monday, for the same reason `seed_dev_one_rms` uses one: repeated runs
# stay a no-op and anything built on these notes is reproducible.
BLOCK_START = dt.date(2026, 3, 2)

# (day offset from BLOCK_START, text). Written the way a note actually gets
# typed -- short, lower-case, mid-thought. Spread over eleven weeks so the
# `since` filter and the 7-day pain window have something to bite on.
#
# Three of these report pain and one says the opposite ("no pain in the knee
# today"), which is the negation case `is_pain_text` has to get right. They sit
# early in the block on purpose: with `--clear` not run, they are far enough in
# the past that the 7-day window ignores them.
DEMO_NOTES: tuple[tuple[int, str], ...] = (
    (0, "first session back, everything felt heavy but moved fine"),
    (2, "slept 5 h, bar speed was poor on bench"),
    (4, "squats felt smooth today, could have done more"),
    (7, "left knee tight at the bottom of squats, no pain though"),
    (9, "bench coming along, touch point feels more consistent"),
    (11, "deadlifts felt slow off the floor, grip gave out before the back did"),
    (14, "woke up tired, cut the accessories and kept the main work"),
    (16, "sharp pain in the lower back on the second deadlift set, stopped there"),
    (18, "back feels better, kept squats light and easy"),
    (21, "good day, bench moved like it was 10 kg lighter"),
    (23, "rows felt strong, upped the weight a touch"),
    (25, "press is the weak point, shoulders ached after"),
    (28, "slept badly again, everything was a grind"),
    (30, "squat depth better with the wider stance"),
    (32, "deadlift felt fast, best session of the block so far"),
    (35, "tweaked something in the right shoulder on press, backed off"),
    (37, "shoulder still grumbling, skipped press and did rows instead"),
    (39, "shoulder fine now, pressed light with no issues"),
    (42, "bench PR for 3 reps, very happy with that"),
    (44, "legs heavy from deadlifts two days ago"),
    (46, "knee felt tight again in the warm up, fine once loaded"),
    (49, "good sleep for once, squats felt easy"),
    (51, "bar speed on bench is dropping as the weights go up"),
    (53, "deadlifts heavy but completed everything as written"),
    (56, "travelled all day, trained late, poor session"),
    (58, "felt flat, no energy, still got the work in"),
    (60, "squats moving well at the higher percentages"),
    (63, "press finally feels less awkward overhead"),
    (65, "rows are the one lift that never feels hard"),
    (67, "deadlift grip holding up better with the chalk"),
    (70, "overreach week is biting, everything aches a bit"),
    (72, "very tired this week, expected given the volume"),
    (74, "bench got stuck halfway on the last rep, failed it"),
    (77, "deload week, everything felt stupidly light"),
    (79, "sleeping better during the deload"),
    (81, "squat test next week, feeling cautiously confident"),
    (84, "openers felt easy, good sign"),
    (86, "hit a squat single above last block's best"),
    (88, "bench test went well, deadlift test tomorrow"),
    (90, "deadlift test done, block finished, knees survived"),
)


def seed_demo_notes(session: Session, athlete: Athlete, embedder) -> int:
    """Insert the synthetic notes that are not there yet. Idempotent by text."""
    existing = set(
        session.scalars(
            sa.select(NoteChunk.text).where(
                NoteChunk.athlete_id == athlete.id, NoteChunk.is_synthetic.is_(True)
            )
        )
    )
    pending = [(offset, text) for offset, text in DEMO_NOTES if text not in existing]
    if not pending:
        return 0

    # Embedded in one call per batch rather than one per note: the embedder
    # batches at 32, so this is three requests instead of forty.
    vectors = embedder.embed_documents([text for _, text in pending])
    for (offset, text), vector in zip(pending, vectors, strict=True):
        session.add(
            NoteChunk(
                athlete_id=athlete.id,
                # No log behind them, which is also how `reindex` knows to
                # re-embed these in place rather than rebuild them from a note.
                log_id=None,
                lift_codes=notes_module.detect_lifts(text),
                text=text,
                noted_on=BLOCK_START + dt.timedelta(days=offset),
                embedding=vector,
                is_synthetic=True,
            )
        )
    session.flush()
    return len(pending)


def clear_demo_notes(session: Session, athlete: Athlete) -> int:
    """Delete only synthetic chunks. A real logged note is never touched."""
    result = session.execute(
        sa.delete(NoteChunk).where(
            NoteChunk.athlete_id == athlete.id, NoteChunk.is_synthetic.is_(True)
        )
    )
    return result.rowcount or 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed SYNTHETIC training notes.")
    parser.add_argument(
        "--clear", action="store_true", help="Delete the synthetic notes instead of adding them."
    )
    parser.add_argument("--athlete-name", default=ATHLETE_NAME)
    args = parser.parse_args()

    if not ping_db():
        print("Database unreachable. Run `make infra` first.", file=sys.stderr)
        return 1

    try:
        with Session(engine) as session:
            athlete = session.exec(select(Athlete).where(Athlete.name == args.athlete_name)).first()
            if athlete is None:
                print(
                    f"No athlete named {args.athlete_name!r}. Run `make seed` first.",
                    file=sys.stderr,
                )
                return 1

            if args.clear:
                removed = clear_demo_notes(session, athlete)
                session.commit()
                print(f"Deleted {removed} synthetic note chunk(s).")
                return 0

            try:
                written = seed_demo_notes(session, athlete, get_embedder())
            except LlmUnavailable as exc:
                # The notes cannot be stored without vectors: `embedding` is
                # NOT NULL, by design (ADR-0002).
                print(f"\n{exc}", file=sys.stderr)
                print("Notes are embedded as they are written, so none were.", file=sys.stderr)
                return 1
            session.commit()
            total = session.scalar(
                sa.select(sa.func.count())
                .select_from(NoteChunk)
                .where(NoteChunk.athlete_id == athlete.id)
            )
    except ProgrammingError:
        print("Schema is missing. Run `make migrate` first.", file=sys.stderr)
        return 1

    print(f"Wrote {written} synthetic note chunk(s) of {len(DEMO_NOTES)}.")
    print(f"{args.athlete_name!r} now has {total} note chunk(s) in total.")
    print(
        "These are SYNTHETIC test data, flagged `is_synthetic`. Search hides them "
        "unless you pass include_synthetic=true, and pain screening ignores them "
        "entirely so a seeded note cannot block a real session."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
