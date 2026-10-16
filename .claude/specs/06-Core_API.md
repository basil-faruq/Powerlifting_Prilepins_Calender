## 1. Overview

Expose the domain core through **FastAPI**: athletes, 1RMs, template import, weekday map, block preview and creation, sessions, logging, and proposals with approve / reject.

After this spec the app is usable without any AI: you can import your sheet, set your days, create a block, approve it and log sessions. Week 1 ends here.

Approval in this spec **commits directly** to the database. Spec 15 replaces that with a Temporal signal.

---

## 2. Depends on

- 02-Database_Schema
- 03-XLSX_Template_Import
- 04-Block_Generator
- 05-Validator

---

## 3. Routes

| Method | Path | Body / Query | Returns |
| --- | --- | --- | --- |
| POST | `/athletes` | `{name, plate_increment_kg?, rounding?}` | `Athlete` |
| GET | `/athletes/{id}` | | `Athlete` + current 1RMs + weekday map |
| PATCH | `/athletes/{id}` | `{plate_increment_kg?, rounding?, timezone?}` | `Athlete` |
| PUT | `/athletes/{id}/one-rep-maxes` | `[{lift_code, kg, set_on, source}]` | current 1RMs |
| GET | `/athletes/{id}/one-rep-maxes/history` | `?lift=` | list, newest first |
| PUT | `/athletes/{id}/weekday-map` | `{slots: {1: 0, 2: 2, 3: 4}}`, `?dry_run=true` | map + problems |
| POST | `/templates/import` | multipart `.xlsx`, `?name=` | `{template_id, entries: 77}` |
| GET | `/templates/{id}` | | template + entries |
| POST | `/blocks/preview` | `{athlete_id, template_id, lifts, start_date}` | `BlockDraft` + `ValidationResult` |
| POST | `/blocks` | same as preview | `{block_id, proposal}` (block `draft`, proposal `pending`) |
| GET | `/blocks/{id}` | | block + status + snapshot |
| GET | `/athletes/{id}/blocks/current` | | active block or 404 |
| GET | `/blocks/{id}/sessions` | `?from=&to=` | sessions with lifts |
| GET | `/sessions/{id}` | | session + log if any |
| POST | `/sessions/{id}/log` | `{performed_on, entries, note?}` | `SessionLog` |
| GET | `/proposals` | `?athlete_id=&status=pending` | list |
| GET | `/proposals/{id}` | | proposal |
| POST | `/proposals/{id}/approve` | | proposal (`approved`) |
| POST | `/proposals/{id}/reject` | `{reason?}` | proposal (`rejected`) |
| PATCH | `/proposals/{id}` | `{changes: [SessionChange]}` | re-validated proposal |

> **Revised 2026-10-13 per ADR-0007.** Four signatures differ from the table above.
>
> 1. `POST /athletes` is **get-or-create**: it returns the existing athlete rather than creating a second one. Single-athlete is a recorded decision (CLAUDE.md), so a second POST is spec 17's wizard being re-run, and idempotence is what makes that wizard safe to re-enter.
> 2. `POST /templates/import` also accepts `?replace=` — the importer already supports it, spec 17's wizard offers it, and without it a corrected spreadsheet is unimportable (a permanent 409).
> 3. The weekday-map body accepts optional `template_id` and `lifts` alongside `slots`. `check_weekday_map` judges only the slots the chosen lifts need, and `{slots}` alone cannot say which those are. Omitted, the check runs against the newest template and every lift it programmes — the strictest reading, and the only one available before a block exists.
> 4. `GET /athletes/{id}` additionally returns `has_synthetic_one_rms`, and every 1RM in every response carries `is_synthetic` (ADR-0004 rule 4). `PUT /athletes/{id}/one-rep-maxes` accepts `source` from the client but **only** `tested | estimated | imported`: `synthetic` is unrepresentable in the request model rather than merely rejected, which answers ADR-0004's Action Required.

---

## 4. Database Schema

- No changes

> **Revised 2026-10-13 per ADR-0007** (migration `0004`). One column was unavoidable: §3 signs `POST /proposals/{id}/reject` with `{reason?}` and the `proposal` table had nowhere to put it. `reasons` is why the *solver* proposed the change — what spec 21's decision trace reads to explain a proposal — so writing a human's rejection there would corrupt it. `proposal.decision_reason TEXT NULL` is the human's half of the record, and it pairs with the `decided_at` that 0001 already had.

