"""Load the built-in block template, so the app works without a spreadsheet.

Run with `make seed`, or `uv run python -m scripts.seed_template` from
`backend/`. The containerised api runs it on every start, right after the
migrations. Idempotent: if a template named B1 already exists -- built in or
imported from a workbook -- nothing is written.

No 1RM is written here. The template is a pattern; the athlete enters their own
maxes, with the date they tested them, in the app.
"""

from __future__ import annotations

import sys

from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session

from domains.strength.builtin_template import ensure_builtin_template
from planner_core.db import engine, ping_db


def main() -> int:
    if not ping_db():
        print("Postgres is not reachable. Start it with `make infra`.", file=sys.stderr)
        return 1

    try:
        with Session(engine) as session:
            created = ensure_builtin_template(session)
            session.commit()
    except ProgrammingError:
        print("Schema is missing. Run `make migrate` first.", file=sys.stderr)
        return 1

    if created is None:
        print("Template B1 already present; left as it is.")
    else:
        print("Built-in template B1 loaded: 77 entries, 11 weeks, 3 days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
