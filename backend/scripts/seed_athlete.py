"""Create the single athlete.

Run with `make seed`, or `uv run python -m scripts.seed_athlete` from
`backend/`. Idempotent: running it twice leaves one athlete.

No 1RMs are seeded, and none are imported from the spreadsheet either. The
maxes in `B1_Mine.xlsx` are examples; the app only ever uses 1RMs the athlete
enters themselves, together with the date they actually tested them. Only the
*pattern* of the block (sets, reps, intensity) comes from the workbook.

`seed()` is split from `main()` so the idempotency can be tested against a
transaction that is rolled back, without a subprocess.
"""

from __future__ import annotations

import sys

from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session, select

from domains.strength.models import Athlete
from planner_core.db import engine, ping_db

ATHLETE_NAME = "Basil"


def seed(session: Session) -> Athlete:
    """Ensure the athlete exists. Safe to call twice.

    This is a single-user local script with no concurrency, so SELECT-then-insert
    is enough: the race that would normally rule that pattern out cannot arise.
    """
    athlete = session.exec(select(Athlete).where(Athlete.name == ATHLETE_NAME)).first()
    if athlete is None:
        # plate_increment_kg, rounding and timezone all take their model
        # defaults: 1.00 kg, floor, and the configured timezone.
        athlete = Athlete(name=ATHLETE_NAME)
        session.add(athlete)
        # Flush rather than commit, so the caller controls the transaction and a
        # test can roll the whole thing back.
        session.flush()
    return athlete


def main() -> int:
    # Checked up front so an unreachable database is one actionable line rather
    # than a traceback after a 2 s hang.
    if not ping_db():
        print(
            "Postgres is not reachable. Start it with `make infra`.",
            file=sys.stderr,
        )
        return 1

    try:
        with Session(engine) as session:
            athlete = seed(session)
            session.commit()
            # Read inside the block: `commit()` expires every attribute, so
            # touching `athlete.name` after the session closes raises
            # DetachedInstanceError rather than reloading it.
            name = athlete.name
    except ProgrammingError:
        # Relation does not exist: the schema has not been migrated yet.
        print("Schema is missing. Run `make migrate` first.", file=sys.stderr)
        return 1

    print(f"Athlete {name!r} ready. Enter your 1RMs and test dates in the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