---

## 5. Functions to Implement

---

### A. `domains/strength/services.py` (business logic; routes stay thin)

- `create_block_proposal(session, athlete_id, template_id, lifts, start_date) -> Proposal`
    - Generates, validates; if `ok = False` → raise `PlanInvalid(violations)`
    - Persists block as `draft` and proposal `new_block` / `pending` with `StrengthPayload`
- `approve_proposal(session, proposal_id) -> Proposal`
    - `new_block`: block → `active`, sessions persisted, weekday map `locked_from = start_date`
    - `reschedule`: applies `changes` (moves set `status = moved`, keep `original_date`; skips set `skipped`)
    - Re-validates against the **current** block state first; stale → `ProposalStale`
    - Marks other pending proposals for the same block `superseded`
- `reject_proposal(session, proposal_id, reason)`
- `edit_proposal(session, proposal_id, changes) -> Proposal` — applies edits to the plan, re-validates, stores result (still `pending`)
- `log_session(session, session_id, data) -> SessionLog` — sets session `done`; if an entry is a completed test single, records the new 1RM (`source = tested`)

> **Revised 2026-10-13 per ADR-0007.** Five points this section left open.
>
> 1. **`persist_block` is split** (ADR-0005 deferred the question here) into `persist_block_row` and `persist_planned_sessions`. `create_block_proposal` calls the first — `proposal.block_id` is NOT NULL, so the block row must exist at proposal time — and `approve_proposal` calls the second. `persist_block` remains as a composition of the two, so the generator suite is unaffected.
> 2. **Staleness is defined.** For `new_block`: the block is no longer `draft`, or already has sessions. For `reschedule` / `weekday_change`: the block is not `active` or `paused`, a `move`'s `from_date` is not the date that session currently has, or the plan re-dates a session already `done`/`missed`. State drift is 409; fresh validator violations at approval are 422 — the plan is not wrong, it is answering a question the database has left behind.
> 3. **Approval handles every action, not just `move` and `skip`.** `_apply_plan` reconciles the block's sessions against `payload.plan` by `seq`, which serves `move`, `skip`, `create` and `repeat_week` through one code path. `weekday_change` gained the payload spec 05 never defined: `WeekdayChangePayload(slots, changes, plan)`.
> 4. **`approved_off_weekday` is derived from the proposal's own changes.** A session a proposal explicitly places *is* the off-weekday move the human is being asked to approve; judging it against the home weekday would make every reschedule unapprovable and spec 08 impossible. Seqs the proposal does not mention are still held to the map.
> 5. **`edit_proposal` applies only `move`.** A `create` or `repeat_week` by hand would have to invent sets, reps and weights, which is the generator's job and never an edit's — that is a new proposal. A failing edit is a 422 rather than a stored invalid payload, since `Proposal` has nowhere to record violations (ADR-0006).
>
> `log_session` reads "a completed test single" as ADR-0005 modification 5 defines it: logged **at or above** the prescribed weight with at least one rep completed, and the new max is the weight actually lifted. A miss, a failure (`reps_done = 0`) or a heavy *working* set records nothing.

---

### B. Routes (`api/routes/athletes.py`, `templates.py`, `blocks.py`, `sessions.py`, `proposals.py`)

- Request / response bodies are Pydantic models in `api/schemas.py` (not SQLModel tables)
- Map domain errors to HTTP codes (section 13)

---

## 6. Changes to `api/main.py`

- Include the new routers
- Register exception handlers for `PlanInvalid`, `ProposalStale`, `WeekdayLocked`, `TemplateImportError`

---

## 7. Files to Change

- `api/main.py`

---

## 8. Files to Create

- `backend/domains/strength/services.py`
- `backend/api/schemas.py`
- `backend/api/routes/athletes.py`, `templates.py`, `blocks.py`, `sessions.py`, `proposals.py`
- `backend/tests/api/test_blocks.py`, `test_proposals.py`, `test_sessions.py`

