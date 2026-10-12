"""Seed synthetic 1RMs so the pipeline can be exercised locally.

**This writes test data. It is not a way to enter your maxes.**

The planner cannot generate a block, compute a weight or simulate anything
without a 1RM, and the authored spreadsheet's maxes are examples rather than
measurements (ADR-0003), so a fresh database has no input at all. This fills
that gap for development and debugging only.

Every row it writes is marked twice and cannot be mistaken for real:

* `is_synthetic = true` — the authoritative "is this a real measurement?",
  which every consumer filters on and the UI must surface a banner for;
* `source = 'synthetic'` — identifying this script specifically as the writer.

A database CHECK makes the two unable to disagree. Clear them with `--clear`,
which deletes only synthetic rows and leaves real ones untouched.

Real 1RMs arrive through `PUT /athletes/{id}/one-rep-maxes` (spec 06) and the
setup wizard (spec 17), always with the date the athlete actually tested them.
Nothing in this file is on that path.

    uv run python -m scripts.seed_dev_one_rms                # plausible defaults
    uv run python -m scripts.seed_dev_one_rms --random        # varied, seeded
    uv run python -m scripts.seed_dev_one_rms --kg squat=150  # override one lift
    uv run python -m scripts.seed_dev_one_rms --clear         # remove them again
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from decimal import Decimal

from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session, select

from domains.strength.enums import LIFT_CODES, OneRepMaxSource
from domains.strength.models import Athlete, OneRepMax
from planner_core.db import engine, ping_db
from scripts.seed_athlete import ATHLETE_NAME

SOURCE: OneRepMaxSource = "synthetic"

# Deliberately NOT the spreadsheet's 160/140/105/70/105. Those are the example
# values ADR-0003 removed, and reusing them here would recreate exactly the
# confusion that change was made to end: a reader seeing them in the database
# could not tell whether they came from this script or from the athlete.
#
# Round numbers, plausibly ordered, and obviously placeholder.
DEFAULT_KG: dict[str, Decimal] = {
    "deadlift": Decimal("150"),
    "squat": Decimal("130"),
    "bench": Decimal("100"),
    "press": Decimal("60"),
    "row": Decimal("90"),
}

# A fixed Monday rather than `date.today()`: determinism keeps repeated runs a
# no-op and keeps anything built on these rows reproducible. Unlike spec 02's
# old placeholder this is safe to hard-code, because the row says it is
# synthetic -- the original problem was never the invented date, it was that
# the invented date was indistinguishable from a real one.
DEFAULT_SET_ON = dt.date(2026, 3, 2)

# --random stays reproducible unless asked otherwise, so a bug found with
# generated numbers can be reproduced.
DEFAULT_SEED = 0
RANDOM_SPREAD = Decimal("0.25")
RANDOM_STEP = Decimal("2.5")


def _random_kg(rng: random.Random, base: Decimal) -> Decimal:
    """A plausible max within +/-25% of `base`, on a 2.5 kg step."""
    low = base * (1 - RANDOM_SPREAD)
    high = base * (1 + RANDOM_SPREAD)
    steps = rng.randint(int(low / RANDOM_STEP), int(high / RANDOM_STEP))
    return RANDOM_STEP * steps


def seed_dev_one_rms(
    session: Session,
    *,
    athlete: Athlete,
    kg: dict[str, Decimal],
    set_on: dt.date = DEFAULT_SET_ON,
) -> list[OneRepMax]:
    """Insert synthetic 1RMs for `athlete`. Safe to call twice.

    Idempotent on `(athlete_id, lift_code, set_on)` by selecting first, the
    same way `seed_athlete.seed` does and for the same reason: `one_rep_max`
    carries no unique constraint to `ON CONFLICT` against.

    Flushes rather than commits, so the caller owns the transaction.
    """
    written: list[OneRepMax] = []
    for lift_code, value in sorted(kg.items()):
        existing = session.exec(
            select(OneRepMax).where(
                OneRepMax.athlete_id == athlete.id,
                OneRepMax.lift_code == lift_code,
                OneRepMax.set_on == set_on,
            )
        ).first()
        if existing is not None:
            continue
        row = OneRepMax(
            athlete_id=athlete.id,
            lift_code=lift_code,
            kg=value,
            set_on=set_on,
            source=SOURCE,
            is_synthetic=True,
        )
        session.add(row)
        written.append(row)
    session.flush()
    return written


def clear_dev_one_rms(session: Session) -> int:
    """Delete every synthetic 1RM. Real rows are never touched.

    Selects on `is_synthetic` rather than on `source`, because that column is
    the authoritative flag -- a row faked with `source = 'tested'` to exercise
    spec 06's test-completion path is synthetic too, and must also go.
    """
    rows = session.exec(select(OneRepMax).where(OneRepMax.is_synthetic)).all()
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def _parse_kg(pairs: list[str] | None, *, random_kg: bool, seed: int) -> dict[str, Decimal]:
    rng = random.Random(seed)
    kg = {
        code: (_random_kg(rng, DEFAULT_KG[code]) if random_kg else DEFAULT_KG[code])
        for code in sorted(DEFAULT_KG)
    }
    for pair in pairs or []:
        lift, _, raw = pair.partition("=")
        if not _:
            raise argparse.ArgumentTypeError(f"--kg expects lift=value, got {pair!r}")
        if lift not in LIFT_CODES:
            raise argparse.ArgumentTypeError(
                f"--kg: {lift!r} is not a lift ({', '.join(sorted(LIFT_CODES))})"
            )
        value = Decimal(raw)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"--kg: {lift} must be positive, got {raw}")
        kg[lift] = value
    return kg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clear",
        action="store_true",
        help="delete every synthetic 1RM instead of writing any; real rows are kept",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="vary the values within +/-25%% on a 2.5 kg step, instead of the fixed defaults",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"seed for --random, so a run is reproducible (default {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--kg",
        action="append",
        metavar="LIFT=KG",
        help="override one lift, repeatable (e.g. --kg squat=150 --kg bench=102.5)",
    )
    parser.add_argument(
        "--set-on",
        type=dt.date.fromisoformat,
        default=DEFAULT_SET_ON,
        metavar="YYYY-MM-DD",
        help=f"the (fictional) date to record (default {DEFAULT_SET_ON.isoformat()})",
    )
    parser.add_argument(
        "--athlete-name",
        default=ATHLETE_NAME,
        help=f"whose maxes to seed (default {ATHLETE_NAME!r})",
    )
    args = parser.parse_args()

    try:
        kg = _parse_kg(args.kg, random_kg=args.random, seed=args.seed)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    if not ping_db():
        print("Postgres is not reachable. Start it with `make infra`.", file=sys.stderr)
        return 1

    try:
        with Session(engine) as session:
            if args.clear:
                removed = clear_dev_one_rms(session)
                session.commit()
                print(f"Removed {removed} synthetic 1RM row(s). Real rows untouched.")
                return 0

            athlete = session.exec(select(Athlete).where(Athlete.name == args.athlete_name)).first()
            if athlete is None:
                print(
                    f"No athlete named {args.athlete_name!r}. Run `make seed` first.",
                    file=sys.stderr,
                )
                return 1

            written = seed_dev_one_rms(session, athlete=athlete, kg=kg, set_on=args.set_on)
            session.commit()
            total = len(
                session.exec(select(OneRepMax).where(OneRepMax.athlete_id == athlete.id)).all()
            )
    except ProgrammingError:
        print("Schema is missing. Run `make migrate` first.", file=sys.stderr)
        return 1

    shown = ", ".join(f"{code} {kg[code]}" for code in sorted(kg))
    print(f"Wrote {len(written)} synthetic 1RM row(s) dated {args.set_on.isoformat()}: {shown}")
    print(f"{args.athlete_name!r} now has {total} 1RM row(s) in total.")
    print(
        "These are SYNTHETIC test data, flagged `is_synthetic`. "
        "Remove them with `--clear` before trusting anything the planner says."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
