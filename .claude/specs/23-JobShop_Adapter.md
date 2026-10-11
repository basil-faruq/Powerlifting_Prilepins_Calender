## 1. Overview

**Optional.** A thin second domain, `domains/jobshop`, that solves one classic **job-shop scheduling** instance with the **same** proposal, validation and approval machinery as the powerlifting planner.

It proves the core is domain-agnostic — "a generic plan → solve → simulate → approve engine, with powerlifting as the first plug-in" — and wins back the direct link to production-planning roles that the gym theme gave up. About a day of work.

---

## 2. Depends on

- 05-Validator (generic `Proposal`, `ValidationResult`, `Validator` protocol)
- 15-Temporal_Workflow (only if you also add the approval workflow; the CLI version does not need it)

---

## 3. Routes

- `POST /jobshop/solve` → `{instance: "ta01", time_limit_s?}` → `Proposal` (schedule) + validation
- `POST /jobshop/proposals/{id}/approve` (stores the approved schedule)

---

## 4. Database Schema

- Reuses the `proposal` table with `kind = "jobshop_schedule"` (allow the new value) and a nullable `block_id`; no new tables

---

## 5. Functions to Implement (`domains/jobshop/`)

---

### A. `instances.py`

- `load_taillard(name) -> JobShopInstance` reading the OR-Library / Taillard text format: `jobs × machines`, then per job a list of `(machine, duration)`
- Ship one or two small instances in `domains/jobshop/data/` with their source and citation in a README

---

### B. `solver.py`

- Classic CP-SAT job-shop model:
    - interval var per operation
    - `AddNoOverlap` per machine
    - precedence inside each job
    - objective: minimise makespan
- Returns `JobShopSchedule(operations: [{job, op, machine, start, end}], makespan, stats)`

---

### C. `validator.py`

- Implements the same `Validator` protocol:
    - `machine_overlap`, `precedence`, `duration_mismatch` hard rules
    - `makespan_above_best_known` as a warning (if a best-known value is stored)

---

### D. `service.py`

- `propose_schedule(instance)` → solve → validate → `Proposal` saved as `pending`
- Approval reuses the generic approve path

---

## 6. Changes to existing files

- `planner_core/proposal.py` → `ProposalKind` gains `"jobshop_schedule"`
- `api/main.py` → include `api/routes/jobshop.py`

---

## 7. Files to Change

- `planner_core/proposal.py`, `api/main.py`, migration for the new proposal kind / nullable `block_id`

---

## 8. Files to Create

- `backend/domains/jobshop/__init__.py`, `instances.py`, `solver.py`, `validator.py`, `service.py`, `data/`
- `backend/api/routes/jobshop.py`
- `backend/tests/test_jobshop.py`

---

## 9. Dependencies

- None new (`ortools` already installed)

---

## 10. README Section

- "Same engine, two domains": a two-column table mapping `PlannedSession ↔ Operation`, `weekday_map ↔ machine calendar`, `missed session ↔ machine breakdown`, `rest gap ↔ precedence / setup time`
- Optional small Gantt image of the job-shop result

---

## 11. Rules for Implementation

- `domains/jobshop` must not import anything from `domains/strength`
- Only `planner_core` is shared; if something needs sharing, move it into `planner_core` first
- Keep it small; this is a proof of generality, not a second product

---

## 12. Expected Behavior

- `POST /jobshop/solve {"instance": "ta01"}` → a valid schedule within the time limit, proposal `pending`
- Validator rejects a hand-edited schedule with two operations overlapping on one machine

---

## 13. Error Handling Expectations

- Unknown instance → 404
- Timeout without solution → 422 with `Infeasible("time limit")`

---

## 14. Definition of Done

- [ ] One Taillard instance solved through the shared proposal/validator path
- [ ] No imports between the two domains
- [ ] README "same engine, two domains" section
