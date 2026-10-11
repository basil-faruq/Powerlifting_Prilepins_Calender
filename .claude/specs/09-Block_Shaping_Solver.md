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

- `reference: list[RefEntry]` — per lift / week / slot: reference `pct`, `sets`, `reps` (from the template, resampled to `weeks`)
- `weeks: int` (6–14)
- `test_week: dict[str, int]`
- `overreach_from_week: int`
- `week1_band: dict[str, tuple[Decimal, Decimal]]` — default 0.55–0.60 for squat / bench / deadlift, 0.70–0.72 for press, 0.55–0.60 for row
- `weights: ShapingWeights` — `pct_dev = 10`, `vol_dev = 10`, `prilepin = 1`, `trend = 1`, `jump = 2`

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

- Within each slot, `p[w+1] ≥ p[w]` for weeks before the lift's **peak week** (its test week; for row, which has no test, the week of its highest reference %, week 7 in your sheet)
- Test entry: `sets = 1`, `reps = 1`, `p ≥ 1000`
- `p ≤ 1100` everywhere
- Week 1 `p` inside `week1_band`
- Relative intensity via a precomputed **allowed table** of `(p, reps)` pairs (`AddAllowedAssignments`):
    - weeks before `overreach_from_week`: `p / rep_max_pct(reps) ≤ 1.0`
    - other weeks: `≤ 1.10`

**Soft terms (objective, minimise)**

- `pct_dev × |p − ref_p|` (`AddAbsEquality`)
- `vol_dev × |vol − ref_vol|`
- `prilepin ×` penalty looked up from a precomputed table indexed by `(p, vol)` (`AddElement` on a flattened index) — 0 inside the zone, distance outside
- `trend ×` `max(0, weekly_vol[w+1] − weekly_vol[w])` per lift during the ramp
- `jump ×` `max(0, p[w+1] − p[w] − 75)` (jumps over 7.5 points)

**Output**

- `ShapedBlock(entries: list[ShapedEntry], objective, stats)` — converted to `DraftLift`s with weights via `round_weight`

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
