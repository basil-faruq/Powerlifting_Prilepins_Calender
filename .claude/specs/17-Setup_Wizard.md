> **Revised 2026-10-19 (ADR-0019).** Implemented and accepted with nine
> modifications. The ones that change this document most:
>
> - **Step 2 collects the date each max was tested.** Non-negotiable 9 and
>   `PUT /one-rep-maxes` both require `set_on`; the spec had number inputs only.
>   Nothing is pre-filled, and nothing may be dated in the future.
> - **Plate steps are 5 / 2.5 / 1 kg, defaulting to 5 kg / nearest** (ADR-0008). The
>   spec's 1 / 1.25 / 2.5 kg and "default 1 kg floor" predate it; 1.25 and 2.5 kg are
>   not steps a paired-plate bar can make.
> - **A 200 from approve is not "approved".** A first approval only *starts* the block's
>   workflow (ADR-0017) and answers at once, so the response can still say `pending` --
>   briefly with a worker running, indefinitely without one. The wizard waits for the
>   proposal to read `approved` and says plainly when it does not.
>
> Read `docs/adr/0019-setup-wizard.md` before changing anything here.

## 1. Overview

A five-step **setup wizard** that takes you from nothing to a pending block proposal: import the template, enter 1RMs, choose lifts, pick your weekdays, choose a start date, preview, create.

This is where "choose which lifts" and "choose the weekday for each training day, then stick with it" from your original idea become UI.

---

## 2. Depends on

- 16-Frontend_Setup

---

## 3. Routes

Frontend: `/setup` (steps as `?step=1..5`)

Backend calls used:

- `GET /templates` (new, see §6), `POST /templates/import[?replace=true]`, `GET /templates/{id}`
- `POST /athletes` (get-or-create, only on a database with no athlete), `PATCH /athletes/{id}`
- `PUT /athletes/{id}/one-rep-maxes`
- `PUT /athletes/{id}/weekday-map?dry_run=true` (live check) and without `dry_run` (save)
- `POST /blocks/preview`, `POST /blocks`
- `POST /proposals/{id}/approve`, `GET /proposals/{id}` (to wait for the decision)

---

## 4. Steps

Before step 1, on a database with no athlete, a one-field "Who is training?" form calls `POST /athletes`. It is not one of the five steps.

---

### Step 1 — Template

- If no template exists: file picker for `.xlsx` (accept `B1_Mine.xlsx`); shows "77 entries · 11 weeks · 3 days" on success
- If one exists: shows it with a "Replace" option. Replace sends `?replace=true`; a plain second import of the same name is a 409
- An import failure lists the server's `problems[]`, one per line, with their cell references

---

### Step 2 — 1RMs

- Per lift: a weight (kg, step 0.5) **and the date it was tested**. Prefilled only from what the server already holds; a lift left blank is simply not entered
- A weight without a date, or a date in the future (in the athlete's timezone), blocks Next
- Only maxes that differ from what the server holds are sent: the endpoint appends a row per lift
- Plate increment **5 / 2.5 / 1 kg** (the total bar jump) and rounding (nearest / down); default **5 kg / nearest** (ADR-0008). A note says 1 kg / down reproduces the spreadsheet

---

### Step 3 — Lifts

- Checkbox per lift; row labelled "accessory"
- Shows which template days each lift uses (from template entries)
- At least one lift required, and each chosen lift needs a saved max

---

### Step 4 — Weekdays

- For each template day used by the chosen lifts: a weekday selector (Mon–Sun)
- Calls the dry-run endpoint on every change (debounced 300 ms) **with the chosen `lifts` and `template_id`** and shows problems inline, e.g. "Squat on Sunday and Monday is only 1 day apart". Left out, the server judges every lift in the template
- Next stays disabled until the verdict on the *current* choice is in
- The rest-gap rule is a starting value, and the step says so
- Note under the form: "These days are locked once the block starts. You can change them later only through a proposal."
- Next saves the map (the preview is built from the saved one)

---

### Step 5 — Start and preview

- A choice of the **next 26 Mondays**, defaulting to the next one (a block must start on a Monday)
- Calls `/blocks/preview`; shows:
    - summary: sessions count, end date, test dates per lift (all read off the draft's sessions; it carries none of its own)
    - a compact table: week × day with lifts and kg
    - validator warnings (collapsed by default), violations (blocking)
    - a "development data" notice when `has_synthetic_one_rms`
- **Create block** → `POST /blocks` → **Start this block** → `POST /proposals/{id}/approve` → navigate to `/calendar`

---

## 5. Components to Implement

- `SetupWizard` (stepper, keeps state in URL + local state)
- `CreateAthleteStep`, `TemplateStep`, `OneRmStep`, `LiftsStep`, `WeekdayStep`, `PreviewStep`
- `BlockPreviewTable` (reused by spec 19 for proposal previews)

---

## 6. Changes to backend

- Added `GET /templates` in spec 16: step 1 had no way to learn an existing template's id

---

## 7. Files to Change

- `frontend/src/router.tsx`, `frontend/src/pages/SetupPage.tsx`

---

## 8. Files to Create

- `frontend/src/features/setup/*.tsx`, `model.ts`, `approve.ts`, `summary.ts`
- `frontend/src/components/BlockPreviewTable.tsx`
- `frontend/src/features/setup/__tests__/` (`WeekdayStep.test.tsx` and the pure logic)

---

## 9. Dependencies

- None new

---

## 10. Copy

- Step titles: "Your template", "Your maxes", "Your lifts", "Your training days", "Start date"
- Create button: "Create block"; approve button: "Start this block"

---

## 11. Rules for Implementation

- Weekday validation logic lives in the backend; the UI only displays problems
- The wizard never writes sessions; it only creates and approves a proposal
- Each step can be revisited without losing later entries
- No max is ever pre-filled, defaulted or used as a placeholder
- Only `approved` is success; a 200 that says `pending` is not

---

## 12. Expected Behavior

- Fresh database → wizard completes in under two minutes with your sheet
- Choosing Mon / Wed / Sun → inline error; Next disabled
- Choosing only squat and deadlift → weekday step asks for Day 1, 2 and 3 (squat uses Day 1 and 3, deadlift Day 2)
- After approval → calendar shows 33 sessions

---

## 13. Error Handling Expectations

- Import error → shows the server's cell reference ("B1 Squat!F12: can't read intensity")
- Preview violations → listed; Create disabled
- Network error → retry button, entries kept
- Approve (ADR-0017):

| Outcome | Meaning | UI |
| --- | --- | --- |
| 200, `approved` | the plan is committed | `/calendar` |
| 200, still `pending` after ~8 s | no worker has applied the start | "Waiting for the workflow worker"; entries kept; press again |
| 409 "already approved" | an earlier attempt committed; only its answer was lost | `/calendar`, not an error |
| 409 otherwise | the proposal moved on | offer a fresh preview |
| 422 | the plan no longer validates | list the violations |
| 503 | Temporal unreachable | "workflow service is not reachable"; entries kept |

---

## 14. Definition of Done

- [x] All five steps work against the real API
- [x] Live weekday validation
- [x] Preview table with warnings and violations
- [x] Create + approve lands on the calendar
- [x] Component test for the weekday step
