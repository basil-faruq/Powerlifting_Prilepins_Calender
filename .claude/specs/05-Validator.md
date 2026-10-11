## 1. Overview

A plain-Python **validator** that checks every plan before you see it. Hard rules **reject** a plan; soft rules attach **warnings** but let it through.

It also defines the generic **Proposal** and **ValidationResult** types in `planner_core`, which every later spec (solver, agent, workflow, UI) uses to carry a change through validation and human approval.

The rules are chosen so that **your own spreadsheet passes**: Prilepin ranges and the volume trend are warnings, because your template breaks them on purpose (deadlift week 1 is 6×6 = 36 reps at 56%, above Prilepin's 18–30).

---

## 2. Depends on

- 04-Block_Generator

---

## 3. Routes

- No new routes

---

## 4. Database Schema

- No changes (`proposal` table exists from spec 02)

---

## 5. Functions to Implement

---

### A. `planner_core/proposal.py` (domain-agnostic)

- `Violation(rule: str, message: str, refs: list[str] = [])` — `refs` = session or entry identifiers
- `ValidationResult(ok: bool, violations: list[Violation], warnings: list[Violation])`
- `ProposalKind = Literal["new_block", "reschedule", "weekday_change"]`
- `Proposal(BaseModel, Generic[PayloadT])`: `id, block_id, kind, payload: PayloadT, cost: dict[str, float], reasons: list[str], warnings: list[Violation], source: Literal["chat", "workflow"], status`
- `Validator` `Protocol` with `validate(plan, context) -> ValidationResult`

---

### B. Strength payload (`domains/strength/schemas.py`)

- `SessionChange(seq, week, slot, action: Literal["create", "move", "skip", "repeat_week"], from_date: date | None, to_date: date | None)`
- `StrengthPayload(changes: list[SessionChange], plan: list[DraftSession], new_one_rms: dict[str, Decimal] | None)`
    - `plan` = the full resulting schedule, so the validator always checks the whole picture, not just the diff

---

### C. `domains/strength/validator.py`

#### `ValidationContext`

- `weekday_map: dict[int, int]`
- `overreach_from_week: int` (7)
- `test_week: dict[str, int]` (lift → week of its test; missing for row)
- `rest_min_days: dict[tuple[str, str], int]` — default `{(lift, lift): 2}` for every lift; cross-lift pairs default 1 (open question: deadlift the day after squat)
- `plate_increment_kg: Decimal`
- `pain_lifts: set[str]` — lifts with a pain/injury note in the last 7 days (spec 12)
- `last_done_pct: dict[str, Fraction]` — highest % completed per lift so far
- `approved_off_weekday: set[int]` — session `seq`s allowed off their home weekday
- `today: date`

#### `validate(plan: list[DraftSession], ctx) -> ValidationResult`

Hard rules (violation → `ok = False`):

| Rule id | Check |
| --- | --- |
| `off_weekday` | Session date's weekday ≠ `weekday_map[slot]`, unless its `seq` is in `approved_off_weekday` |
| `one_per_day` | Two sessions on the same date |
| `order` | Session dates not strictly increasing in `seq` order (a repeated week gets new `seq`s, so it stays valid) |
| `rest_gap` | Two sessions sharing a lift closer than `rest_min_days` |
| `pct_early` | `pct > 1.0` in a week earlier than `test_week[lift] − 1` (lifts without a test may never exceed 1.0) |
| `pct_cap` | `pct > 1.10` anywhere |
| `rel_intensity` | `rel_intensity > 1.0` in a week before `overreach_from_week`, or `> 1.10` anywhere |
| `plate_multiple` | `weight_kg` not a multiple of `plate_increment_kg` |
| `pain_progression` | Lift in `pain_lifts` with a future session whose `pct > last_done_pct[lift]` |
| `past_date` | A `create` or `move` change with `to_date < today` |

Soft rules (warning only):

| Rule id | Check |
| --- | --- |
| `prilepin_reps_per_set` | Reps per set outside the zone range (tests excluded) |
| `prilepin_total_reps` | `sets × reps` outside the zone's total range (tests excluded) |
| `volume_trend` | A lift's weekly total reps rises while its weekly top % also rises |

---

## 6. Changes to existing files

- None

---

## 7. Files to Change

- None

---

## 8. Files to Create

- `backend/planner_core/proposal.py`
- `backend/domains/strength/validator.py`
- `backend/tests/test_validator.py`

---

## 9. Dependencies

- None new

---

## 10. Prilepin Zones (hard-coded; your sheet's copy was corrupted into dates)

| Zone (% of 1RM) | Reps per set | Total reps |
| --- | --- | --- |
| below 55% | not checked | not checked |
| 55% – < 70% | 3–6 | 18–30 |
| 70% – < 80% | 3–6 | 12–24 |
| 80% – < 90% | 2–4 | 10–20 |
| 90% and above | 1–2 | 4–10 |

---

## 11. Rules for Implementation

- Pure function: no DB, no LLM, no clock (use `ctx.today`)
- Every violation message names the lift, week and slot, e.g. `squat W10 S3: 104.2% is above 100% before week 10`
- Thresholds (`1.10`, `2` days, `7`) live in one `ValidatorConfig` Pydantic model with these defaults
- Write the tests **before** the implementation; they double as the spec

---

## 12. Expected Behavior

- **Golden test:** the default block from spec 04 (all lifts, Mon/Wed/Fri) → `ok = True`, zero violations, warnings allowed
- Squat W10 S3 at 104.2% passes (`test_week[squat] = 11`, so week 10 is allowed); the same set moved into week 8 fails `pct_early`
- Squat W11 test at 108.3% passes `pct_cap` (≤ 110%)
- A row set above 100% fails `pct_early` (row has no test week)
- Two squat sessions one day apart → `rest_gap`
- Weight 81.5 kg with increment 1.0 → `plate_multiple`
- Squat in `pain_lifts` and next squat session at a higher % than last done → `pain_progression`
- Deadlift W1 6×6 @ 56% → warning `prilepin_total_reps`, not a violation

---

## 13. Error Handling Expectations

- Empty plan → `ok = True` (nothing to break)
- Unknown lift in plan → raise `ValueError` (programming error, not a validation result)
- The validator never raises for rule breaches; it returns them

---

## 14. Definition of Done

- [ ] All 10 hard rules and 3 soft rules implemented with tests (one passing, one failing case each)
- [ ] Default block passes with zero violations
- [ ] Messages are readable and name lift / week / slot
- [ ] `Proposal`, `ValidationResult`, `Violation` live in `planner_core` with no domain imports
