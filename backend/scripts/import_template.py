"""Import the authored spreadsheet as a block template.

Run with `make import`, or from `backend/`:

    uv run python -m scripts.import_template ../data/B1_Mine.xlsx

Only the block's pattern is imported: which lift trains on which day of which
week, for how many sets and reps, at what intensity. Never the weights Excel
computed, and never the workbook's `1RM` sheet -- those maxes are examples.
The athlete's own 1RMs, and the dates they tested them, are entered in the app
and are the only ones the planner ever uses.

So this writes `block_template` and `template_entry`, and nothing else.

Re-importing the same name fails unless `--replace` is passed, which swaps the
entries while keeping the template's id.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session, select

from domains.strength.importer import (
    TemplateExistsError,
    TemplateImportError,
    import_template,
)
from domains.strength.models import TemplateEntry
from planner_core.db import engine, ping_db

DEFAULT_NAME = "B1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, metavar="PATH", help="the .xlsx workbook to import")
    parser.add_argument(
        "--name",
        default=DEFAULT_NAME,
        help=f"template name to write (default {DEFAULT_NAME})",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing template of this name, keeping its id",
    )
    args = parser.parse_args()

    # Checked before ping_db, so a mistyped path is diagnosable without
    # Postgres running.
    if not args.path.is_file():
        print(f"{args.path} does not exist.", file=sys.stderr)
        return 1

    if not ping_db():
        print("Postgres is not reachable. Start it with `make infra`.", file=sys.stderr)
        return 1

    try:
        with Session(engine) as session:
            template_id = import_template(
                session,
                args.path,
                name=args.name,
                replace=args.replace,
            )
            session.commit()
            # Re-read after the commit rather than returning counts from
            # import_template: it keeps that function's signature as the spec
            # states it, and turns this line into a real post-condition check
            # against the database instead of an echo of what was intended.
            rows = session.exec(
                select(TemplateEntry).where(TemplateEntry.template_id == template_id)
            ).all()
    except (TemplateImportError, TemplateExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ProgrammingError:
        # Relation does not exist: the schema has not been migrated yet.
        print("Schema is missing. Run `make migrate` first.", file=sys.stderr)
        return 1

    tests = sum(1 for row in rows if row.is_test)
    print(f"Template {args.name!r} ready: {len(rows)} entries, {tests} tests (id {template_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
