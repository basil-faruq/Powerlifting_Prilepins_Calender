## 1. Overview

> **Amended 2026-10-14, during implementation.** The four day-count tiers
> below were replaced by four **structural** outcomes, and the `illness` tier
> was removed. See §15 for the full list of changes and ADR-0010 for why.

Decide **what to do when you can't train**, based on what the calendar still
allows, then let the calendar solver (spec 07) fill in the dates.

Your weekdays stay locked. If the session fits elsewhere this week, it moves
there; if not, it takes the same weekday next week with the same tonnage and
the rest of the block moves back with it; if you trained past it already, its
lifts fold into the next session of the same lifts; and if two weeks went by
with nothing done, the block restarts on reduced planning maxes. Every
outcome needs your approval.

The remaining thresholds are **starting values to tune**, not research
findings.

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
| `restart_after_empty_weeks` | 2 | consecutive elapsed weeks with nothing completed → restart |
| `restart_one_rm_factor` | 0.95 | multiplier on the restarted block's **planning** maxes |
| `restart_estimate_window_weeks` | 4 | how far back, in template weeks, a restart looks for a logged set |
| `solver_time_limit_s` | 5.0 | total CP-SAT budget for an in-week move |
| `solver_n_options` | 3 | how many placements to offer |
| `solver_seed` | 0 | |
| `solver_num_workers` | 8 | |

Every one of these is labelled a starting value in its `Field(description=…)`.
Seven is deliberately **not** configurable: a week is structural.

---

### B. `MissedEvent`

- `block_id, missed_seqs: tuple[int, ...], unavailable_from: date, unavailable_until: date | None, injury: bool = False, today: date`
- `unavailable_days` — every day the athlete could not train, inclusive. An open window closes the day before `today`.
- `days_out` is retained for reporting only. Classification no longer consults it.

---

### C. `classify(event, block_state, cfg) -> Tier`

Structural, and in this order:

| Tier | When |
| --- | --- |
| *(raises `ValueError`)* | the block is not `active` or `paused`, a `missed_seq` is unknown, or one is already `done` |
| `restart` | `injury = True`, or `restart_after_empty_weeks` consecutive **elapsed** template weeks have no `done` session |
| `fold` | a session numbered after the missed one is already `done` — `seq` order leaves nowhere to put it |
| `in_week_move` | exactly one session missed, `today` is still inside its training week, and at least one day in that week keeps every rest gap |
| `week_shift` | otherwise |

A week is *elapsed* when every session in it is dated before `today`. A
part-elapsed week is counted neither way, because the athlete may still
train it.

The day counts are gone because `unavailable_days` already does their work:
a long absence leaves no feasible day and falls through to `week_shift`
without a threshold being consulted.

---

### D. `propose(event, block_state, ctx, cfg, *, source, fast, …) -> PolicyResult`

| Tier | What it does |
| --- | --- |
| `in_week_move` | Calendar solver with `allow_flex_days = True`, every other session pinned through `history_seqs`, `horizon_end` = the Sunday of that training week. Up to `solver_n_options` placements, ranked by cost. |
| `week_shift` | A deterministic `+7k` translation of every session from the missed one onward, where `k` is the fewest whole weeks that put it on or after `today` and off an unavailable day. No solver: adding a multiple of seven preserves the weekday, the head and tail date ranges stay disjoint, and straddling rest gaps only widen. The session keeps its own sets, reps and weight. |
| `fold` | The missed session's lifts join the next still-planned session that trains any of them; that session keeps its own numbers and both lifts' entries survive. One `SessionChange(action="fold", into_seq=…)`; no dates move. |
| `restart` | `Proposal(kind="new_block")` against a replacement block the **caller** builds, with `payload.supersedes_block_id` naming the block to retire. |

Each option runs the validator with its own moved seqs waived, is dropped if
invalid (rules logged), and becomes a `Proposal` with `cost` from
`calendar_solver.cost_of` and `reasons` in plain words.

`propose` returns a `PolicyResult(tier, proposals, failure)` rather than a
bare list: "nothing valid survived" needs a reason attached, and a list
cannot carry one.

`ctx` is passed in, not built: `test_week` must come from the template
(ADR-0006 rule 6) and a `BlockState` does not carry the template's entries.

---

### E. Missed test rule

- If a missed session holds a lift's test single and no later test is logged, that lift's next block starts from the **old** 1RM (handled by `next_block_one_rms`, spec 04)

---

## 6. Changes to existing files

> **Amended.** "None" was wrong: a pure policy cannot read a running block,
> and two of its four outcomes need the write path to learn something.

- `generator.py` — `draft_session_of`, the declared inverse of
  `persist_planned_sessions`. Also fixes a live lift-ordering bug: `lifts_of`
  reads alphabetically while `generate_block` writes in training order.
- `schemas.py` — `BlockState`; `SessionChange.action` gains `"fold"` and an
  `into_seq`; `StrengthPayload.supersedes_block_id`.
