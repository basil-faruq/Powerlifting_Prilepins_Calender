## 1. Overview

Use **CP-SAT** to decide **what to lift** each session — sets, reps and % — when you want a block that differs from your template: a shorter block, a meet on a fixed date, or a lift starting from a different week.

With default settings it **reproduces your template**; with other settings it bends the template instead of breaking it. This is what makes "mathematical optimisation" an honest CV line, since template × 1RM alone needs no solver.

The generator (spec 04) still produces normal blocks. Shaping only runs when you ask for something the template doesn't cover.

---

## 2. Depends on

- 04-Block_Generator
- 05-Validator

---

## 3. Routes

- `POST /blocks/shape-preview` → `{athlete_id, template_id, lifts, start_date, weeks?, test_week?: {lift: week}}` → shaped `BlockDraft` + `ValidationResult` + solver stats

---

## 4. Database Schema

- No changes. A shaped block is stored like any other (its entries live in `planned_lift`, not in the template).

---

## 5. Functions to Implement (`domains/strength/shaping_solver.py`)

---

### A. `ShapingProblem`

> **Amended 2026-10-14.** The per-lift `week1_band` and the per-lift ramp
> peak are infeasible against the author's own template. See §15.

- `reference: tuple[RefEntry, ...]` — per lift / week / slot: `p` in tenths of a percent, `sets`, `reps`, the authored `(pct_num, pct_den)` pair, `is_test`, and `exact` (did this row come from a template week, or from interpolating between two?)
- `weeks: int` (6–14)
- `test_week: dict[str, int]`, `test_slot: dict[str, int]` — derived from the reference unless a caller moves one
- `ramp_end: dict[tuple[str, int], int]` — **per `(lift, slot)`**: the last week that must not dip, which is that slot's own peak, not its lift's test week
- `week1_band: dict[tuple[str, int], tuple[int, int]]` — **per `(lift, slot)`**, derived as the reference's own week-1 value ± `week1_margin_tenths`
- `overreach_from_week: int` — rescaled when `weeks` differs from the template's
- `weights: ShapingWeights` — `pct_dev = 10`, `vol_dev = 10`, **`rep_dev = 10`**, `prilepin = 1`, `trend = 1`, `jump = 2`
- `config: ShapingConfig` — bounds and tolerances, each a labelled starting value

---

### B. `resample_reference(entries, weeks) -> list[RefEntry]`

- Same length → returns the template unchanged
- Otherwise maps new week `w` to template position `1 + (w − 1) × (T − 1) / (weeks − 1)` and linearly interpolates `pct`; sets and reps taken from the nearest template week
- The test single stays on the last week of its lift's ramp

---

### C. `solve(problem, time_limit_s=10.0, seed=0) -> ShapedBlock | Infeasible`

**Variables (per entry)**

- `p` — % in **tenths of a percent** (integers 400…1100) so template values such as 58.3% fit
- `sets` ∈ 1..8, `reps` ∈ 1..12, `vol = sets × reps` (`AddMultiplicationEquality`)

**Hard constraints**

- Within each slot, `p[w+1] ≥ p[w]` for weeks up to **that slot's** `ramp_end`
- Test entry: fixed in the domain at `sets = 1`, `reps = 1`, `p ∈ [1000, 1100]`
- `p ≤ 1100` everywhere
- Week 1 `p` inside its slot's `week1_band`
- **`pct_early` and the row cap**, folded into the `p` domain as a per-entry ceiling: 1100 only from the week before that lift's test, and 1000 for a lift with no test at all. Spec 09 omitted both; without them the solver returns blocks the validator refuses.
- Relative intensity via `AddElement` over a **twelve-entry** table of per-reps bounds, then `p ≤ bound`. The bound is **inclusive**: the author's press week 5 sits at exactly 1.0000.
    - weeks before `overreach_from_week`: `p / rep_max_pct(reps) ≤ 1.0`
    - other weeks: `≤ 1.10`

**Soft terms (objective, minimise)**

- `pct_dev × |p − ref_p|` (`AddAbsEquality`)
- `vol_dev × |vol − ref_vol|`
- **`rep_dev × |reps − ref_reps|`** — not in the original spec, and mandatory
- `prilepin ×` distance outside the zone, with the zone fixed from the **reference** intensity at build time
- `trend ×` `max(0, weekly_vol[w+1] − weekly_vol[w])` per lift during the ramp
- `jump ×` `max(0, p[w+1] − p[w] − 75)` — **soft only**; the author's own block breaks it six times

The model is seeded with the reference via `add_hint`. Measured: 10.1 s → 0.4 s single-threaded.

**Output**

- `ShapedBlock(entries, weeks, test_week, overreach_from_week, cost, status, stats)`, or `ShapingInfeasible(reason, stats)` — returned, never raised. Converted to a `BlockDraft` by `to_draft`, which calls `generate_block`: `ShapedEntry` satisfies `TemplateRow` structurally, so the dates, the seq numbering and every weight come from the golden-tested path.
- An entry the solver left untouched keeps its **authored** `(pct_num, pct_den)`. `583/1000` and `70/120` are the same intensity to four decimals and different kilograms once rounded.

---

## 6. Changes to `api/routes/blocks.py`

- Add `POST /blocks/shape-preview`
- `POST /blocks` accepts an optional `shaped: true` with the same parameters, storing the shaped plan in the proposal payload

---

## 7. Files to Change

- `api/routes/blocks.py`, `domains/strength/services.py`

---

## 8. Files to Create

- `backend/domains/strength/shaping_solver.py`
- `backend/tests/test_shaping_solver.py`

---

## 9. Dependencies

- `ortools` (already added)

---

## 10. Why the template wins by default

- `pct_dev` and `vol_dev` weigh 10× more than the Prilepin and trend penalties
- Moving deadlift W1 from 36 reps to 30 would save 6 Prilepin points but cost 60 deviation points, so the solver keeps your 6×6

---

## 11. Rules for Implementation

- Precompute all tables in plain Python before building the model
- Keep the model per block small (≈ 80 entries × 3 variables); target < 10 s
- Every result goes through the spec 05 validator before it is returned
- No LLM involvement; the agent only passes parameters

---

## 12. Expected Behavior

- **Golden test:** default 11-week reference → every `p` within 1 tenth-point of the template and every `sets`/`reps` equal; validator `ok`
- `weeks = 8` → valid block, % rising per slot, each test single on its lift's last ramp week
- `test_week = {press: 11}` → press peaks in week 11 instead of 9
- Impossible request (e.g. `weeks = 6` with week-1 band forcing too steep a ramp) → `Infeasible` with reason

---

## 13. Error Handling Expectations

- `weeks` outside 6–14 → 422
- Solver timeout with a feasible solution → returned with `status = FEASIBLE`
- No solution → 422 with `Infeasible.reason`

---

## 14. Definition of Done

- [ ] Golden test reproduces the template
- [ ] 8-week and moved-test cases produce valid blocks
- [ ] All hard constraints covered by tests
- [ ] Solver stats returned for tracing
