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

Frozen, `extra="forbid"`, with tuples and frozensets rather than lists and sets (ADR-0005's convention).

- `CalendarProblem`
    - `sessions: tuple[SessionToPlace, ...]` — `seq, week, slot, lifts: frozenset[str], original_date, pinned_date: date | None`
    - `today: date`
    - `horizon_end: date` — last allowed day. Pass `None` for "original block end + 28 days" (`DEFAULT_HORIZON_SLACK_DAYS`, a **starting value**); a `before` validator fills it in, so the field is never `None` afterwards
    - `hard_end_date: date | None` — e.g. a meet or test day; last session must be on or before it
    - `weekday_map: dict[int, int]`
    - `allow_flex_days: bool` — may a session leave its home weekday?
    - `unavailable: frozenset[date]`
    - `rest_min_days: dict[tuple[str, str], int]`
    - `config: ValidatorConfig` — carried whole, so the rest thresholds have one source
    - `weights: CostWeights` — `delay = 1`, `off_weekday = 3`, `end_slip = 10`
    - Raises `ValueError` on a duplicate `seq`, `today > horizon_end`, `hard_end_date < today`, or a non-pinned session whose slot has no home weekday. These are setup errors, not infeasible calendars.
- `ScheduleOption`
    - `assignments: dict[int, date]` (seq → date, pinned sessions included)
    - `cost: CostBreakdown(total, delay_days, off_weekday_count, end_slip_days)`
    - `status: Literal["OPTIMAL", "FEASIBLE"]`
    - `stats: SolverStats`
- `SolverStats(status, objective, wall_time_s, num_booleans)`
- `Infeasible(reason, relaxations_tried: tuple[str, ...], relaxation: str | None, stats: SolverStats | None)`

---

### A2. `problem_from_plan(plan, *, today, weekday_map, history_seqs, ...) -> CalendarProblem`

- `plan` is the **whole** schedule, history included — ADR-0006 has callers never pre-filter
- `history_seqs` (status `done` or `missed`) are **pinned** to the day they happened: they still constrain rest and ordering, but no option may re-date them (ADR-0007)
- Everything else is free, *including* a session whose planned date has already passed — that is the unlogged missed session

---

### B. `solve(problem, n_options=3, time_limit_s=5.0, seed=0, *, check, num_workers=8) -> tuple[ScheduleOption, ...] | Infeasible`

`check: Callable[[ScheduleOption], ValidationResult]` is a **required keyword**, so the §11 validator gate cannot be forgotten. `validator_check(plan, ctx)` builds the real one; it waives `approved_off_weekday` for exactly the seqs the option moves, which is the derivation `services.validation_context` performs at approval.

`time_limit_s` is a **total** budget across every solve, not per solve. Spent time is read from the solver, never from a clock.

**Variables**

- `d[s]`: integer day index (0 = `today`) for each session; domain = allowed days only:
    - home weekday of its slot, plus every other day if `allow_flex_days`
    - minus `unavailable`, minus days after `horizon_end`, never before `today`
    - a pinned session is a plain integer **constant**, not a variable (its index may be negative)
    - an empty domain short-circuits to `Infeasible` rather than being handed to CP-SAT, which would report `MODEL_INVALID` — indistinguishable from a bug
- `home[s]`: boolean, true when `d[s]` falls on its home weekday, via `d[s] % 7` (`add_modulo_equality`). **Not** `AddElement` over a 0/1 list per day: home weekdays are an arithmetic progression, and the table form measured 3.35 s per solve against 0.02 s for modulo (ADR-0009 modification 2)

**Hard constraints**

Each mirrors how the spec 05 validator measures the same thing. Every one skips a pair where **both** sessions are pinned in the past, exactly as the validator does.

- `add_all_different(d)` — at most one session per day, over everything not already past
- `d[k] < d[k+1]` in `seq` order — keeps template order, so each lift's order is kept too
- For **every** pair `s < t` (not just consecutive ones): `d[t] − d[s] ≥ max(rest_days_needed(a, b) for a in lifts[s] for b in lifts[t])`, stated only when that exceeds the gap strict ordering already guarantees. Squat sits on slots 1 and 3, so a consecutive-only rule would skip the squat-to-squat gap whenever slot 2 falls between them. The rule itself comes from `validator.rest_days_needed`, shared rather than re-stated
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

- An **ordering conflict** is reported directly, without the ladder: a free session that must fall before a pinned day already in the past (Monday missed, Wednesday trained anyway, now Thursday) cannot be fixed by any relaxation. The block has to be reshaped — spec 08's step-back — and naming a relaxation would be a lie.
- Otherwise retry **cumulatively** with: (1) `allow_flex_days = True`, (2) no `hard_end_date`, (3) `horizon_end + 28`. Steps that would change nothing are skipped and not listed in `relaxations_tried`.
- When a relaxation works, return `Infeasible` naming it — **not** the relaxed options. Going off-weekday or abandoning a meet date changes what the athlete asked for; spec 08 re-solves once a human has agreed.

---

### C. `to_plan(draft_sessions, option) -> tuple[DraftSession, ...]`

- Applies an option's dates to the session list for the validator and the proposal payload, via `model_copy` (`DraftSession` is frozen). Raises `ValueError` for a seq the plan does not have.

### C2. `to_changes(draft_sessions, option) -> tuple[SessionChange, ...]`

- One `move` per session whose date **actually changed**, with accurate `from_date` and `to_date`, and nothing else. ADR-0007: exactly the sessions placed — the approval path derives `approved_off_weekday` from this list, so over-reporting silently widens the waiver.

### C3. `cost_of(problem, assignments) -> CostBreakdown`

- The objective recomputed in plain Python. Stated twice on purpose: a test asserts it equals CP-SAT's objective, so a mis-stated objective surfaces instead of silently optimising for the wrong thing.

---

## 6. Changes to existing files

- `domains/strength/validator.py` — `_rest_days` split into a public `rest_days_needed(lift_a, lift_b, overrides, config)`. The solver must *build* schedules satisfying the rule the validator *judges* by, and two implementations would drift.
- `migrations/env.py` — `fileConfig(..., disable_existing_loggers=False)`. The default is `True`, and this module is imported in process, so every logger already created was switched off and the solver's dropped-option warnings vanished.

---

## 7. Files to Change

- `backend/domains/strength/validator.py`
- `backend/migrations/env.py`

---

## 8. Files to Create

- `backend/domains/strength/calendar_solver.py`
- `backend/tests/test_calendar_solver.py`

---

## 9. Dependencies

- `ortools` (9.15.6755; pulls in numpy, pandas, protobuf, absl-py)

---

## 10. Solver Settings

| Parameter | Value |
| --- | --- |
| `max_time_in_seconds` | 5.0 total across all solves (tests: 2.0) |
| `num_workers` | 8 (tests: 1, for determinism). `num_search_workers` is deprecated in ortools 9.15 |
| `random_seed` | 0 |

---

## 11. Rules for Implementation

- Build the model from plain data; no DB access inside the solver
- Pure: no clock, no randomness either. `today` arrives on the problem, and elapsed time is read from the solver — asserted by an AST test mirroring the validator's
- Day indexes relative to `today`; convert to dates only at the edges. A negative index is a session whose planned day has passed
- Every option must pass the spec 05 validator before it is returned; options that fail are dropped and logged. The gate is the required `check` argument, not a convention
- Never recompute a weight, a set or a rep (CLAUDE.md non-negotiable #1). Only dates change
- Never re-date a session in `history_seqs` (ADR-0007)
- Record solver stats (`status`, `objective`, `wall_time`, `num_booleans`) on every option **and** on `Infeasible` for tracing (spec 14)

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
- `hard_end_date` before `today` → `ValueError` (a setup error, not an infeasible calendar)
- Duplicate `seq`, or a non-pinned session whose slot has no home weekday → `ValueError`
- Every allowed day blocked out → `Infeasible` naming the session, not `MODEL_INVALID`
- Solver time-out with no solution → `Infeasible("time limit")`
- A missed session trapped before a session already trained → `Infeasible` naming the `order` conflict, with no relaxation attempted

---

## 14. Definition of Done

- [x] All five behaviours above covered by tests
- [x] Options are distinct and sorted by cost
- [x] Every returned option passes the validator
- [x] Solver stats attached to results

Completed 2026-10-13; 31 tests in `backend/tests/test_calendar_solver.py`, suite at 546. Accepted with nine modifications — see **ADR-0009**, which also records the prerequisite **ADR-0008** (5 kg / nearest plate increment) this spec's test block is generated with.

**Carried to spec 08:** `approved_off_weekday` is derived only from the current proposal's changes, so a session left off its home weekday by an *earlier* approved proposal fails `off_weekday` and gets its option dropped. `services.validation_context` should also waive seqs whose stored status is `moved`.
