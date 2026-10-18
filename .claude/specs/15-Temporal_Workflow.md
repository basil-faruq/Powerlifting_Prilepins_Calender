> **Revised 2026-10-18 (ADR-0017).** Implemented and accepted with thirteen
> modifications. The three that change this document most:
>
> - **`mark_missed` is gone, and `missed` is never written.** ADR-0010 left
>   "whether `missed` is ever written" open; the answer is no. A session the
>   athlete may still make up stays `planned` with a date in the past, which
>   is the only thing that lets the calendar solver re-place it. `missed`
>   means written off, and nothing but a human writes it off.
> - **`approve`, `reject` and `edit` are Temporal *updates*, not signals.**
>   Those routes must answer with the decided `ProposalOut`, and a signal
>   cannot tell its caller what happened. The other four stay signals.
> - **There is no `illness` tier** (§12). ADR-0010 modification 3 removed the
>   four day-counted tiers in favour of four structural outcomes.
>
> Read `docs/adr/0017-temporal-block-workflow.md` before changing anything
> here.

## 1. Overview

One durable **Temporal `BlockWorkflow`** per block, living for the whole 11 weeks. It sleeps on timers between sessions, notices missed sessions, asks for a reschedule, waits for your approval as a **Temporal update**, and rolls into the next block with `continue_as_new`.

This replaces the direct-commit approval from spec 06: approvals now go **through the workflow**, so the plan's state machine lives in one place and survives restarts.

---

## 2. Depends on

- 06-Core_API
- 08-Missed_Session_Policy
- 13-Agent_Router_And_Tools
- Also, unlisted but used: 04-Block_Generator (`next_block_one_rms`) and
  14-Langfuse_Tracing (`traced`).

It additionally discharges the deferrals ADR-0011, ADR-0012 and ADR-0014 each
left to this spec: shape-preview, simulate and note embedding all become
activities here.

---

## 3. Routes (changed / new)

