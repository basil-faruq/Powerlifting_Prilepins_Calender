## 1. Overview

One durable **Temporal `BlockWorkflow`** per block, living for the whole 11 weeks. It sleeps on timers between sessions, notices missed sessions, asks for a reschedule, waits for your approval as a **signal**, and rolls into the next block with `continue_as_new`.

This replaces the direct-commit approval from spec 06: approvals now go **through the workflow**, so the plan's state machine lives in one place and survives restarts.

---

## 2. Depends on

- 06-Core_API
- 08-Missed_Session_Policy
- 13-Agent_Router_And_Tools

---

## 3. Routes (changed / new)

| Method | Path | Change |
| --- | --- | --- |
| POST | `/proposals/{id}/approve` | `new_block` → starts `BlockWorkflow`; other kinds → `approve` signal |
| POST | `/proposals/{id}/reject` | sends `reject` signal (or rejects directly if no workflow yet) |
| PATCH | `/proposals/{id}` | sends `edit` signal |
| POST | `/sessions/{id}/log` | saves log, then sends `session_logged` signal |
| POST | `/blocks/{id}/unavailable` | `{from, until?, injury?}` → `report_unavailable` signal |
| POST | `/blocks/{id}/pause` · `/resume` | `pause(reason)` / `resume(date)` signals |
| GET | `/blocks/{id}/state` | `current_state` query |

---

## 4. Database Schema

- No changes (`block.workflow_id` exists). Workflow id format: `block-{block_id}`.

---

## 5. Functions to Implement (`domains/strength/workflows/`)

---

### A. Activities (`activities.py`) — all DB, solver and LLM work happens here

| Activity | Does |
| --- | --- |
| `load_block_state(block_id)` | sessions, statuses, logs, 1RMs |
| `commit_proposal(proposal_id)` | spec 06 `approve_proposal` logic |
| `reject_proposal(proposal_id, reason)` | |
| `apply_edit(proposal_id, changes)` | re-validate, store |
| `mark_missed(session_ids)` | status → `missed` |
| `run_missed_policy(event)` | spec 08 → proposals saved as `pending`, `source = workflow`; writes a `decision` row (route `reschedule`, root span `workflow.missed_session`) |
| `finish_block(block_id)` | status `done`; `next_block_one_rms` (spec 04) → saves new 1RMs |
| `create_next_block(block_id)` | generator with new 1RMs, same lifts and weekdays, start = next Monday → `new_block` proposal (`pending`) |

Retry policy: 3 attempts, exponential backoff; solver activities `start_to_close_timeout = 60 s`.

---

### B. `BlockWorkflow.run(block_id)` (`block_workflow.py`)

States: `ACTIVE → (AWAITING_RESCHEDULE_APPROVAL) → ACTIVE … → PAUSED ⇄ ACTIVE → DONE`

1. `load_block_state`
2. For each next planned session:
    - `workflow.wait_condition(logged_or_signal, timeout = end of session day + 12 h)` (day boundary in `settings.timezone`)
    - logged → continue
    - timeout → `mark_missed` → `run_missed_policy` → state `AWAITING_RESCHEDULE_APPROVAL` → wait for `approve` / `reject` / `edit` signal (no timeout) → `commit_proposal` → reload state
3. `report_unavailable` / `pause` signals interrupt the wait and go through step 2's missed path with the given dates
4. All sessions done or skipped → `finish_block` → `create_next_block` → wait for `approve` of the new block's proposal → `workflow.continue_as_new(next_block_id)`
5. Rejecting the next block ends the workflow (`DONE`)

Signals: `approve(proposal_id)`, `reject(proposal_id, reason)`, `edit(proposal_id, changes)`, `session_logged(session_id)`, `report_unavailable(from, until, injury)`, `pause(reason)`, `resume(on_date)`

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

---

## 7. Files to Change

- `api/routes/proposals.py`, `sessions.py`, `blocks.py`
- `domains/strength/agent/tools.py` (`log_session` sends the signal)
- `Makefile` (add `worker`)

---

## 8. Files to Create

- `backend/domains/strength/workflows/__init__.py`, `block_workflow.py`, `activities.py`, `worker.py`
- `backend/api/temporal_client.py`
- `backend/tests/test_block_workflow.py`

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
- Approval only via signal → activity; nothing else writes planned sessions
- Missed-session proposals from the workflow and from chat use the same policy code
- `continue_as_new` keeps history short across blocks

---

## 12. Expected Behavior

Tested with `WorkflowEnvironment.start_time_skipping()` (11 weeks run in seconds):

- Log every session → block `done`, new 1RMs saved, next block proposal pending, workflow continues as new after approval
- Skip logging Wednesday W3 → session `missed`, reschedule proposals pending, state `AWAITING_RESCHEDULE_APPROVAL`; approve → dates updated, workflow resumes
- `report_unavailable` for 14 days in W6 → `illness` tier proposals
- Restart the worker mid-block → workflow resumes from where it was

---

## 13. Error Handling Expectations

- Signal for an unknown proposal → ignored and logged (workflow keeps running)
- Activity failure after retries → workflow stays in its state; `/blocks/{id}/state` shows `error` with the message
- Temporal unreachable from the API → 503 on approve / log routes

---

## 14. Definition of Done

- [ ] Workflow runs a full block in the time-skipping test
- [ ] Missed session → proposals → approval signal → resumed plan
- [ ] Roll-over to the next block via `continue_as_new`
- [ ] API approvals and logs go through signals
- [ ] Worker restart resumes correctly
