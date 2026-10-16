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

> **Revised 2026-10-12 per ADR-0006.** Four details:
>
> 1. **Collections are tuples, not the `list` above** - the same one-word deviation, for the same reason, as ADR-0003 modification 13: `frozen=True` blocks attribute assignment but not in-place mutation of a list, and a validation result that can be edited after the fact is not evidence of anything.
> 2. **`ok` is derived, not asserted.** A `model_validator` rejects any `ValidationResult` whose `ok` disagrees with its violations, and `ValidationResult.of(violations, warnings)` is the normal constructor. `ok=True` alongside a violation would let a plan past the only gate there is (CLAUDE.md #2).
> 3. **`ProposalKind`, `ProposalStatus` and `ProposalSource` are defined here**, in `planner_core/proposal.py`, and `domains/strength/enums.py` re-exports them. `Proposal` is typed by them and `planner_core` may not import a domain (ADR-0001 rule 1), so defining them in the domain would have meant two copies of the same vocabulary - and `models.Proposal`'s CHECK constraints derive from the `enums` aliases. See §6.
> 4. **`Proposal` uses PEP 695 generic syntax** (`class Proposal[PayloadT]`) rather than `Generic[PayloadT]`, which is what `ruff`'s UP046 requires on Python 3.12.

---

### B. Strength payload (`domains/strength/schemas.py`)

- `SessionChange(seq, week, slot, action: Literal["create", "move", "skip", "repeat_week"], from_date: date | None, to_date: date | None)`
- `StrengthPayload(changes: list[SessionChange], plan: list[DraftSession], new_one_rms: dict[str, Decimal] | None)`
    - `plan` = the full resulting schedule, so the validator always checks the whole picture, not just the diff

> **Revised 2026-10-12 per ADR-0006.** `SessionChange` validates that a change which **places** a session says where: `create` and `move` require `to_date`, and `move` also requires `from_date`. The `past_date` rule reads `to_date`, so a `move` without one would silently pass the single check that protects against scheduling into the past.

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

> **Revised 2026-10-12 per ADR-0006.** Three departures, all in the same direction - a threshold belongs wherever it has exactly one owner:
>
> 1. **`overreach_from_week` stays on the context, not in `ValidatorConfig`**, contrary to §11. It is per-template data, already stored on `block_template.overreach_from_week` (ADR-0002), so putting it in the config would create a second copy that can disagree with the template the plan was generated from.
> 2. **`rest_min_days` defaults to `{}`**, and the same-lift (2) and cross-lift (1) defaults live in `ValidatorConfig` as `same_lift_rest_days` and `cross_lift_rest_days`. Pre-populating `{(lift, lift): 2}` for every lift would hard-code a threshold into data the caller assembles, and the lookup needs a fallback anyway. A pair is matched **in either order**, so `{("squat", "deadlift"): 2}` covers both. Both defaults remain **starting values** traced to the author's own Mon/Wed/Fri layout and labelled as such at the fields (CLAUDE.md guardrail); the cross-lift question is still open (CONTEXT.md §9).
> 3. **`read_test_weeks(entries)` was added** to build `test_week` from the **template**, never from the plan under validation - a plan that moved its own test single must not thereby move the week before which exceeding 100% is a violation. It raises if one lift has two test weeks. The name is not `test_weeks` because pytest collects any importable name beginning with `test_`.

#### `validate(plan: list[DraftSession], ctx) -> ValidationResult`

> **Revised 2026-10-12 per ADR-0006.** The signature is `validate(plan, ctx, *, changes=(), config=DEFAULT_CONFIG)`.
>
> **`changes` had to be added**, keyword-only and defaulting to empty: `past_date` is a rule about a *requested change*, and the plan alone cannot express it. A session already sitting in the past is history; only *moving* one there is a mistake. `StrengthValidator` - a small class satisfying `planner_core.Validator` - unpacks a `StrengthPayload` into the two arguments, so the engine's protocol stays domain-agnostic.
>
> **The guards that raise `ValueError` are wider than §13's.** Besides an unknown lift in the plan, the validator rejects an unknown lift in any *context* key (`test_week`, `pain_lifts`, `last_done_pct`, `rest_min_days`) and a **duplicated `seq`**. Both are programming errors upstream, and both would otherwise mean a plan silently half-checked - `seq` is the identity every `refs` entry and both ordering rules depend on.

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

> **Revised 2026-10-12 per ADR-0006.** Three decisions about these ten, the first two the author's:
>
> 1. **The four calendar rules judge today onward** (`off_weekday`, `one_per_day`, `order`, `rest_gap`). `plan` is the whole resulting schedule, history included, and a proposal cannot change what already happened - so a rest gap the athlete trained through in week 2 must not veto a reschedule in week 9. A pair **straddling** `today` is still judged. The loading rules (`pct_early`, `pct_cap`, `rel_intensity`, `plate_multiple`) read the whole plan, because the arithmetic does not care what day it is.
> 2. **`pain_progression` blocks when there is no baseline.** If a lift is in `pain_lifts` and absent from `last_done_pct`, every session of that lift from today onward is a violation: with nothing completed there is no evidence the planned load is tolerable, and the safe direction is to stop.
> 3. **`rel_intensity` is recomputed** as `pct / rep_max_pct(reps)` rather than read from `DraftLift.rel_intensity`. That field is a 4 dp cache written by the generator, and a solver that changed a set's reps without recomputing it would otherwise be validated against a stale number. A test pins this by feeding a plan whose cached value says 0.5000 while the real figure is 1.0233.
>
> Also: `off_weekday` treats a session whose **slot is absent from the weekday map** as off its weekday - it has no home weekday to be on. `rel_intensity` reports the cap breach *or* the early-overreach breach for a set, never both.
>
> **Not added here:** `week <= block_template.weeks` and `slot <= slots`, the cross-table bound ADR-0002 rule 3 left to "specs 04/05". Spec 04's `generate_block` enforces both, and `ValidationContext` deliberately carries no template, so the validator does not re-check them. See ADR-0006's Related Decisions.

Soft rules (warning only):

| Rule id | Check |
| --- | --- |
| `prilepin_reps_per_set` | Reps per set outside the zone range (tests excluded) |
| `prilepin_total_reps` | `sets × reps` outside the zone's total range (tests excluded) |
| `volume_trend` | A lift's weekly total reps rises while its weekly top % also rises |

> **Revised 2026-10-12 per ADR-0006.** **"Weekly" means a *training* week, not a week number.** The plan is split into runs of consecutive sessions in `seq` order sharing a week number, broken when the number changes *or* when a `(week, slot)` pair comes round again - which is what a repeated week looks like. Grouping by week number alone would merge a repeat into the original and read the doubled volume as a rise, warning about the one change spec 08 most often proposes. A test pins it.
>
> Prilepin's zone bounds are **inclusive at the lower edge** (55% is in the 55-70 band), and below 55% nothing is checked, as §10 says.

---

## 6. Changes to existing files

- None

> **Revised 2026-10-12 per ADR-0006.** There were four, each small and each argued in ADR-0006:
>
> 1. **`domains/strength/enums.py`** re-exports `ProposalKind`, `ProposalStatus` and `ProposalSource` from `planner_core.proposal` instead of defining them (see §5A). The `PROPOSAL_*` tuples and `models.Proposal`'s CHECK constraints are unchanged and still derive from the aliases, which `test_models.py`'s drift test asserts.
> 2. **`domains/strength/schemas.py`** gained `SessionChange` and `StrengthPayload`, which §5B places there.
> 3. **`tests/golden.py` is new, and `tests/conftest.py` gained four session-scoped fixtures** (`golden_rows`, `golden_entries`, `golden_maxes`, `golden_draft`), moved out of `test_generator.py` so the validator suite can assert CLAUDE.md #5 against the same authored block the generator suite proves the 77 weights of. A pure move: no fixture body changed, and `test_generator.py` still passes.
> 4. **`tests/test_proposal.py` is a fifth new file**, beyond §8's three. It covers the `planner_core` types, the `SessionChange` date rules and the Definition-of-Done item that `planner_core` imports nothing from `domains` - an AST walk of the package, which belongs with the types rather than with the strength rules.

---

## 7. Files to Change

- None

---

## 8. Files to Create

- `backend/planner_core/proposal.py`
- `backend/domains/strength/validator.py`
- `backend/tests/test_validator.py`

> **Revised 2026-10-12 per ADR-0006.** Plus `backend/tests/test_proposal.py` and `backend/tests/golden.py` - see §6.

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

> **Revised 2026-10-12 per ADR-0006.** Hard-coded in `validator.PRILEPIN_ZONES` as exact `Fraction` bounds, lower edge inclusive, with no row below 55%.

---

## 11. Rules for Implementation

- Pure function: no DB, no LLM, no clock (use `ctx.today`)
- Every violation message names the lift, week and slot, e.g. `squat W10 S3: 104.2% is above 100% before week 10`
- Thresholds (`1.10`, `2` days, `7`) live in one `ValidatorConfig` Pydantic model with these defaults
- Write the tests **before** the implementation; they double as the spec

> **Revised 2026-10-12 per ADR-0006.** `ValidatorConfig` holds `pct_cap`, `rel_intensity_cap`, `overreach_rel_intensity`, `same_lift_rest_days` and `cross_lift_rest_days`. It does **not** hold `overreach_from_week` (per-template, so it is on the context - §5C) or the 7-day pain window (spec 12 decides which lifts are painful; the validator only receives the set). The tests were written first and ran red.

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

> **Revised 2026-10-12 per ADR-0006.** All verified, and the golden block's warning counts are now **pinned**: 33 `prilepin_reps_per_set`, 34 `prilepin_total_reps` and exactly 4 `volume_trend` (squat W2→W3, press W3→W4, squat W8→W9, squat W10→W11). Changing any of those counts is a calibration decision, so a test makes it visible rather than silent.

---

## 13. Error Handling Expectations

- Empty plan → `ok = True` (nothing to break)
- Unknown lift in plan → raise `ValueError` (programming error, not a validation result)
- The validator never raises for rule breaches; it returns them

> **Revised 2026-10-12 per ADR-0006.** A duplicated `seq`, and an unknown lift in a *context* key, also raise - see §5C.

---

## 14. Definition of Done

- [ ] All 10 hard rules and 3 soft rules implemented with tests (one passing, one failing case each)
- [ ] Default block passes with zero violations
- [ ] Messages are readable and name lift / week / slot
- [ ] `Proposal`, `ValidationResult`, `Violation` live in `planner_core` with no domain imports

> **Completed 2026-10-12; see [ADR-0006](../../docs/adr/0006-validator-and-proposal-types.md).** Spec 05 accepted with modifications, each annotated in place above. All 13 rules implemented with a passing and a failing case each; **the authored block validates with zero violations**; `make lint` clean; `make test` **453 passed** (89 new), green in all four combinations of "has Postgres" and "has the workbook" - every new test is pure, so all 89 run in all four.