> **Revised 2026-10-13 per ADR-0007.** Also created: `backend/planner_core/errors.py` (the proposal-lifecycle errors — `NotFound`, `PlanInvalid`, `ProposalStale`, `AlreadyDecided` — which belong to the engine, not to this domain, since spec 23's jobshop reaches the same four dead ends); `backend/migrations/versions/0004_proposal_decision_reason.py`; and `backend/tests/api/{__init__,conftest}.py` plus `test_athletes.py`, `test_templates.py` and `test_flow.py`. `WeekdayLocked` lives in `services.py` — a weekday map is a strength-domain idea.
>
> Also changed: `planner_core/db.py` gains `make_engine(url)` and `tests/conftest.py` an `api_client` fixture that overrides `get_session`, which is **ADR-0002 modification 16** discharged — without it a route test writes to the developer's real database. `domains/strength/schemas.py` gains `WeekdayChangePayload`, and `tests/conftest.py`'s `db_session` gains `join_transaction_mode="create_savepoint"` so a route's `commit` releases a savepoint instead of committing the test's transaction.

---

## 9. Dependencies

- `python-multipart` (file upload)

---

## 10. Status Transitions

| Object | Allowed transitions |
| --- | --- |
| block | `draft → active` (approve), `draft → abandoned` (reject), `active ⇄ paused`, `active → done` |
| proposal | `pending → approved / rejected / superseded` |
| session | `planned → done / missed / moved / skipped`, `moved → done / missed` |

---

## 11. Rules for Implementation

- Routes contain no business logic; they call `services.py`
- Only `approve_proposal` writes `planned_session` changes (the human approval gate)
- Weekday map changes while a block is `active` → rejected; must go through a `weekday_change` proposal
- OpenAPI schema must be clean (the frontend generates types from it in spec 16)

---

## 12. Expected Behavior

- Import sheet → set Mon/Wed/Fri → preview shows 33 sessions and `ok = True`
- `POST /blocks` → pending proposal; `GET /blocks/{id}/sessions` returns nothing until approved
- Approve → 33 sessions visible; weekday map locked
- Logging squat W11 Day 1 at 151 kg × 1 → new squat 1RM 151 kg (`tested`)
- Second approve on the same proposal → 409

> **Revised 2026-10-13 per ADR-0007.** The logging example's numbers are barred from tests: 151 derives from the spreadsheet's example squat max of 140, and CLAUDE.md's guardrail plus ADR-0004 require that a reader can always tell where a number came from. `tests/api/` uses its own maxes (`API_MAXES`: squat 125, bench 95, deadlift 145, press 55, row 85 — distinct from both the spreadsheet's and `seed_dev_one_rms`'s), and reads the prescribed weight back from the API rather than writing it down, which is stronger: a test that hard-codes the number it expects cannot notice the generator changing it.
>
> The start date is likewise not the golden fixture's fixed 2026-01-05. A block entirely in the past satisfies the four calendar rules vacuously — they all judge from `ctx.today` onward (ADR-0006) — so the route tests start on the next Monday, which is the only way they exercise `off_weekday` and `rest_gap` at all.

---

## 13. Error Handling Expectations

| Error | HTTP |
| --- | --- |
| Not found | 404 |
| `PlanInvalid` (validator violations) | 422, body lists violations |
| `ProposalStale`, already decided, `WeekdayLocked` | 409 |
| `TemplateImportError` | 400 with the cell reference |
| Bad weekday map (`check_weekday_map` problems) | 422 with problems |

> **Revised 2026-10-13 per ADR-0007.** Four rows the table was missing.
>
> | Error | HTTP |
> | --- | --- |
> | `TemplateExistsError` (name taken, `replace=false`) | 409 — deliberately not a `TemplateImportError` (ADR-0003), and `?replace=true` is the way past it where nothing fixes a bad cell |
> | An upload that is not a readable `.xlsx` (`BadZipFile`, `InvalidFileException`) | 400, as `TemplateImportError`. Translated in the route, not the importer: only an upload can be junk, and spec 03's error is about a workbook's *contents* |
> | A second log for the same session | 409, pre-checked rather than left to `uq_session_log_planned_session_id` — an `IntegrityError` aborts the whole transaction (ADR-0002 rule 1) |
> | `generate_block`'s `ValueError` (missing 1RM, non-Monday start, a lift the template has no rows for) | 422, wrapped as a one-violation `PlanInvalid` so a client has one error body to parse rather than two |
>
> Every refusal shares one body (`ProblemOut`): `detail` for a generic client, and `violations` / `warnings` / `problems` for one that shows them against the sessions they name. A rejected plan's warnings travel with it — a plan can be refused *and* carry advice worth showing.

---

## 14. Definition of Done

- [ ] All routes implemented and visible in `/docs`
- [ ] End-to-end API test: import → weekday map → block → approve → log
- [ ] Approval is the only path that writes sessions
- [ ] Error codes match the table
