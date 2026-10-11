## 1. Overview

Turn a template + your current 1RMs + your chosen lifts and weekdays into a **concrete block**: dated sessions with sets, reps and kg.

This is the core rule your spreadsheet encodes: `weight = FLOOR(1RM × %, increment)`. It also computes **relative intensity** (how close a set is to your estimated rep max) for the validator, and the **next block's 1RMs** after a test.

No solver yet: dates come straight from the weekday map. The solver (spec 07) only moves sessions when something changes.

---

## 2. Depends on

- 03-XLSX_Template_Import

---

## 3. Routes

- No new routes (exposed in spec 06)

---

## 4. Database Schema

- No changes. Writes `block`, `planned_session`, `planned_lift`.

---

## 5. Functions to Implement

---

### A. `domains/strength/brzycki.py`

- `REP_PCT: dict[int, Decimal]` — your P1RM sheet, column A (Brzycki):

| Reps | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| % of 1RM | 1.00 | 0.95 | 0.90 | 0.88 | 0.86 | 0.83 | 0.80 | 0.78 | 0.76 | 0.75 | 0.72 | 0.70 | 0.68 |

- `rep_max_pct(reps) -> Decimal`: table lookup; reps > 13 → `(37 − reps) / 36`
- `rel_intensity(pct: Fraction, reps) -> Decimal` = `pct / rep_max_pct(reps)`, rounded to 4 dp
- `estimate_1rm(weight_kg, reps) -> Decimal` = `weight × 36 / (37 − reps)` (Brzycki formula); reps must be 1..12

---

### B. `domains/strength/rounding.py`

- `round_weight(raw: Fraction, increment: Decimal, mode: Literal["floor", "nearest"]) -> Decimal`
    - `floor`: largest multiple of `increment` ≤ raw
    - `nearest`: nearest multiple, ties round up
- Exact arithmetic only (`Fraction` → `Decimal` at the end)

---

### C. `domains/strength/generator.py`

---

#### `check_weekday_map(weekday_map, template_entries, lifts) -> list[str]`

- Slots must map to **strictly increasing weekdays** (Day 1 before Day 2 before Day 3 in the same week)
- No two slots on the same weekday
- For each lift appearing in more than one slot, the gap between its sessions (including the wrap to next week) must be ≥ 2 days
- Returns human-readable problems; empty list = OK

---

#### `generate_block(athlete, template, entries, one_rms, lifts, start_date, weekday_map) -> BlockDraft`

- `start_date` must be a Monday
- Session date = `start_date + 7 × (week − 1) + weekday_map[slot]` days
- Keep only entries whose lift is in `lifts`; group by `(week, slot)`; drop groups with no lifts
- For each lift: `weight_kg = round_weight(one_rm × pct_num / pct_den, athlete.plate_increment_kg, athlete.rounding)`
- `rel_intensity` from `brzycki.rel_intensity`
- `seq` = order by date (1..n)
- Returns a Pydantic `BlockDraft` (not persisted):
    - `athlete_id, template_id, start_date, lifts, one_rm_snapshot`
    - `sessions: list[DraftSession(week, slot, seq, date, lifts: list[DraftLift])]`
    - `DraftLift(lift_code, variation, sets, reps, pct_num, pct_den, weight_kg, rel_intensity, is_test)`

---

#### `persist_block(session, draft, status="draft") -> Block`

- Inserts `block` (number = previous max + 1 for the athlete), sessions (`original_date = date`) and lifts in one transaction

---

#### `next_block_one_rms(block, logs) -> dict[str, NewOneRm]`

- Lift with a test entry whose log shows the single completed at the prescribed weight → new 1RM = that weight, `source = tested`
- Test missed or failed → keep the block's snapshot value (doc rule: a missed test keeps the old 1RM)
- Lift without a test (row) → `max(old, best estimate_1rm from logs in the last 4 weeks)`; `source = estimated` only if it went up

---

## 6. Changes to existing files

- None

---

## 7. Files to Change

- None

---

## 8. Files to Create

- `backend/domains/strength/brzycki.py`
- `backend/domains/strength/rounding.py`
- `backend/domains/strength/generator.py`
- Add `BlockDraft`, `DraftSession`, `DraftLift`, `NewOneRm` to `domains/strength/schemas.py`
- `backend/tests/test_brzycki.py`, `test_rounding.py`, `test_generator.py`

---

## 9. Dependencies

- None new

---

## 10. Default Weekly Layout (from your sheet)

| Slot | Lifts |
| --- | --- |
| Day 1 | Squat, Bench (close grip) |
| Day 2 | Deadlift, Press |
| Day 3 | Squat, Bench (wide grip), Row |

Example weekday map for tests: Day 1 = Monday (0), Day 2 = Wednesday (2), Day 3 = Friday (4).

---

## 11. Rules for Implementation

- No float arithmetic anywhere in weight calculation
- The generator is a pure function (no DB access); `persist_block` is the only writer
- Choosing a subset of lifts must not change the remaining lifts' numbers
- Brzycki table values must match the P1RM sheet exactly (test reads the sheet and compares)

---

## 12. Expected Behavior

- **Golden test:** athlete with increment 1.0 / floor, 1RMs {deadlift 160, squat 140, bench 105, press 70, row 105}, all lifts, Mon/Wed/Fri, start Monday → 33 sessions, 77 planned lifts, every `weight_kg` equal to `tests/fixtures/b1_expected_weights.json`
- Spot checks: squat W1 Day 1 = 3×10 @ 81 kg; squat W11 Day 1 = 1×1 @ 151 kg; bench W10 Day 3 = 1×1 @ 112 kg; press W9 = 1×1 @ 72 kg
- Lifts = `[squat, deadlift]` → 33 sessions (Day 1 and 3 hold squat; Day 2 deadlift), no bench/press/row rows
- Lifts = `[deadlift]` → 11 sessions, all on Day 2
- With increment 2.5 / floor: squat W1 Day 1 = 80 kg
- `check_weekday_map` with Day 1 = Mon, Day 2 = Wed, Day 3 = Sun → error (squat on Sunday then Monday, only 1 day apart across the week boundary)
- `check_weekday_map` with Day 1 = Wed, Day 2 = Mon → error (slots not in increasing order)

---

## 13. Error Handling Expectations

- `start_date` not a Monday → `ValueError("start_date must be a Monday")`
- A chosen lift without a 1RM → `ValueError("no 1RM for press")`
- Weekday map missing a slot that the chosen lifts need → `ValueError`
- `estimate_1rm` with reps ≥ 13 → `ValueError`

---

## 14. Definition of Done

- [ ] Golden test passes for all 77 weights
- [ ] Rounding modes and increments tested
- [ ] Lift subsets produce correct sessions
- [ ] `check_weekday_map` rejects bad maps with readable messages
- [ ] `next_block_one_rms` tested for: test made, test missed, accessory estimate