- `enums.py` — `HISTORY_STATUSES`, so the policy, the solver and the services
  cannot drift on what counts as settled.
- `rep_max.py` — `quantize_kg` made public, so a restart's scaled max lands
  on the same grid as every other kilogram.
- `calendar_solver.py` — `to_plan`/`to_changes` accept a plain assignment map
  as well as a `ScheduleOption`, so `week_shift` can reuse the "what counts
  as a move" rule without fabricating a solve.
- `services.py` — `load_block_state`, `propose_for_missed`,
  `build_block_draft(one_rms=…)`, `validation_context(already_moved=…)`,
  `_apply_plan`'s fold branch, `_retire_superseded_block`.

---

## 7. Files to Change

See §6.

---

## 8. Files to Create

- `backend/domains/strength/missed_policy.py`
- `backend/tests/test_missed_policy.py`
- `backend/tests/api/test_missed_and_shaping.py` (the database half)

---

## 9. Dependencies

- None new

---

## 10. Plain-Language Reasons (templates)

- Move: "Moves {weekday} {date}'s {lifts} to {new_weekday} {new_date}. Nothing else changes."
- Week shift: "{lifts} was due {weekday} {date}. It moves to {new_weekday} {new_date} with the same sets, reps and weight, and everything after it moves back {k} weeks. The block now ends {end}."
- Fold: "{lifts} was missed on {weekday} {date} and you have trained since, so it cannot be moved without training out of order. Its lifts join {new_weekday} {new_date}'s session, which keeps its own numbers."
- Restart: "Restarts at week 1 on {date}, on planning maxes of {lift kg list}." Always followed by: "These are planning numbers for the new block, reduced to allow for time off. Your recorded 1RMs are unchanged — only a tested single changes those."

---

## 11. Rules for Implementation

- The policy never writes to the DB; it only returns proposals
- `injury = True` always goes to `restart`, and the proposal's `reasons` include "Get the injury checked by a physio before progressing"
- All thresholds come from `MissedPolicyConfig`; no magic numbers in code
- A missed session the athlete intends to make up stays `planned` with a date in the past until a reschedule is approved, so the solver may re-place it. `missed` is terminal — it means written off. This is what keeps ADR-0007 mod 4 intact without amendment.
- A restart's reduction applies to the new block's `one_rm_snapshot` and **never** writes a `one_rep_max` row (ADR-0005 rule 6, CLAUDE.md #9). It applies whether or not recent logs exist, and is capped so a restart can never raise a max.
- A `fast=True` flag swaps the CP-SAT call for a greedy "next valid home day" placement (used by the simulator in spec 10). It affects `in_week_move` only; the other three outcomes are computed directly and are identical either way.

---

## 12. Expected Behavior

- Miss Monday W3, back Tuesday, nothing trained since → `in_week_move`; the session goes to Tuesday
- Away from W5's Monday until the following Monday → `in_week_move` finds no day, so `week_shift`: the session takes a later Monday with the same tonnage, everything after it moves back, block end slips the same amount
- Miss Monday, train Wednesday anyway → `fold`: Monday's lifts join Friday's session, no dates move
- Injury, however short → `restart`, with the physio line
- Two elapsed weeks with nothing completed → `restart` on maxes reduced by 5%

---

## 13. Error Handling Expectations

- No valid option survives validation → `PolicyResult(failure=PolicyFailed(reason, dropped))`, which the agent can show. `PolicyFailed` is returned, never raised.
- Event for a block that is not `active` or `paused` → `ValueError`. `paused` is running (ADR-0007 §10).
- A `missed_seq` the block does not have, or one already `done` → `ValueError`

---

## 14. Definition of Done

- [x] All four outcomes implemented
- [x] Every returned proposal passes the validator
- [x] Reasons are plain sentences with dates and lifts
- [x] `fast` mode tested to give the same tier, and the **same date**, as CP-SAT

---

## 15. Amendments made during implementation (2026-10-14)

Recorded in ADR-0010.

1. **Day-count tiers → structural outcomes.** `short`/`week`/`illness`/`restart` became `in_week_move`/`week_shift`/`fold`/`restart`, decided by what the calendar allows. `short_max_days`, `week_max_days`, `illness_max_days`, `lower_one_rm_pct` and `early_weeks_carry_on` are gone.
2. **`fold` is new**, and covers the most likely real case — missing Monday and training Wednesday anyway — which none of the original four tiers could express. `seq` order makes a move impossible and `UNIQUE(block_id, seq)` makes renumbering impossible.
3. **`week_shift` needs no solver.** It is a `+7k` translation with a short proof, which also makes `fast` and exact mode identical for it.
4. **`restart` is a `new_block` proposal**, not a `reschedule`. ADR-0007 keeps approval to dates and statuses, and a restart changes the maxes, the weeks and every weight.
5. **The 0.95 factor touches only the block snapshot**, never `one_rep_max`.
6. **"No changes to existing files" was wrong.** See §6.
7. **`propose` returns `PolicyResult`**, not `list[Proposal]`, so a failure can carry its reason.
