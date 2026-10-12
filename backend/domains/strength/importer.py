"""Read `data/B1_Mine.xlsx` into a block template.

The workbook is the historical artefact every later spec derives from, and it
is treated as strictly read-only source material (CLAUDE.md guardrail). Two
authoring errors are worked around here rather than fixed upstream: the
`Wide Grp` typo in `B1 Bench` W11 Day 3, and the bare literal `0.5` in
`B1 Squat` W11 Day 3 where every other intensity is a formula.

What this module does **not** do is as important as what it does:

* **It never reads the workbook's `1RM` sheet.** Those maxes, and the dates
  behind them, are examples. Only 1RMs the athlete enters themselves -- with
  the date they actually tested them -- ever reach `one_rep_max`. What the
  workbook contributes is the *pattern* of the block and nothing else: which
  lift trains on which day of which week, for how many sets and reps, at what
  intensity. The importer therefore writes `block_template` and
  `template_entry`, and no other table.
* It never imports the computed `Weight` column into the template. Only sets,
  reps and the authored `%` fraction are stored, so the template works for any
  future 1RM (CLAUDE.md guardrail). `Weight` is read in exactly one place,
  `scripts/build_golden_fixture.py`, to capture Excel's own answers as the
  oracle spec 04 is measured against.
* It never treats the denominator as a 1RM. In the authored file the squat
  rows divide by 120 while that sheet's example max is 140, and bench week 11
  divides by 125 against 105. The denominator is an independent authored datum
  -- the target max the block was programmed against -- so it is neither
  checked against anything nor mistaken for the athlete's real max. Spec 04
  computes a weight from the athlete's own 1RM and this fraction; `pct_den`
  never stands in for it.
* It never judges whether an intensity is sensible. Spec 05's validator owns
  ceilings, Prilepin ranges and the volume trend, and CLAUDE.md #5 is explicit
  that a rule rejecting the source template is miscalibrated (the squat test is
  130/120, about 108%). This module is the most permissive component in the
  system about *values* and the strictest about *structure*.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import sqlalchemy as sa
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import ValidationError
from sqlmodel import Session, select

from domains.strength import enums
from domains.strength.models import Block, BlockTemplate, Lift, TemplateEntry
from domains.strength.schemas import ParsedEntry, ParsedTemplate


class TemplateImportError(Exception):
    """The workbook cannot be read, or its contents are not a coherent template."""


class TemplateExistsError(Exception):
    """The target template name is taken and cannot, or may not, be overwritten.

    Deliberately *not* a subclass of TemplateImportError. The causes differ --
    a bad file versus existing database state -- and spec 06 maps them to
    different status codes, so subclassing would let `except
    TemplateImportError` silently swallow a conflict.
    """


# --- What the workbook looks like -----------------------------------------
# Sheet names are a module constant rather than derived from the template
# `name`, because `f"{name} {label}"` would make `--name B2` find no sheets at
# all: the sheet names belong to the file, the template name belongs to the
# database row.
#
# A sheet's lift comes from the sheet name and nothing else. `B1 Press!C1`
# reads 'Bench 1RM' (a copy-paste artefact) and `B1 Press!A3` reads 'Strict
# Press', so neither the label nor the title band can be trusted. This table
# makes that structural instead of a comment someone can miss.
LIFT_SHEETS: tuple[tuple[str, enums.LiftCode], ...] = (
    ("B1 Deadlift", "deadlift"),
    ("B1 Squat", "squat"),
    ("B1 Bench", "bench"),
    ("B1 Press", "press"),
    ("B1 Row", "row"),
)

WEEK_HEADER = "Week"
REQUIRED_HEADERS: tuple[str, ...] = ("Week", "Day", "Sets", "Reps", "Intensity")
# Scanning is bounded because `ws.max_row` is inflated by style-only trailing
# rows: 35 on `B1 Deadlift` where the data ends at 15, 33 on `B1 Squat` where
# it ends at 26.
HEADER_SCAN_ROWS = 20

# An alias table, never a fuzzy match: `Wide Grp` is here because the file
# contains it (B1 Bench W11 Day 3) and for no other reason. `Wde Grip` must
# still fail loudly rather than be guessed at.
_VARIATION_ALIASES: dict[str, enums.Variation] = {
    "close grip": "close_grip",
    "wide grip": "wide_grip",
    "wide grp": "wide_grip",
}

# `=70/120`, `=112.5/105`. Anything with a second operator, a function call or
# a cell reference is rejected rather than evaluated -- a spreadsheet formula
# is never handed to eval().
_FRACTION_FORMULA = re.compile(r"\A=\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*\Z")


# --- Small helpers --------------------------------------------------------


def _at(ref: str | None) -> str:
    """`"B1 Squat!F26: "` or `""`, so one message template serves both callers."""
    return f"{ref}: " if ref else ""


def _ref(cell) -> str:
    """An A1-style reference a human can paste into Excel's name box."""
    return f"{cell.parent.title}!{cell.coordinate}"


