## 1. Overview

Import your spreadsheet `B1_Mine.xlsx` into the database as a **block template**: 77 rows of lift × week × slot with sets, reps and %1RM.

The importer stores **percentages as exact fractions** taken from the cell formulas (e.g. `=70/120`), never the computed kg, so the template works for any future 1RM.

It also produces a **golden fixture** from the weights Excel already calculated, which spec 04 uses to prove the generator is correct.

---

## 2. Depends on

- 02-Database_Schema

---

## 3. Routes

- No new routes here (upload endpoint comes in spec 06)

---

## 4. Database Schema

- No changes. Writes to `block_template`, `template_entry`, optionally `one_rep_max`.

---

## 5. Functions to Implement (`domains/strength/importer.py`)

---

### A. `parse_workbook(path: Path) -> ParsedTemplate`

- Opens with `openpyxl.load_workbook(path)` (formulas, **not** `data_only`)
- Reads every sheet named `B1 <Lift>`: `Deadlift`, `Squat`, `Bench`, `Press`, `Row`
- Finds the header row by locating the cell `Week` in column A (row 4 in your file); maps columns **by header name**, not position:
    - `Week`, `Day`, `Sets`, `Reps`, `Intensity`, and `Variation` when present (Bench only)
- `Week` is only filled on the first row of each week → carry it forward to the next rows
- Skips rows where `Day` is empty
- Returns a Pydantic `ParsedTemplate(name, weeks, slots, entries: list[ParsedEntry], one_rms: dict[str, Decimal] | None)`

---

### B. `parse_intensity(cell_value) -> tuple[Decimal, Decimal]`

- `"=70/120"` → `(70, 120)`
- `"=112.5/105"` → `(112.5, 105)`
- Plain number `0.5` → `(1, 2)` via `Fraction(str(value))`
- Anything else → `TemplateImportError` naming sheet and cell

---

### C. `normalize_variation(text) -> str | None`

- `Close Grip` → `close_grip`
- `Wide Grip`, `Wide Grp` (typo in your Bench sheet, W11 Day 3) → `wide_grip`
- Empty → `None`; unknown text → `TemplateImportError`

---

### D. `mark_tests(entries)`

- `is_test = True` when `sets == 1 and reps == 1 and pct > 1`
- Exactly one test per lift is expected; row has none (it is never tested) — that is valid

---

### E. `parse_one_rms(wb) -> dict[str, Decimal]`

- From sheet `1RM`: header row 1 (`Deadlift 1RM`, `Squat 1RM`, …), values from row 2 (Block 0)

---

### F. `import_template(session, path, name="B1", import_one_rms_for: UUID | None = None) -> UUID`

- Parses, then inserts `block_template` (weeks = 11, slots = 3, overreach_from_week = 7) and its entries in one transaction
- If a template with the same name exists → `TemplateExistsError` unless `replace=True`
- If `import_one_rms_for` is given → inserts 1RMs with `source = imported`

---

### G. `scripts/build_golden_fixture.py`

- Opens the workbook **with `data_only=True`** to read Excel's cached `Weight` values
- Writes `tests/fixtures/b1_expected_weights.json`: list of `{lift, week, slot, variation, sets, reps, weight_kg}` (77 items)
- Run once; commit the JSON

---

## 6. Changes to `Makefile`

- `make import` → `uv run python -m scripts.import_template ../data/B1_Mine.xlsx`

---

## 7. Files to Change

- `Makefile`

---

## 8. Files to Create

- `backend/domains/strength/importer.py`
- `backend/domains/strength/schemas.py` (`ParsedTemplate`, `ParsedEntry`)
- `backend/scripts/import_template.py`, `backend/scripts/build_golden_fixture.py`
- `backend/tests/fixtures/b1_expected_weights.json`
- `backend/tests/test_importer.py`

---

## 9. Dependencies

- `openpyxl`

---

## 10. Facts About Your File (verified)

| Lift | Rows | Template slot(s) | Test entry |
| --- | --- | --- | --- |
| Deadlift | 11 | 2 | W11 S2, 1×1 @ 170/160 |
| Squat | 22 | 1, 3 | W11 S1, 1×1 @ 130/120 |
| Bench | 22 | 1 (close), 3 (wide); W10 both wide | W10 S3, 1×1 @ 112.5/105 |
| Press | 11 | 2 | W9 S2, 1×1 @ 72.5/70 |
| Row | 11 | 3 | none |

- Total: **77 entries**
- Squat W11 S3 intensity is the literal `0.5`, not a formula
- Ignore the sheets' `Rel. Intensity` columns (the Bench one reads Sets instead of Reps); spec 04 recomputes it
- Ignore the `Prilepins Chart` sheet (Excel turned its ranges into dates); spec 05 hard-codes the values
- The `P1RM` table is hard-coded in spec 04, not imported

---

## 11. Rules for Implementation

- Never import computed weights into the template; only sets, reps and the % fraction
- Use `Fraction` / `Decimal` for intensity; no float arithmetic
- Column lookup by header text, so an extra column (like Bench's `Variation`) cannot shift values
- Import is all-or-nothing (single transaction)

---

## 12. Expected Behavior

- `make import` creates template `B1` with 77 entries and 4 test entries
- Re-running without `--replace` fails with a clear message
- Golden fixture JSON has 77 rows; e.g. squat W1 S1 = 81 kg, deadlift W11 S2 = 170 kg, bench W10 S3 = 112 kg

---

## 13. Error Handling Expectations

- Missing lift sheet → `TemplateImportError("sheet 'B1 Press' not found")`
- Unparseable intensity → error naming sheet + cell (e.g. `B1 Squat!F12`)
- Unknown variation text → error naming the cell

---

## 14. Definition of Done

- [ ] 77 entries imported; counts per lift match the table above
- [ ] Intensities stored as exact fractions (`70/120`, not `0.5833`)
- [ ] `is_test` set on exactly 4 entries
- [ ] `Wide Grp` normalised to `wide_grip`
- [ ] Golden fixture generated from Excel's cached values and committed
- [ ] Tests cover formula, literal and malformed intensities
