"""Capture Excel's own 77 weights as the oracle spec 04 is measured against.

Run once, then commit `tests/fixtures/b1_expected_weights.json`:

    uv run python -m scripts.build_golden_fixture

There is deliberately **no Makefile target** for this. The fixture is the
oracle for CLAUDE.md non-negotiable #4's 77-weight golden test, which "is not
to be weakened to accommodate a refactor" -- and a one-keystroke
`make golden` is precisely how it would get weakened: a refactor breaks eight
weights, someone regenerates, the suite goes green, and the regression is now
the expectation. Regenerating is a deliberately typed command.

Needs no database and no Postgres running: the workbook is the only input.

This is the one place in the codebase that reads the spreadsheet's computed
`Weight` column, and the one place that reads its example maxes. The template
itself stores only sets, reps and the authored fraction (CLAUDE.md guardrail),
and the importer never touches the `1RM` sheet, because those maxes are
examples rather than anyone's real numbers.

A test oracle is the exception, and it has to be: Excel computed each weight as
`FLOOR(max x intensity, 1)` using the max cached on that lift sheet, so the
fixture is only reproducible if it carries that max as the *input* beside the
weight as the *expected output*. Each row therefore records `one_rm_kg`, and
spec 04's golden test feeds that number to the generator rather than reaching
for the athlete's own 1RM -- which is a different number, entered by them, and
would give different weights.

So the example maxes live in exactly one place in this repository: a committed
test fixture, labelled as such, alongside the outputs they produced. Nothing in
the application reads them.

Each row also carries the authored intensity pair (`pct_num`, `pct_den`) and
`is_test`, so the fixture holds *every* input the generator needs. Spec 04's
golden test builds its template entries from this file alone, and therefore
runs on a fresh clone where the gitignored workbook is absent (ADR-0005).
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from domains.strength.importer import (
    LIFT_SHEETS,
    TemplateImportError,
    parse_workbook,
    sheet_layout,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent

DEFAULT_WORKBOOK = _BACKEND_DIR.parent / "data" / "B1_Mine.xlsx"
DEFAULT_OUT = _BACKEND_DIR / "tests" / "fixtures" / "b1_expected_weights.json"

WEIGHT_HEADER = "Weight"
# Matches planned_lift.weight_kg's NUMERIC(6,2), so spec 04's assertion is a
# plain `==` with no rescaling.
WEIGHT_PLACES = Decimal("0.01")

# Every lift sheet keeps its reference max in D1, as an array formula that
# resolves through INDIRECT to the `1RM` sheet. Read from the data_only
# workbook it gives the resolved number Excel actually multiplied by, which is
# more direct -- and harder to mispair -- than re-deriving it from the `1RM`
# sheet's own headers.
REFERENCE_MAX_CELL = "D1"


def _reference_max(sheet) -> Decimal:
    """The max this lift sheet's weights were computed against.

    Quantized like `weight_kg` so a consumer parses both the same way.
    """
    value = sheet[REFERENCE_MAX_CELL].value
    ref = f"{sheet.title}!{REFERENCE_MAX_CELL}"
    if value is None:
        raise TemplateImportError(
            f"{ref} has no cached reference max. Open the workbook in Excel and save it "
            "so the formula results are stored, then run this again."
        )
    try:
        reference = Decimal(str(value)).quantize(WEIGHT_PLACES)
    except InvalidOperation:
        raise TemplateImportError(f"{ref}: reference max {value!r} is not a number") from None
    if reference <= 0:
        raise TemplateImportError(f"{ref}: reference max {reference} is not positive")
    return reference


def build_rows(path: Path) -> list[dict[str, object]]:
    """One row per template entry, carrying the weight Excel cached for it.

    The two reads of the workbook are paired by `(source_sheet, source_row)`,
    which `ParsedEntry` carries for exactly this purpose:

    * `parse_workbook` reads formulas, giving the structure and the provenance;
    * `data_only=True` reads Excel's cached results, giving the weights.

    The `Weight` column index comes from running the importer's own
    `sheet_layout` against the *cached* workbook rather than copying indices
    across, so the pairing is verified rather than assumed. Header cells are
    literal strings in both modes, so the two layouts agree -- and there is
    only ever one implementation of the header lookup.
    """
    parsed = parse_workbook(path)
    cached = load_workbook(path, data_only=True)
    try:
        weight_columns: dict[str, int] = {}
        reference_maxes: dict[str, Decimal] = {}
        for sheet_name, lift_code in LIFT_SHEETS:
            columns = sheet_layout(cached[sheet_name], lift_code).columns
            if WEIGHT_HEADER not in columns:
                raise TemplateImportError(
                    f"sheet {sheet_name!r}: no {WEIGHT_HEADER!r} column, so there is no "
                    "cached weight to capture"
                )
            weight_columns[sheet_name] = columns[WEIGHT_HEADER]
            reference_maxes[sheet_name] = _reference_max(cached[sheet_name])

        rows: list[dict[str, object]] = []
        for entry in parsed.entries:
            cell = cached[entry.source_sheet].cell(
                row=entry.source_row, column=weight_columns[entry.source_sheet]
            )
            ref = f"{entry.source_sheet}!{cell.coordinate}"
            if cell.value is None:
                # `data_only=True` yields None for a formula whose result was
                # never cached -- which is what happens to any file last
                # written by openpyxl rather than by Excel. Without this check
                # the fixture would be generated as 77 nulls and the golden
                # test would pass against nothing.
                raise TemplateImportError(
                    f"{ref} has no cached weight. Open the workbook in Excel and save it "
                    "so the formula results are stored, then run this again."
                )
            try:
                weight = Decimal(str(cell.value)).quantize(WEIGHT_PLACES)
            except InvalidOperation:
                raise TemplateImportError(
                    f"{ref}: cached weight {cell.value!r} is not a number"
                ) from None

            rows.append(
                {
                    # Spec 03 §5G names this key `lift`, where the models and
                    # the database column say `lift_code`. Keeping the spec's
                    # name: the fixture is a declared interface spec 04 reads,
                    # and renaming it buys nothing but churn.
                    "lift": entry.lift_code,
                    "week": entry.week,
                    "slot": entry.slot,
                    "variation": entry.variation,
                    "sets": entry.sets,
                    "reps": entry.reps,
                    # The INPUT Excel used, recorded so the row is a complete
                    # (inputs -> expected output) pair. An example max, and the
                    # only place one survives in this repository.
                    "one_rm_kg": str(reference_maxes[entry.source_sheet]),
                    # A string, not a JSON number. `json.load` turns 81.0 into
                    # a float, and `Decimal(81.0)` is the exact trap
                    # CLAUDE.md #4 exists to prevent. A string forces
                    # `Decimal(row["weight_kg"])` at every read site and makes
                    # a float physically unable to enter the comparison.
                    "weight_kg": str(weight),
                    # The intensity, exactly as authored and unreduced (ADR-0003
                    # rule 2), plus the test flag. Added by spec 04 so the
                    # golden test can build its entries from this file alone
                    # and run on a clone that has no workbook. Strings for the
                    # same reason as `weight_kg`.
                    "pct_num": str(entry.pct_num),
                    "pct_den": str(entry.pct_den),
                    "is_test": entry.is_test,
                }
            )
    finally:
        cached.close()

    # `(lift, week, slot)` is the unique key minus `template_id`, so the sort
    # is total and the file is byte-stable across runs. Alphabetical by lift,
    # which a consumer can reproduce without importing `enums`.
    return sorted(rows, key=lambda row: (row["lift"], row["week"], row["slot"]))


def render(rows: list[dict[str, object]]) -> str:
    """The exact bytes of the fixture file.

    No wrapper object and no `generated_at`: the file is compared, and a
    timestamp would churn it on every run. No `sort_keys` either -- the keys
    stay in the order spec §5G lists them, with `one_rm_kg` appended, then
    spec 04's `pct_num`, `pct_den` and `is_test` (ADR-0005).
    """
    return json.dumps(rows, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_WORKBOOK,
        metavar="PATH",
        help=f"the workbook to read (default {DEFAULT_WORKBOOK})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        metavar="PATH",
        help=f"where to write the fixture (default {DEFAULT_OUT})",
    )
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"{args.path} does not exist.", file=sys.stderr)
        return 1

    try:
        rows = build_rows(args.path)
    except TemplateImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # newline="\n" is not optional on Windows: the default would write CRLF
    # and the committed fixture would churn on every regeneration.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(rows), encoding="utf-8", newline="\n")

    tests = sum(1 for row in rows if row["variation"] is not None)
    print(f"Wrote {len(rows)} rows to {args.out} ({tests} with a variation).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
