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

---

## 4. Database Schema

- No changes

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

---

## 13. Error Handling Expectations

| Error | HTTP |
| --- | --- |
| Not found | 404 |
| `PlanInvalid` (validator violations) | 422, body lists violations |
| `ProposalStale`, already decided, `WeekdayLocked` | 409 |
| `TemplateImportError` | 400 with the cell reference |
| Bad weekday map (`check_weekday_map` problems) | 422 with problems |

---

## 14. Definition of Done

- [ ] All routes implemented and visible in `/docs`
- [ ] End-to-end API test: import → weekday map → block → approve → log
- [ ] Approval is the only path that writes sessions
- [ ] Error codes match the table
