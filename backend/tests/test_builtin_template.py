"""The template that ships with the app, so nobody needs the spreadsheet.

Offline tests pin the file to the golden fixture, so the program everyone gets
is exactly the one the 77-weight golden test verifies. Database tests check
that loading it is idempotent and never overwrites a template already there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlmodel import Session, select

from domains.strength import builtin_template, enums, models
from tests import golden

PATTERN_KEYS = (
    "lift_code",
    "variation",
    "week",
    "slot",
    "sets",
    "reps",
    "pct_num",
    "pct_den",
    "is_test",
)


def _key(entry) -> tuple:
    return (entry.lift_code, entry.week, entry.slot)


def _pattern(entry) -> tuple:
    return tuple(getattr(entry, name) for name in PATTERN_KEYS)


# --- Offline ----------------------------------------------------------------


def test_builtin_template_matches_the_golden_fixture_entry_for_entry():
    shipped = sorted(builtin_template.load_builtin_template().entries, key=_key)
    oracle = sorted(golden.entries_from(golden.load_rows()), key=_key)

    assert len(shipped) == 77
    assert [_pattern(e) for e in shipped] == [_pattern(e) for e in oracle]


def test_builtin_template_has_the_blocks_shape_and_four_tests():
    parsed = builtin_template.load_builtin_template()

    assert parsed.name == "B1"
    assert (parsed.weeks, parsed.slots, parsed.overreach_from_week) == (
        enums.TEMPLATE_WEEKS,
        enums.TEMPLATE_SLOTS,
        enums.OVERREACH_FROM_WEEK,
    )
    assert sum(1 for e in parsed.entries if e.is_test) == 4
    assert {e.lift_code for e in parsed.entries} == set(enums.LIFT_CODES)
    # Built in, not imported: no filename to report.
    assert parsed.source_file is None


def test_builtin_template_keeps_the_authored_fractions_unreduced():
    """`112.5/105` must arrive as 112.5 and 105, not as a float or reduced pair."""
    bench_test = next(
        e
        for e in builtin_template.load_builtin_template().entries
        if e.lift_code == "bench" and e.is_test
    )
    assert (bench_test.pct_num, bench_test.pct_den) == (Decimal("112.5"), Decimal("105"))


def test_builtin_template_file_carries_no_weights_or_maxes():
    """A pattern only: every weight comes from the athlete's own 1RM."""
    text = builtin_template.BUILTIN_PATH.read_text(encoding="utf-8")
    assert "weight" not in text
    assert "one_rm" not in text


# --- Database -----------------------------------------------------------------


def test_ensure_builtin_template_writes_it_once(db_session: Session):
    existing = db_session.exec(
        select(models.BlockTemplate).where(models.BlockTemplate.name == "B1")
    ).first()
    if existing is not None:
        pytest.skip("the test database already holds a template named B1")

    first = builtin_template.ensure_builtin_template(db_session)
    second = builtin_template.ensure_builtin_template(db_session)

    assert first is not None
    assert second is None
    rows = db_session.exec(
        select(models.TemplateEntry).where(models.TemplateEntry.template_id == first)
    ).all()
    assert len(rows) == 77
    assert sum(1 for row in rows if row.is_test) == 4


def test_ensure_builtin_template_leaves_an_existing_b1_alone(db_session: Session):
    """An imported workbook named B1 must survive every restart."""
    existing = db_session.exec(
        select(models.BlockTemplate).where(models.BlockTemplate.name == "B1")
    ).first()
    if existing is None:
        existing = models.BlockTemplate(
            name="B1", weeks=11, slots=3, overreach_from_week=7, source_file="mine.xlsx"
        )
        db_session.add(existing)
        db_session.flush()
    before = (existing.id, existing.source_file)

    assert builtin_template.ensure_builtin_template(db_session) is None

    db_session.refresh(existing)
    assert (existing.id, existing.source_file) == before


def test_ensure_builtin_template_writes_no_one_rep_max_rows(db_session: Session):
    before = len(db_session.exec(select(models.OneRepMax)).all())
    builtin_template.ensure_builtin_template(db_session)
    db_session.flush()
    assert len(db_session.exec(select(models.OneRepMax)).all()) == before