| Method | Path | Change |
| --- | --- | --- |
| POST | `/proposals/{id}/approve` | a `new_block` with no workflow **starts** one; a `new_block` carrying `supersedes_block_id` (spec 08's restart) is an `approve` **update** to the *superseded* block's workflow, which continues as new into the replacement; everything else is an `approve` update to its own block's workflow |
| POST | `/proposals/{id}/reject` | `reject` update (or rejects directly if no workflow yet) |
| PATCH | `/proposals/{id}` | `edit` update |
| POST | `/sessions/{id}/log` | saves log, then sends `session_logged` signal |
| POST | `/blocks/{id}/unavailable` | `{from, until?, injury?}` → `report_unavailable` signal |
| POST | `/blocks/{id}/pause` · `/resume` | `pause(reason)` / `resume(date)` signals |
| GET | `/blocks/{id}/state` | `current_state` query. A block with no workflow answers 200 with `state = "none"`, not 404 |

---

## 4. Database Schema

- No changes (`block.workflow_id` exists). Workflow id format:
  `block-{block_id}` **of the first block in the chain**. `continue_as_new`
  keeps the workflow id, so a block reached by rolling over is owned by an
  execution named after its predecessor -- and `block.workflow_id`, written
  by the `bind_workflow` activity, is the authority. The API reads the
  column and only derives the id for a block never started. This discharges
  ADR-0007's Action Required. No migration and no index: nothing looks a
  block up *by* `workflow_id`.

---

## 5. Functions to Implement (`domains/strength/workflows/`)

---

### A. Activities (`activities.py`) — all DB, solver and LLM work happens here

| Activity | Does |
| --- | --- |
| `bind_workflow(block_id, workflow_id)` | writes `block.workflow_id` (the chain id) |
| `load_block_state(block_id)` | sessions, statuses and each one's deadline, computed in the athlete's timezone |
| `set_block_status(block_id, status)` | `active` ⇄ `paused` |
| `commit_proposal(proposal_id)` | spec 06 `approve_proposal` logic |
| `reject_proposal(proposal_id, reason)` | |
| `apply_edit(proposal_id, changes)` | re-validate, store |
| `run_missed_policy(event)` | spec 08 → proposals saved as `pending`, `source = workflow`; writes a `decision` row (route `reschedule`, root span `workflow.missed_session`) |
| `finish_block(block_id)` | status `done`; `next_block_one_rms` (spec 04) → saves **only the `estimated` rows**. A tested single was already banked by `log_session` (ADR-0005), and an unchanged max was never measured. Idempotent: `done` is the run marker and each row is checked for first |
| `create_next_block(block_id)` | generator with new 1RMs, same lifts and weekdays, start derived from the plan rather than a clock → `new_block` proposal (`pending`, `source = workflow`) |
| `simulate_block`, `shape_preview`, `index_note` | the three ADR-0011/0012/0014 deferrals, each its own one-activity workflow |

Retry policy: 3 attempts, exponential backoff; solver activities
`start_to_close_timeout = 60 s`, and `simulate_block` 180 s. Deterministic
refusals -- `AlreadyDecided`, `BlockNotRunning`, `NotFound`, `PlanInvalid`,
`ProposalStale`, `WeekdayLocked` -- are **non-retryable**: asking again
cannot change the answer, and a retried `AlreadyDecided` would be reported
in place of whatever really happened.

---

### B. `BlockWorkflow.run(block_id)` (`block_workflow.py`)

States: `ACTIVE → (AWAITING_RESCHEDULE_APPROVAL) → ACTIVE … → PAUSED ⇄ ACTIVE → DONE`,
plus `AWAITING_ROLLOVER_APPROVAL` (distinct from the reschedule wait, or the
UI cannot label the dialog) and `ERROR`.

1. `load_block_state`
2. For each next planned session:
    - `workflow.wait_condition(logged_or_signal, timeout = end of session day + GRACE_HOURS)`, the day boundary in **`athlete.timezone`** (matching `today_for`), computed in the activity and carried as a UTC instant so no clock or `zoneinfo` enters the workflow. `GRACE_HOURS = 12` is a **starting value**, labelled as one.
    - logged → continue
    - timeout → `run_missed_policy` → state `AWAITING_RESCHEDULE_APPROVAL` → wait for an `approve` / `reject` / `edit` update (no timeout) → `commit_proposal` → reload state.
      The session is **not** marked `missed`: it stays `planned` with a date in the past so the solver may re-place it (ADR-0010).
    - if every option is rejected, the policy is not asked again about the same sessions until `MISSED_RETRY_COOLDOWN_HOURS` has passed. Without that the loop is unbounded -- the session is still `planned` and still in the past, so the next pass asks the same question immediately, forever.
3. `report_unavailable` / `pause` signals interrupt the wait and go through step 2's missed path with the given dates
4. All sessions done or skipped → `finish_block` → `create_next_block` → wait for `approve` of the new block's proposal → `workflow.continue_as_new(next_block_id)`
5. Rejecting the next block ends the workflow (`DONE`)

**Updates** (they answer their caller): `approve(proposal_id)`,
`reject(proposal_id, reason)`, `edit(proposal_id, changes)`.

**Signals** (the database already says it): `session_logged(session_id)`,
`report_unavailable(from, until, injury)`, `pause(reason)`, `resume(on_date)`.

`pause` sets the block `paused` and records the day; `resume` sets it back to
`active` and, if anything fell due in between, raises that gap as an absence
so it goes through the one reschedule path rather than a second mechanism.

An `approve` / `reject` / `edit` naming a proposal the workflow does not know
is **ignored and logged**, not failed: the athlete may have approved a
chat-proposed reschedule while the workflow sat waiting on a session. The
*route* is still strict and answers 409 for an already-decided proposal.

Queries: `current_state() -> {state, next_session, pending_proposal_ids}`, `next_session()`

---

### C. Worker (`worker.py`)

- Connects to `settings.temporal_address`, task queue `settings.temporal_task_queue`
- Registers `BlockWorkflow` and all activities
- `make worker` target

---

### D. `api/temporal_client.py`

- Shared Temporal client for the API; helpers `start_block_workflow`, `signal`, `query`

---

## 6. Changes to `domains/strength/services.py`

- `approve_proposal` stays as the activity body; routes no longer call it directly

> **Amended.** "Only that" was wrong. Four additive, keyword-only,
> default-preserving changes were needed, so no existing caller moved:
>
> - `create_block_proposal(one_rms=, source=, new_one_rms=)` -- the rollover
>   computes the next block from the maxes the last one produced, writes the
>   first proposal with `source = "workflow"`, and finally gives
>   `StrengthPayload.new_one_rms` a producer (ADR-0007 "Deliberately not done 6").
> - `record_one_rep_maxes(block_id=, today=)` -- attribution, and a clock the
>   caller supplies. Without the latter a time-skipping test would call every
>   logged date a future one and refuse the whole block.
> - `log_session(embed=)` -- the log route now indexes the note afterwards, in
>   its own activity.
> - new `today_at(athlete, as_of)` -- `today_for` without reading the clock.

---

## 7. Files to Change

- `api/routes/proposals.py`, `sessions.py`, `blocks.py`
- ~~`domains/strength/agent/tools.py` (`log_session` sends the signal)~~ --
  **wrong.** The tools are sync, take a session, and are called *from inside*
  an activity; a tool signalling the workflow that invoked it is circular.
  ADR-0015 already said the workflow reaches the tool through its own
  activity. The only change here is making `missed_seqs` and `resolve_seqs`
  public, since the activities are now a second caller.
- `Makefile` (add `worker`)

---

## 8. Files to Create

- `backend/domains/strength/workflows/__init__.py`, `contracts.py`, `block_workflow.py`, `activities.py`, `worker.py`
- `backend/planner_core/temporal_errors.py`
- `backend/api/temporal_client.py`
- `backend/tests/test_block_workflow.py` (the determinism guards),
  `tests/test_temporal_errors.py`, `tests/test_workflow_contracts.py`,
  `tests/test_unit_of_work.py`, `tests/test_today_at.py`,
  `tests/api/test_workflow_activities.py`, `tests/api/test_workflow_rollover.py`,
  `tests/api/test_block_workflow_run.py` (time-skipping),
  `tests/api/test_workflow_routes.py`

---

## 9. Dependencies

- `temporalio`

---

## 10. Determinism Rules (Temporal)

- Workflow code: no DB, no HTTP, no `datetime.now()` (use `workflow.now()`), no random, no solver calls
- Everything with side effects is an activity
- Pass IDs and small Pydantic models to activities, not ORM objects

---

## 11. Rules for Implementation

- One workflow per block; id `block-{block_id}` (idempotent start)
- Approval only via update → activity; nothing else writes planned sessions
- Missed-session proposals from the workflow and from chat use the same policy code
- `continue_as_new` keeps history short across blocks

---

## 12. Expected Behavior

Tested with `WorkflowEnvironment.start_time_skipping()` (11 weeks run in seconds):

- Log every session → block `done`, new 1RMs saved, next block proposal pending, workflow continues as new after approval
- Skip logging Wednesday W3 → session `missed`, reschedule proposals pending, state `AWAITING_RESCHEDULE_APPROVAL`; approve → dates updated, workflow resumes
- `report_unavailable` for 14 days in W6 → a **structural** outcome, not an
  `illness` tier: ADR-0010 modification 3 removed the day-counted tiers. A
  fortnight away yields `week_shift` where the calendar allows it, and
  `restart` once two elapsed template weeks hold nothing completed. Injury,
  however short, is always `restart`.
- Restart the worker mid-block → workflow resumes from where it was

---

## 13. Error Handling Expectations

- Signal for an unknown proposal → ignored and logged (workflow keeps running)
- Activity failure after retries → workflow stays in its state; `/blocks/{id}/state` shows `error` with the message
- Temporal unreachable from the API → 503 on approve / reject / edit / state /
  pause / resume / unavailable. **Not on the log route**: it commits the
  athlete's record first and signals afterwards, so a 503 would be a lie
  about work already done, and a training log must never be lost to
  infrastructure (ADR-0014). Nothing is lost -- the signal carries no
  information the database does not have, and the deadline timer reloads
  and sees `done` either way.

---

## 14. Definition of Done

- [x] Workflow runs a full block in the time-skipping test
- [x] Missed session → proposals → approval → resumed plan
- [x] Roll-over to the next block via `continue_as_new`
- [x] API approvals go through the workflow, and logs signal it after committing
- [x] **Worker restart resumes correctly** -- verified by hand, not by a test.
  A running block was paused with the worker stopped; the signal was accepted
  by the server, and when the worker came back it processed it and the block
  reached `paused` in the database. Resuming brought it back to `active`.
  The automated suite cannot express this: its activities share the test's
  single database connection, so a second worker cannot be started alongside
  the first (see ADR-0017's "Still open").
