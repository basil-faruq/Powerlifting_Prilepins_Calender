## 1. Overview

Import your spreadsheet `B1_Mine.xlsx` into the database as a **block template**: 77 rows of lift × week × slot with sets, reps and intensity.

The importer stores **percentages as exact fractions** taken from the cell formulas (e.g. `=70/120`), never the computed kg, so the template works for any 1RM.

What is imported is the block's **pattern** and nothing more. The workbook's `1RM` sheet holds **example** maxes with no test date, and is never read: the only 1RMs the planner computes from are ones the athlete entered together with the date they tested them (ADR-0003).

It also produces a **golden fixture** from the weights Excel already calculated, which spec 04 uses to prove the generator is correct.

---

## 2. Depends on

- 02-Database_Schema

---

## 3. Routes

- No new routes here (upload endpoint comes in spec 06)

---

## 4. Database Schema

- No changes. Writes to `block_template` and `template_entry`, and to no other table.

> **Revised 2026-10-12 per ADR-0003.** Originally read *"…optionally `one_rep_max`."* See §5E.

---

## 5. Functions to Implement (`domains/strength/importer.py`)

---

### A. `parse_workbook(path: Path, *, name="B1", weeks=11, slots=3, overreach_from_week=7) -> ParsedTemplate`

- Opens with `openpyxl.load_workbook(path)` (formulas, **not** `data_only`)
- Reads every sheet named `B1 <Lift>`: `Deadlift`, `Squat`, `Bench`, `Press`, `Row`
- Finds the header row by locating the cell whose **text** is `Week` in column A (row 4 in your file); maps columns **by header name**, not position:
    - `Week`, `Day`, `Sets`, `Reps`, `Intensity`, and `Variation` when present (Bench only)
    - Beware: column A also holds `Block` at row 1 and a merged title band at row 3 (`Strict Press` on the Press sheet), so "first non-empty cell" finds the wrong row on all five sheets
- `Week` is only filled on the first row of each week → carry it forward to the next rows, resetting per sheet
- Skips rows where `Day` is empty — **before** updating the carried week, so trailing style-only rows cannot influence the parse
- Returns a Pydantic `ParsedTemplate(name, weeks, slots, overreach_from_week, source_file, entries: tuple[ParsedEntry, ...])`

> **Revised 2026-10-12 per ADR-0003.** `weeks`/`slots` come from constants, never from `max(week)`/`max(slot)` of the data, or the bound check in §11 becomes a tautology. `one_rms` is gone from the return type — see §5E. `ParsedEntry` additionally carries `source_sheet` and `source_row`, which name the cell in every error message and let §5G pair the two reads of the workbook.

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

### E. ~~`parse_one_rms(wb) -> dict[str, Decimal]`~~ — **withdrawn**

> **Revised 2026-10-12 per ADR-0003.** This section originally read: *"From sheet `1RM`: header row 1 (`Deadlift 1RM`, `Squat 1RM`, …), values from row 2 (Block 0)."*
>
> **Not implemented.** The workbook's maxes are **examples** and the file records no date they were tested, so reading that sheet can only manufacture data. The importer never opens it. A 1RM is the athlete's own measurement, entered with the date they tested it, and is the only kind the planner computes from.
>
> The one legitimate reader of those example values is `scripts/build_golden_fixture.py`, which records each as `one_rm_kg` beside the weight Excel derived from it — see §5G.

---

### F. `import_template(session, path, *, name="B1", replace=False) -> UUID`

- Parses, then inserts `block_template` (weeks = 11, slots = 3, overreach_from_week = 7) and its entries in one transaction
- If a template with the same name exists → `TemplateExistsError` unless `replace=True`; `replace=True` keeps the template's `id` and swaps only its entries
- Writes `block_template` and `template_entry` and **no other table**

> **Revised 2026-10-12 per ADR-0003.** The signature originally read `import_template(session, path, name="B1", import_one_rms_for: UUID | None = None)`, with a final bullet *"If `import_one_rms_for` is given → inserts 1RMs with `source = imported`."* That parameter is removed for the reason given in §5E. `replace` is now in the signature, which the original omitted despite requiring it.

---

### G. `scripts/build_golden_fixture.py`

- Opens the workbook **with `data_only=True`** to read Excel's cached `Weight` values
- Writes `tests/fixtures/b1_expected_weights.json`: list of `{lift, week, slot, variation, sets, reps, one_rm_kg, weight_kg, pct_num, pct_den, is_test}` (77 items), sorted by `(lift, week, slot)`
- `one_rm_kg`, `weight_kg`, `pct_num` and `pct_den` are **decimal strings**, so `json.load` cannot turn them into floats
- Run once; commit the JSON

> **Revised 2026-10-12 per ADR-0003.** The key list originally omitted `one_rm_kg`, the sort order and the string typing.
>
> `one_rm_kg` is required: Excel computed each weight as `FLOOR(max × intensity, 1)` from the max cached on that lift sheet, and since §5E no longer reads those maxes, the fixture has to carry its own input or no later spec can reproduce a single weight. Each row is therefore a complete (inputs → expected output) pair, and **this file is the only place an example max survives.** Spec 04's golden test feeds `one_rm_kg`, never the athlete's own max.
>
> **Revised 2026-10-12 per ADR-0005 (spec 04).** The key list gained `pct_num`, `pct_den` and `is_test`, so eleven keys rather than eight.
>
> With only the max and the expected weight, the fixture was still not a complete oracle: the generator also needs the *intensity*, which lived only in the gitignored workbook. Spec 04's 77-weight golden test — the one CLAUDE.md #4 calls not-to-be-weakened — therefore skipped on a fresh clone and in CI, which is where it matters most. The pair is written exactly as authored and unreduced, matching `template_entry`. `test_importer.py::test_the_golden_fixture_agrees_with_the_workbook_on_every_entry` compares all 77 rows against `parse_workbook` whenever the real file *is* present, so the fixture cannot silently drift from its source.

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
- **The workbook contributes the block's pattern and nothing else.** Its `1RM` sheet is never read, and no code path may derive, default or back-fill a `one_rep_max` row (ADR-0003 rule 1)
- **Never treat `pct_den` as a 1RM.** It is the target max the block was programmed against: the squat rows divide by 120 while that sheet's example max is 140, and bench W11 divides by 125 against 105. Any check comparing the two would reject the source file

---

## 12. Expected Behavior

- `make import` creates template `B1` with 77 entries and 4 test entries, and leaves `one_rep_max` empty
- Re-running without `--replace` fails with a clear message
- Golden fixture JSON has 77 rows; e.g. squat W1 S1 = 81 kg from a 140 kg max, deadlift W11 S2 = 170 kg from 160, bench W10 S3 = 112 kg from 105

---

## 13. Error Handling Expectations

- Missing lift sheet → `TemplateImportError("sheet 'B1 Press' not found")`
- Unparseable intensity → error naming sheet + cell (e.g. `B1 Squat!F12`)
- Unknown variation text → error naming the cell

---

## 14. Definition of Done

- [x] 77 entries imported; counts per lift match the table above
- [x] Intensities stored as exact fractions (`70/120`, not `0.5833`)
- [x] `is_test` set on exactly 4 entries
- [x] `Wide Grp` normalised to `wide_grip`
- [x] Golden fixture generated from Excel's cached values and committed, carrying the `one_rm_kg` each weight was derived from
- [x] Tests cover formula, literal and malformed intensities
- [x] **No 1RM is written by any code path** (added 2026-10-12 per ADR-0003)
