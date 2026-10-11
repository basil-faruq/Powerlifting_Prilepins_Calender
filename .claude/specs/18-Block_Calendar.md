## 1. Overview

The **block calendar**: the gym version of the factory Gantt chart. Rows are weeks of the block, columns are weekdays, and each cell is a session showing lifts and kg, shaded by intensity, with done / missed / moved states.

It is the main screen. Pending proposals appear as a **ghost overlay** so you can see what a reschedule would change before approving it.

---

## 2. Depends on

- 16-Frontend_Setup

---

## 3. Routes

Frontend: `/calendar`

Backend calls used:

- `GET /blocks/{id}/sessions`
- `GET /blocks/{id}/state`
- `GET /proposals?status=pending`
- `GET /sessions/{id}`

---

## 4. Layout

---

### A. Grid

- Rows: block weeks (W1…W11, plus repeated weeks if any), labelled with the Monday's date
- Columns: Mon–Sun; your training weekdays get a subtle column highlight
- Today: outlined cell; current week row highlighted

---

### B. Session cell

- One line per lift: `LiftBadge` + `sets×reps @ kg` (e.g. `SQ 3×10 @ 81`)
- Background shade = top %1RM of the session (5-step sequential scale from 55% to 110%)
- Status:
    - `done` → check mark
    - `missed` → red outline + "missed"
    - `moved` → arrow icon, tooltip "Moved from Mon 12 Oct"
    - `skipped` → greyed, struck through
- Test single → small "TEST" tag

---

### C. Ghost overlay (pending proposal)

- Toggle "Show proposed changes" when a reschedule proposal is pending
- Moved sessions drawn dashed at their new date, faded at the old one
- Clicking the banner opens the proposal in the chat panel (spec 19)

---

### D. Session drawer

- Click a cell → side drawer: full prescription per lift (sets, reps, %, kg, rel. intensity), log if done, notes, buttons "Log this session" (→ `/log?session=`) and "I can't make it" (→ unavailable dialog, spec 20)

---

## 5. Components to Implement

- `BlockCalendar`, `CalendarWeekRow`, `SessionCell`, `SessionDrawer`, `ProposalOverlayToggle`
- `intensityShade(pct)` helper

---

## 6. Changes to backend

- None expected (`/blocks/{id}/sessions` returns lifts per session)

---

## 7. Files to Change

- `frontend/src/router.tsx`

---

## 8. Files to Create

- `frontend/src/pages/CalendarPage.tsx`
- `frontend/src/features/calendar/*.tsx`
- `frontend/src/features/calendar/__tests__/SessionCell.test.tsx`

---

## 9. Dependencies

- None new (CSS grid, no Gantt library)

---

## 10. Phone Layout

- Below 768 px: switch to a **week list** — one card per week with its sessions stacked; current week expanded, others collapsed

---

## 11. Rules for Implementation

- Read-only screen: no mutations except opening dialogs
- Shade scale and lift colours from `theme.ts`; must work in dark mode
- Colour is never the only signal (status icons and text too)

---

## 12. Expected Behavior

- After setup with Mon / Wed / Fri: 11 rows, 3 filled cells per row
- Logging a session → its cell shows done without a page reload (query invalidation)
- Pending reschedule → banner + ghost overlay toggle
- Phone width → week cards, no horizontal scroll

---

## 13. Error Handling Expectations

- No active block → `EmptyState` with "Set up a block" button
- Sessions request fails → `ErrorState` with retry

---

## 14. Definition of Done

- [ ] Grid renders a full block with statuses and shading
- [ ] Ghost overlay for pending proposals
- [ ] Session drawer with full prescription
- [ ] Phone week-list layout
- [ ] Cell component test
