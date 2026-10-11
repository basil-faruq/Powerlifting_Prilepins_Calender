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

- `POST /templates/import`, `GET /templates/{id}`
- `PUT /athletes/{id}/one-rep-maxes`
- `PUT /athletes/{id}/weekday-map?dry_run=true` (live check) and without `dry_run` (save)
- `POST /blocks/preview`, `POST /blocks`
- `POST /proposals/{id}/approve`

---

## 4. Steps

---

### Step 1 — Template

- If no template exists: file picker for `.xlsx` (accept `B1_Mine.xlsx`); shows "77 entries · 11 weeks · 3 days" on success
- If one exists: shows it with a "Replace" option

---

### Step 2 — 1RMs

- Number inputs (kg, step 0.5) for squat, bench, deadlift, press, row; prefilled from current 1RMs
- Plate increment (1 / 1.25 / 2.5 kg) and rounding (floor / nearest); default 1 kg floor like your sheet

---

### Step 3 — Lifts

- Checkbox per lift; row labelled "accessory"
- Shows which template days each lift uses (from template entries)
- At least one lift required

---

### Step 4 — Weekdays

- For each template day used by the chosen lifts: a weekday selector (Mon–Sun)
- Calls the dry-run endpoint on every change (debounced 300 ms) and shows problems inline, e.g. "Squat on Sunday and Monday is only 1 day apart"
- Note under the form: "These days are locked once the block starts. You can change them later only through a proposal."

---

### Step 5 — Start and preview

- Date picker limited to **Mondays**, default next Monday
- Calls `/blocks/preview`; shows:
    - summary: sessions count, end date, test dates per lift
    - a compact table: week × day with lifts and kg
    - validator warnings (collapsed by default), violations (blocking)
- **Create block** → `POST /blocks` → **Approve** button on the returned proposal → navigate to `/calendar`

---

## 5. Components to Implement

- `SetupWizard` (stepper, keeps state in URL + local state)
- `TemplateStep`, `OneRmStep`, `LiftsStep`, `WeekdayStep`, `PreviewStep`
- `BlockPreviewTable` (reused by spec 19 for proposal previews)

---

## 6. Changes to backend

- None expected; add small endpoints only if a step needs data not yet exposed

---

## 7. Files to Change

- `frontend/src/router.tsx`

---

## 8. Files to Create

- `frontend/src/pages/SetupPage.tsx`
- `frontend/src/features/setup/*.tsx`
- `frontend/src/components/BlockPreviewTable.tsx`
- `frontend/src/features/setup/__tests__/WeekdayStep.test.tsx`

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

---

## 12. Expected Behavior

- Fresh database → wizard completes in under two minutes with your sheet
- Choosing Mon / Wed / Sun → inline error; Next disabled
- Choosing only squat and deadlift → weekday step asks for Day 1, 2 and 3 (squat uses Day 1 and 3, deadlift Day 2)
- After approval → calendar shows 33 sessions

---

## 13. Error Handling Expectations

- Import error → shows the server's cell reference ("B1 Squat!F12: can't read intensity")
- Preview violations → listed with lift / week / day; Create disabled
- Network error → retry button, entries kept

---

## 14. Definition of Done

- [ ] All five steps work against the real API
- [ ] Live weekday validation
- [ ] Preview table with warnings and violations
- [ ] Create + approve lands on the calendar
- [ ] Component test for the weekday step
