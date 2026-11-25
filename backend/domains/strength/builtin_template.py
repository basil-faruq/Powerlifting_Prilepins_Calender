"""The block pattern that ships with the app, so nobody needs a spreadsheet.

`templates/b1.json` holds the authored 11-week program as a pattern only: for
each lift, week and slot, the sets, reps and the intensity as its exact
authored fraction. It carries no weight and no max -- a weight is always the
athlete's own 1RM times the fraction, computed by the generator -- so the file
is the same for everyone and the numbers come from whoever enters their 1RMs.

It was taken from the committed golden fixture, and a test keeps the two in
step entry for entry. Importing a workbook (`importer.import_template`,
`POST /templates/import`) remains the way to use a different program.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

from sqlmodel import Session, select

from domains.strength.importer import persist_template
from domains.strength.models import BlockTemplate
from domains.strength.schemas import ParsedEntry, ParsedTemplate

BUILTIN_PATH = Path(__file__).resolve().parent / "templates" / "b1.json"


def load_builtin_template(path: Path = BUILTIN_PATH) -> ParsedTemplate:
    """Read the shipped pattern into the same shape the workbook importer produces.

    Fractions are read from strings straight into `Decimal`, never through a
    float, so `112.5/105` arrives exactly as authored.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = tuple(
        ParsedEntry(
            lift_code=row["lift"],
            variation=row["variation"],
            week=row["week"],
            slot=row["slot"],
            sets=row["sets"],
            reps=row["reps"],
            pct_num=Decimal(row["pct_num"]),
            pct_den=Decimal(row["pct_den"]),
            is_test=row["is_test"],
            # Provenance for error messages: the file and the entry's position.
            source_sheet=path.name,
            source_row=index + 1,
        )
        for index, row in enumerate(raw["entries"])
    )
    return ParsedTemplate(
        name=raw["name"],
        weeks=raw["weeks"],
        slots=raw["slots"],
        overreach_from_week=raw["overreach_from_week"],
        # None marks it as built in; an imported workbook records its filename.
        source_file=None,
        entries=entries,
    )


def ensure_builtin_template(session: Session) -> uuid.UUID | None:
    """Write the built-in template unless one of that name exists. Safe to call twice.

    Returns the new template's id, or None when nothing was written. An existing
    template of the same name is left untouched, whether it came from here or
    from an imported workbook: a later import replaces it, a restart never does.
    Flushes, never commits, like `persist_template`.
    """
    parsed = load_builtin_template()
    existing = session.exec(select(BlockTemplate).where(BlockTemplate.name == parsed.name)).first()
    if existing is not None:
        return None
    return persist_template(session, parsed)