def _row_ref(entry: ParsedEntry) -> str:
    return f"{entry.source_sheet}!row {entry.source_row}"


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _exact_pair(value: Decimal) -> tuple[Decimal, Decimal]:
    """A Decimal as its reduced numerator and denominator, losslessly."""
    fraction = Fraction(value)
    return Decimal(fraction.numerator), Decimal(fraction.denominator)


def _as_int(value: object, *, ref: str | None, field: str) -> int:
    """A whole number from a cell that openpyxl hands back as `6.0`.

    Every numeric cell in the workbook is a float, so a plain `int(value)`
    would also silently accept `2.5` as 2. Requiring integrality turns that
    into `B1 Squat!C9: sets 2.5 is not a whole number`.
    """
    # Before the int branch: bool is a subclass of int, so True would pass as 1.
    if isinstance(value, bool):
        raise TemplateImportError(f"{_at(ref)}{field} is a boolean ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        try:
            whole = int(value)
        except (ValueError, OverflowError, InvalidOperation):
            raise TemplateImportError(
                f"{_at(ref)}{field} {value!r} is not a whole number"
            ) from None
        if value != whole:
            raise TemplateImportError(f"{_at(ref)}{field} {value} is not a whole number")
        return whole
    if isinstance(value, str):
        try:
            decimal = Decimal(value.strip())
        except InvalidOperation:
            raise TemplateImportError(f"{_at(ref)}{field} {value!r} is not a number") from None
        return _as_int(decimal, ref=ref, field=field)
    raise TemplateImportError(f"{_at(ref)}{field} is a {type(value).__name__} ({value!r})")


def _as_decimal(value: object, *, ref: str | None, field: str) -> Decimal:
    if isinstance(value, bool):
        raise TemplateImportError(f"{_at(ref)}{field} is a boolean ({value!r})")
    if isinstance(value, (int, float, Decimal, str)):
        try:
            # str() first, always: Decimal(0.1) is
            # 0.1000000000000000055511151231257827, Decimal("0.1") is 0.1.
            return Decimal(str(value).strip())
        except InvalidOperation:
            raise TemplateImportError(f"{_at(ref)}{field} {value!r} is not a number") from None
    raise TemplateImportError(f"{_at(ref)}{field} is a {type(value).__name__} ({value!r})")


# --- Reading one sheet's shape --------------------------------------------


@dataclass(frozen=True, slots=True)
class SheetLayout:
    """Where a lift sheet keeps its columns.

    A dataclass rather than a Pydantic model, which is a deliberate exception
    to the project's "Pydantic v2 for every data shape" convention: this is a
    parser coordinate that is never serialised and never crosses an API
    boundary, and keeping `openpyxl` out of `schemas.py` matters because spec
    06 imports that module for its response models.
    """

    sheet_name: str
    lift_code: str
    header_row: int
    columns: dict[str, int]


def _find_header_row(ws: Worksheet) -> int:
    """The row whose column A reads 'Week'.

    Matching the *text* is load-bearing. Column A holds 'Block' at row 1 and a
    merged title band at row 3 -- which on `B1 Press` reads 'Strict Press' --
    so a "first non-empty cell in column A" implementation lands on row 1 in
    all five sheets and every column index comes out wrong.
    """
    limit = min(ws.max_row, HEADER_SCAN_ROWS)
    for row in range(1, limit + 1):
        value = ws.cell(row=row, column=1).value
        if isinstance(value, str) and value.strip().casefold() == WEEK_HEADER.casefold():
            return row
    raise TemplateImportError(
        f"sheet {ws.title!r}: no {WEEK_HEADER!r} cell in column A within the first "
        f"{HEADER_SCAN_ROWS} rows"
    )


def sheet_layout(ws: Worksheet, lift_code: str) -> SheetLayout:
    """Map header text to a column index, so position never matters.

    This is spec 03 §11's rule made structural. `B1 Bench` inserts `Variation`
    at column C and shifts six columns right by one; because nothing here reads
    a fixed offset, that shift cannot move a value into the wrong field. For
    the same reason `Rel. Intensity` and `Tonnage (kg)` are ignored by
    construction rather than by discipline -- they are never named.
    """
    header_row = _find_header_row(ws)
    columns: dict[str, int] = {}
    for cell in ws[header_row]:
        if _is_blank(cell.value):
            continue
        text = str(cell.value).strip()
        if text in columns:
            raise TemplateImportError(
                f"sheet {ws.title!r}: header {text!r} appears twice, in columns "
                f"{get_column_letter(columns[text])} and {cell.column_letter}"
            )
        columns[text] = cell.column
    missing = [header for header in REQUIRED_HEADERS if header not in columns]
    if missing:
        # Listing what *was* found is what makes this debuggable in seconds.
        # A merged header cell yields None for its trailing columns and so
        # lands here too, which is the right failure.
        raise TemplateImportError(
            f"sheet {ws.title!r}: no {', '.join(repr(h) for h in missing)} column in "
            f"header row {header_row} (found: {', '.join(sorted(columns))})"
        )
    return SheetLayout(
        sheet_name=ws.title,
        lift_code=lift_code,
        header_row=header_row,
        columns=columns,
    )


# --- Cell-level parsing ---------------------------------------------------


def parse_intensity(value: object, *, ref: str | None = None) -> tuple[Decimal, Decimal]:
    """The authored intensity as an exact numerator/denominator pair.

    `"=70/120"` -> `(70, 120)`, `"=112.5/105"` -> `(112.5, 105)`, the bare
    literal `0.5` -> `(1, 2)`.

    The asymmetry is intentional. A formula keeps its **literal** pair rather
    than a reduced one, because `70/120` records that the author programmed
    70 kg against a 120 kg reference, and that reference is not the 1RM -- so
    reducing to `7/12` destroys an independent authored datum irreversibly.
    Arithmetic is indifferent either way (`Fraction(70)/Fraction(120)` equals
    `Fraction(7)/Fraction(12)` exactly), so fidelity decides. A bare number has
    no authored pair to preserve, so its canonical exact value is stored.

    The consequence: two equal intensities may hold unequal pairs, so compare
    through `ParsedEntry.pct`, never through the tuple.

    `ref` is keyword-only with a default so this stays usable as a unit, while
    still being able to satisfy spec §13's "error naming sheet + cell".
    """
    # `D1` on all five lift sheets is an ArrayFormula object, not a str, so the
    # obvious `value.startswith("=")` raises AttributeError instead of saying
    # anything useful. No intensity cell is array-entered in *this* file, so
    # this branch is insurance -- but it turns a traceback into a named cell.
    if isinstance(value, ArrayFormula):
        value = value.text

    # Before the numeric branch: bool is a subclass of int, so True would
    # otherwise parse as the intensity 1/1.
    if isinstance(value, bool):
        raise TemplateImportError(f"{_at(ref)}intensity is a boolean ({value!r})")
    if value is None:
        raise TemplateImportError(f"{_at(ref)}intensity is empty")

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TemplateImportError(f"{_at(ref)}intensity is empty")
        match = _FRACTION_FORMULA.fullmatch(text)
        if match is not None:
            num, den = Decimal(match[1]), Decimal(match[2])
        elif text.startswith("="):
            raise TemplateImportError(
                f"{_at(ref)}intensity formula {text!r} is not a plain "
                "numerator/denominator division"
            )
        else:
            num, den = _exact_pair(_as_decimal(text, ref=ref, field="intensity"))
    elif isinstance(value, (int, float, Decimal)):
        num, den = _exact_pair(_as_decimal(value, ref=ref, field="intensity"))
    else:
        raise TemplateImportError(
            f"{_at(ref)}cannot read intensity from a {type(value).__name__} ({value!r})"
        )

    if num <= 0 or den <= 0:
        raise TemplateImportError(f"{_at(ref)}intensity {num}/{den} is not positive")
    return num, den


def normalize_variation(value: object, *, ref: str | None = None) -> enums.Variation | None:
    """`Close Grip` -> `close_grip`, `Wide Grp` -> `wide_grip`, blank -> None."""
    if _is_blank(value):
        return None
    text = str(value).strip()
    # Collapses internal runs of whitespace too, so 'Wide  Grip' normalises.
    key = " ".join(text.casefold().split())
    try:
        return _VARIATION_ALIASES[key]
    except KeyError:
        raise TemplateImportError(f"{_at(ref)}unknown variation {text!r}") from None


# --- Reading the workbook -------------------------------------------------


def _parse_sheet(ws: Worksheet, lift_code: str) -> list[ParsedEntry]:
    layout = sheet_layout(ws, lift_code)
    columns = layout.columns
    entries: list[ParsedEntry] = []
    # Reset per sheet, so Squat's blank Day-3 weeks cannot bleed into Bench.
    carried_week: int | None = None

    for row in range(layout.header_row + 1, ws.max_row + 1):
        day_cell = ws.cell(row=row, column=columns["Day"])
        # This skip deliberately comes FIRST, before the week carry is
        # updated. A row with no `Day` is not a data row, so nothing on it may
        # influence the parse -- which keeps the style-only trailing rows inert
        # even though this workbook demonstrably holds stray orphans elsewhere
        # (`B1 Bench!G1` is a bare 9.0 under no header).
        if _is_blank(day_cell.value):
            continue

        week_cell = ws.cell(row=row, column=columns["Week"])
        if not _is_blank(week_cell.value):
            carried_week = _as_int(week_cell.value, ref=_ref(week_cell), field="week")
        if carried_week is None:
            raise TemplateImportError(f"{_ref(week_cell)}: a data row with no Week value above it")

        intensity_cell = ws.cell(row=row, column=columns["Intensity"])
        pct_num, pct_den = parse_intensity(intensity_cell.value, ref=_ref(intensity_cell))

        variation: enums.Variation | None = None
        if "Variation" in columns:
            variation_cell = ws.cell(row=row, column=columns["Variation"])
            variation = normalize_variation(variation_cell.value, ref=_ref(variation_cell))

        sets_cell = ws.cell(row=row, column=columns["Sets"])
        reps_cell = ws.cell(row=row, column=columns["Reps"])
        try:
            entries.append(
                ParsedEntry(
                    lift_code=lift_code,
                    variation=variation,
                    week=carried_week,
                    # `Day` is 1/2/3 throughout and maps straight to a slot.
                    slot=_as_int(day_cell.value, ref=_ref(day_cell), field="slot"),
                    sets=_as_int(sets_cell.value, ref=_ref(sets_cell), field="sets"),
                    reps=_as_int(reps_cell.value, ref=_ref(reps_cell), field="reps"),
                    pct_num=pct_num,
                    pct_den=pct_den,
                    source_sheet=ws.title,
                    source_row=row,
                )
            )
        except ValidationError as exc:
            # A nested ValidationError names a field and no place, so wrap it.
            raise TemplateImportError(f"{ws.title}!row {row}: {exc}") from exc

    if not entries:
        raise TemplateImportError(
            f"sheet {ws.title!r}: no data rows below the header in row {layout.header_row}"
        )
    return entries


def mark_tests(entries: Sequence[ParsedEntry]) -> list[ParsedEntry]:
    """Flag the max attempts: a single, above the reference max.

    Pure -- returns new entries and never mutates the input.

    `>` and not `>=` is the sharp edge here. The workbook holds several near
    misses that must not be flagged: press W8 is 2x1 at 70/70, squat W10 slot
    1 is 3x1 at 120/120, bench W10 slot 1 is 3x1 at 100/105. A single at
    exactly the max is a top single, not a new max.

    The comparison goes through `ParsedEntry.pct`, so it is exact Fraction
    arithmetic rather than a float division (CLAUDE.md #4).

    Spec 03 §5D says "exactly one test per lift is expected" and then, in its
    next clause, that row has none. The enforceable rule is therefore *at most*
    one per lift, and it lives in `check_template`.
    """
    return [
        entry.model_copy(update={"is_test": True})
        if (entry.sets == 1 and entry.reps == 1 and entry.pct > 1)
        else entry
        for entry in entries
    ]


def parse_workbook(
    path: Path,
    *,
    name: str = "B1",
    weeks: int = enums.TEMPLATE_WEEKS,
    slots: int = enums.TEMPLATE_SLOTS,
    overreach_from_week: int = enums.OVERREACH_FROM_WEEK,
) -> ParsedTemplate:
    """Read every lift sheet into one `ParsedTemplate`. No database involved.

    Opened with formulas rather than `data_only=True`: the authored fraction is
    the point, and the computed weight is deliberately not imported.

    `weeks` and `slots` come from `enums`, **not** from `max(week)` /
    `max(slot)` of the data. Deriving them would make `check_template`'s bound
    check a tautology, so a workbook with a fat-fingered week 110 would quietly
    produce a 110-week template instead of failing.
    """
    path = Path(path)
    # Not read_only=True: random access by row and column is the whole
    # strategy, and the inflated row counts are handled by skipping, not by
    # streaming.
    wb = load_workbook(path, data_only=False)
    try:
        entries: list[ParsedEntry] = []
        for sheet_name, lift_code in LIFT_SHEETS:
            if sheet_name not in wb.sheetnames:
                raise TemplateImportError(
                    f"sheet {sheet_name!r} not found; workbook has: {', '.join(wb.sheetnames)}"
                )
            entries.extend(_parse_sheet(wb[sheet_name], lift_code))
        # The workbook's `1RM` sheet is deliberately not read. Its maxes are
        # examples, and the athlete's own come from the app.
    finally:
        wb.close()

    return ParsedTemplate(
        name=name,
        weeks=weeks,
        slots=slots,
        overreach_from_week=overreach_from_week,
        # Basename only: the absolute path is machine-specific and the file is
        # private data.
        source_file=path.name,
        # Source order (sheet order, then row order) is already deterministic
        # and preserves provenance. Sorting is the golden fixture's job.
        entries=tuple(mark_tests(entries)),
    )


# --- Whole-template checks ------------------------------------------------


def check_template(parsed: ParsedTemplate) -> list[str]:
    """Universal invariants any coherent template must satisfy.

    Returns human-readable problems; empty means fine. Collecting rather than
    raising means one import run reports every bad row instead of the first.

    The split against the parse is: a cell-level failure raises immediately
    with the exact cell, because the parse cannot continue past it; a
    whole-template failure happens after every row already exists, so
    gathering is free.

    Three of these pre-empt a specific database error, which matters because
    an IntegrityError both reads badly and aborts the transaction, taking
    everything after it down too.

    `week <= weeks` living here is not taking spec 05's job. ADR-0002 rule 3
    calls it a validator rule because at the database the bound is in another
    row of another table; inside a `ParsedTemplate` it is same-object. Spec 05
    validates plans, not templates.
    """
    problems: list[str] = []
    if not parsed.entries:
        # The guard against a header misdetection that otherwise yields a
        # perfectly successful import of nothing.
        problems.append("the workbook produced no entries")

    seen: dict[tuple[str, int, int], ParsedEntry] = {}
    tests: dict[str, list[ParsedEntry]] = {}
    for entry in parsed.entries:
        if not 1 <= entry.week <= parsed.weeks:
            problems.append(
                f"{_row_ref(entry)}: week {entry.week} is outside weeks 1..{parsed.weeks}"
            )
        if not 1 <= entry.slot <= parsed.slots:
            problems.append(
                f"{_row_ref(entry)}: slot {entry.slot} is outside slots 1..{parsed.slots}"
            )
        first = seen.get(entry.key)
        if first is None:
            seen[entry.key] = entry
        else:
            # Pre-empts uq_template_entry_template_id_lift_code_week_slot, and
            # names both rows so the duplicate is findable.
            problems.append(
                f"{_row_ref(entry)} repeats {entry.lift_code} week {entry.week} "
                f"slot {entry.slot}, already at {_row_ref(first)}"
            )
        if entry.is_test:
            tests.setdefault(entry.lift_code, []).append(entry)

    for lift_code, rows in sorted(tests.items()):
        if len(rows) > 1:
            problems.append(
                f"{lift_code} has {len(rows)} test entries "
                f"({', '.join(_row_ref(row) for row in rows)}); at most one is expected"
            )
    return problems


def _variation_problems(session: Session, parsed: ParsedTemplate) -> list[str]:
    """Check each variation against the lift that is allowed to carry it.

    `ck_template_entry_variation_known` only restricts the value *set*, so the
    database accepts `close_grip` on `deadlift` without complaint. The
    authority on which lift may carry which variation is the seeded
    `lift.variations` JSONB, and this is exactly the class of invariant
    ADR-0002 found a `Literal` gives you nothing for.

    It needs a session, so it joins the problem list at the write boundary
    rather than during the parse.
    """
    declared = {lift.code: set(lift.variations or ()) for lift in session.exec(select(Lift)).all()}
    problems: list[str] = []
    for entry in parsed.entries:
        if entry.variation is None:
            continue
        allowed = declared.get(entry.lift_code)
        if allowed is None:
            # An unknown lift_code is the foreign key's to report.
            continue
        if entry.variation not in allowed:
            problems.append(
                f"{_row_ref(entry)}: {entry.lift_code} does not declare variation "
                f"{entry.variation!r} (lift.variations = {sorted(allowed)})"
            )
    return problems


# --- Writing ---------------------------------------------------------------


def persist_template(
    session: Session,
    parsed: ParsedTemplate,
    *,
    replace: bool = False,
) -> uuid.UUID:
    """Write a parsed template and its entries. Flushes, never commits.

    The caller owns the transaction, which is what lets a test run this
    against a transaction that is rolled back afterwards.

    Touches `block_template` and `template_entry` only. Nothing here writes
    `one_rep_max`: a max is the athlete's own measurement, entered with the
    date they tested it, and importing a block pattern is not an occasion to
    invent one.

    All-or-nothing (spec §11) follows from validating everything before the
    first `add`, and from never committing here.
    """
    problems = check_template(parsed)

    existing = session.exec(select(BlockTemplate).where(BlockTemplate.name == parsed.name)).first()
    if existing is not None:
        if not replace:
            raise TemplateExistsError(
                f"template {parsed.name!r} already exists (id {existing.id}); "
                "pass --replace to overwrite it"
            )
        # Checked up front, not left to the database. `block.template_id` has
        # no ON DELETE, so touching a referenced template raises
        # fk_block_template_id_block_template -- an unreadable message that
        # also poisons the transaction, so nothing after it can run.
        numbers = session.exec(select(Block.number).where(Block.template_id == existing.id)).all()
        if numbers:
            raise TemplateExistsError(
                f"template {parsed.name!r} is used by block(s) "
                f"{', '.join(str(number) for number in sorted(numbers))}; delete them "
                "first, or import under a different name"
            )

    problems += _variation_problems(session, parsed)
    if problems:
        raise TemplateImportError("\n".join(problems))

    if existing is None:
        template = BlockTemplate(
            name=parsed.name,
            weeks=parsed.weeks,
            slots=parsed.slots,
            overreach_from_week=parsed.overreach_from_week,
            source_file=parsed.source_file,
        )
        session.add(template)
    else:
        # The template row and its id survive a replace; only the entries are
        # swapped. Stable identity for spec 06's GET /templates/{id}, and it
        # does not depend on an ORM-level parent delete cascading through a
        # relationship that is not configured. The database's ON DELETE CASCADE
        # on template_entry.template_id remains the backstop.
        session.execute(sa.delete(TemplateEntry).where(TemplateEntry.template_id == existing.id))
        existing.weeks = parsed.weeks
        existing.slots = parsed.slots
        existing.overreach_from_week = parsed.overreach_from_week
        existing.source_file = parsed.source_file
        session.add(existing)
        template = existing
    session.flush()

    session.add_all(
        [
            TemplateEntry(
                template_id=template.id,
                lift_code=entry.lift_code,
                variation=entry.variation,
                week=entry.week,
                slot=entry.slot,
                sets=entry.sets,
                reps=entry.reps,
                pct_num=entry.pct_num,
                pct_den=entry.pct_den,
                is_test=entry.is_test,
            )
            for entry in parsed.entries
        ]
    )

    session.flush()
    return template.id


def import_template(
    session: Session,
    path: Path,
    *,
    name: str = "B1",
    replace: bool = False,
) -> uuid.UUID:
    """Parse `path` and write it as template `name`, in one transaction.

    Spec 03 §5F's signature omits `replace` although its own next line requires
    it; it is added here, keyword-only. Its `import_one_rms_for` parameter is
    deliberately absent -- see the module docstring.
    """
    parsed = parse_workbook(Path(path), name=name)
    return persist_template(session, parsed, replace=replace)
