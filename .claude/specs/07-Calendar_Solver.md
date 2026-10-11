## 1. Overview

Use **Google OR-Tools CP-SAT** to decide **which day each remaining session lands on** when something changes: a missed session, days you can't train, or a fixed end date.

This is the "when" half of the optimisation (spec 09 is the "what"). It returns **2–3 distinct options**, each with its cost broken down, so the agent can explain the trade-off and you can pick one.

---

## 2. Depends on

- 04-Block_Generator
- 05-Validator

---

## 3. Routes

- No new routes (used by spec 08 and the agent)

---

## 4. Database Schema

- No changes

---

## 5. Functions to Implement (`domains/strength/calendar_solver.py`)

---

### A. Input / output models

- `CalendarProblem`
    - `sessions: list[SessionToPlace]` — `seq, week, slot, lifts: set[str], original_date, pinned_date: date | None`
    - `today: date`
    - `horizon_end: date` — last allowed day (default: original block end + 28 days)
    - `hard_end_date: date | None` — e.g. a meet or test day; last session must be on or before it
    - `weekday_map: dict[int, int]`
    - `allow_flex_days: bool` — may a session leave its home weekday?
    - `unavailable: set[date]`
    - `rest_min_days: dict[tuple[str, str], int]`
    - `weights: CostWeights` — `delay = 1`, `off_weekday = 3`, `end_slip = 10`
- `ScheduleOption`
    - `assignments: dict[int, date]` (seq → date)
    - `cost: CostBreakdown(total, delay_days, off_weekday_count, end_slip_days)`
    - `status: Literal["OPTIMAL", "FEASIBLE"]`
- `Infeasible(reason: str, relaxations_tried: list[str])`

---

### B. `solve(problem, n_options=3, time_limit_s=5.0, seed=0) -> list[ScheduleOption] | Infeasible`

**Variables**

- `d[s]`: integer day index (0 = `today`) for each session; domain = allowed days only:
    - home weekday of its slot, plus every other day if `allow_flex_days`
    - minus `unavailable`, minus days after `horizon_end`
    - `pinned_date` sessions get a single-value domain
- `home[s]`: boolean, true when `d[s]` falls on its home weekday (via `AddElement` over a precomputed 0/1 list per day)

**Hard constraints**

- `AddAllDifferent(d)` — at most one session per day
- `d[s] < d[s+1]` in `seq` order — keeps template order, so each lift's order is kept too
- For consecutive sessions `s < t` sharing a lift: `d[t] − d[s] ≥ rest_min_days`
- If `hard_end_date`: `d[last] ≤ index(hard_end_date)`

**Objective (minimise)**

- `delay × Σ max(0, d[s] − original[s])`
- `+ off_weekday × Σ (1 − home[s])`
- `+ end_slip × max(0, d[last] − original[last])`

**Multiple options**

- After each solve, add a no-good cut (`Σ (d[s] ≠ previous[s]) ≥ 1`, via booleans) and solve again, up to `n_options`
- Drop options whose total cost is more than 3× the best
- Sort by total cost

**Infeasibility**

- If infeasible, retry with: (1) `allow_flex_days = True`, (2) no `hard_end_date`, (3) `horizon_end + 28` — report the first relaxation that works in `reason`

---

### C. `to_plan(draft_sessions, option) -> list[DraftSession]`

- Applies an option's dates to the session list for the validator and the proposal payload

---

## 6. Changes to existing files

- None

---

## 7. Files to Change

- None

---

## 8. Files to Create

- `backend/domains/strength/calendar_solver.py`
- `backend/tests/test_calendar_solver.py`

---

## 9. Dependencies

- `ortools`

---

## 10. Solver Settings

| Parameter | Value |
| --- | --- |
| `max_time_in_seconds` | 5.0 (tests: 2.0) |
| `num_search_workers` | 8 (tests: 1, for determinism) |
| `random_seed` | 0 |

---

## 11. Rules for Implementation

- Build the model from plain data; no DB access inside the solver
- Day indexes relative to `today`; convert to dates only at the edges
- Every option must pass the spec 05 validator before it is returned; options that fail are dropped and logged
- Record solver stats (`status`, `objective`, `wall_time`, `num_booleans`) on the returned object for tracing (spec 14)

---

## 12. Expected Behavior

- **No change:** the default block with nothing missed → one option, cost 0, every date equals `original_date`
- **One missed Monday session, flex allowed:** best option moves it to Tuesday (delay 1, off_weekday 1) only if the rest gap to the next squat still holds; otherwise to the next valid day
- **Flex not allowed:** the missed session and all following sessions shift to the next home weekdays; `end_slip > 0`
- **Unavailable 10 days:** no session lands inside the window
- **Impossible hard end date:** returns `Infeasible` with `reason` naming the relaxation that worked

---

## 13. Error Handling Expectations

- Empty session list → returns one option with cost 0
- `today` after `horizon_end` → `ValueError`
- Solver time-out with no solution → `Infeasible("time limit")`

---

## 14. Definition of Done

- [ ] All five behaviours above covered by tests
- [ ] Options are distinct and sorted by cost
- [ ] Every returned option passes the validator
- [ ] Solver stats attached to results
