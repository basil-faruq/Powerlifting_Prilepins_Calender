## 1. Overview

Decide **what to do when you can't train**, based on how long you are out, then let the calendar solver (spec 07) fill in the dates.

Your weekdays stay locked. A short miss moves one session; a week out repeats a week; illness steps the block back; a long break restarts it on a re-estimated 1RM. Every outcome is a **reschedule proposal** that needs your approval.

The thresholds are **starting values to tune**, not research findings.

---

## 2. Depends on

- 07-Calendar_Solver

---

## 3. Routes

- No new routes (called by the agent in spec 13 and the workflow in spec 15)

---

## 4. Database Schema

- No changes

---

## 5. Functions to Implement (`domains/strength/missed_policy.py`)

---

### A. `MissedPolicyConfig` (Pydantic, defaults)

| Field | Default | Meaning |
| --- | --- | --- |
| `short_max_days` | 2 | back within this many days → move one session |
| `week_max_days` | 10 | up to this → repeat the last completed week |
| `illness_max_days` | 21 | up to this → step back two template weeks |
| `restart_one_rm_factor` | 0.95 | multiplier on re-estimated 1RM when restarting (placeholder) |
| `lower_one_rm_pct` | 0.05 | "lower 1RMs and restart" alternative |
| `early_weeks_carry_on` | 3 | misses in weeks 1–3 may simply continue |

---

### B. `MissedEvent`

- `block_id, missed_seqs: list[int], unavailable_from: date, unavailable_until: date | None, injury: bool = False, today: date`
- `days_out = (unavailable_until or today) − unavailable_from` in days

---

### C. `classify(event, block_state, cfg) -> Tier`

| Tier | When |
| --- | --- |
| `short` | `days_out ≤ short_max_days` and one session missed |
| `week` | `days_out ≤ week_max_days` |
| `illness` | `days_out ≤ illness_max_days` |
| `restart` | longer, or `injury = True` |

---

### D. `propose(event, block_state, cfg, solver_opts) -> list[Proposal]` (max 3, ranked)

| Tier | Default option | Also offered |
| --- | --- | --- |
| `short` | **Move**: calendar solver with `allow_flex_days = True`, horizon to end of next week | **Skip** (only if the session holds only accessory lifts or its top % < 60%); **fold** into the next session of the same lifts (that session keeps its own numbers) |
| `week` | **Repeat week**: insert a copy of the last completed template week (new `seq`s, action `repeat_week`), push the rest by 7 days | **Carry on** when the miss falls in weeks 1–`early_weeks_carry_on` |
| `illness` | **Step back two weeks**: resume from template week `current − 2`, push end date | **Lower 1RMs** by `lower_one_rm_pct` and restart from week 1 |
| `restart` | **Restart** from week 1 with `new 1RM = best estimate_1rm from logs in the last 4 weeks × restart_one_rm_factor` (falls back to the snapshot if no logs) | **Re-test first**: schedule a test single per lift, then restart |

Each option:

1. builds the new plan (dates via `calendar_solver.solve`, numbers via the generator for changed 1RMs)
2. runs the validator with `approved_off_weekday` = the seqs this option moves
3. is dropped if invalid (reason logged)
4. becomes a `Proposal(kind="reschedule", source=…)` with `cost` from the solver and `reasons` in plain words (e.g. "Moves Monday's squat/bench to Tuesday; rest of the week unchanged")

---

### E. Missed test rule

- If a missed session holds a lift's test single and no later test is logged, that lift's next block starts from the **old** 1RM (handled by `next_block_one_rms`, spec 04)

---

## 6. Changes to existing files

- None

---

## 7. Files to Change

- None

---

## 8. Files to Create

- `backend/domains/strength/missed_policy.py`
- `backend/tests/test_missed_policy.py`

---

## 9. Dependencies

- None new

---

## 10. Plain-Language Reasons (templates)

- Move: "Moves {weekday} {lifts} to {new_weekday} {date}. Nothing else changes."
- Repeat week: "Repeats week {n} so you return at the same intensity. Block ends {k} days later."
- Step back: "Drops back two weeks to week {n}. Block ends on {date}."
- Restart: "Restarts at week 1 on estimated 1RMs: {lift kg list}."

---

## 11. Rules for Implementation

- The policy never writes to the DB; it only returns proposals
- `injury = True` always goes to `restart`, and the proposal's `reasons` include "Get the injury checked by a physio before progressing"
- All thresholds come from `MissedPolicyConfig`; no magic numbers in code
- A `fast=True` flag swaps the CP-SAT call for a greedy "next valid home day" placement (used by the simulator in spec 10)

---

## 12. Expected Behavior

- Miss Monday W3, back Tuesday → tier `short`; first option moves the session to Tuesday or Wednesday
- Out 8 days in W5 → tier `week`; first option repeats W4, block end +7 days
- Out 2 weeks in W6 → tier `illness`; first option resumes at W4
- Out 5 weeks → tier `restart`; new 1RMs come from logs × 0.95
- Miss in W2 for 6 days → `week`, with "carry on" offered

---

## 13. Error Handling Expectations

- No valid option survives validation → return an empty list plus a `PolicyFailed` reason the agent can show
- Event for a block that is not `active` → `ValueError`

---

## 14. Definition of Done

- [ ] All four tiers implemented with their alternatives
- [ ] Every returned proposal passes the validator
- [ ] Reasons are plain sentences with dates and lifts
- [ ] `fast` mode tested to give the same tier and a valid plan
