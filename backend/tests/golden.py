"""The authored block as a test input, shared by the generator and validator suites.

Built from the **committed fixture** rather than the gitignored workbook, so
everything here runs on a fresh clone (ADR-0005 modification 15). The example
maxes (160/140/105/70/105) reach a test only through this module, as the
fixture's `one_rm_kg` -- every other test uses different numbers, so a reader
can always tell which block a weight came from (CLAUDE.md guardrail).

`conftest.py` exposes these as session-scoped fixtures; the constants are
imported directly where a test needs them.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from domains.strength import generator, models
from domains.strength.schemas import BlockDraft, ParsedEntry

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "b1_expected_weights.json"

# The author's own layout: Day 1 Monday, Day 2 Wednesday, Day 3 Friday.
MON_WED_FRI = {1: 0, 2: 2, 3: 4}

# A Monday, as `block.start_date` requires.
START = dt.date(2026, 1, 5)


def athlete(increment: str = "1.0", rounding: str = "floor") -> models.Athlete:
    """An athlete, unsaved. The generator only reads three fields."""
    return models.Athlete(
        name="test-athlete",
        plate_increment_kg=Decimal(increment),
        rounding=rounding,
    )


def template(weeks: int = 11, slots: int = 3) -> models.BlockTemplate:
    return models.BlockTemplate(name="B1-test", weeks=weeks, slots=slots, overreach_from_week=7)


def load_rows() -> list[dict]:
    """The committed oracle. No skip: the file is version-controlled."""
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def entries_from(rows: list[dict]) -> tuple[ParsedEntry, ...]:
    """The fixture's 77 rows as template entries the generator can consume."""
    return tuple(
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
            source_sheet=f"B1 {row['lift'].title()}",
            source_row=index + 2,
        )
        for index, row in enumerate(rows)
    )


def maxes_from(rows: list[dict]) -> dict[str, Decimal]:
    """The example maxes Excel computed the fixture's weights from.

    Read from the fixture rather than typed in, so the inputs cannot drift from
    the outputs they produced.
    """
    return {row["lift"]: Decimal(row["one_rm_kg"]) for row in rows}


def draft_from(entries: tuple[ParsedEntry, ...], maxes: dict[str, Decimal]) -> BlockDraft:
    """The authored block, generated with the spreadsheet's own settings."""
    return generator.generate_block(
        athlete("1.0", "floor"),
        template(),
        entries,
        maxes,
        sorted(maxes),
        START,
        MON_WED_FRI,
    )
