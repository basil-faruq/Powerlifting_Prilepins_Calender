## 1. Overview

The **decision trace** screen: a timeline of every agent decision — what was asked, which route and filter fired, which tools ran with what results, what was proposed, and whether you approved it — with a link to the full Langfuse trace.

This answers "understand every decision it makes" from the original idea, and is the screen to show in an interview.

---

## 2. Depends on

- 14-Langfuse_Tracing
- 16-Frontend_Setup

---

## 3. Routes

Frontend: `/decisions` (optional `?id=` to open one)

Backend calls used:

- `GET /decisions?athlete_id=&route=&outcome=&limit=50`
- `GET /decisions/{id}`

---

## 4. Layout

---

### A. Timeline list

- One row per decision, newest first:
    - time, source (`chat` / `workflow`), `RouteBadge`, filter fired (if any)
    - first line of the user message, or "Missed session W3 Wed" for workflow decisions
    - outcome tag: `approved`, `rejected`, `answered`, `failed`, `pending`
- Filters: route, outcome, source

---

### B. Decision drawer

- **Input**: full message (or workflow event)
- **Routing**: filter results, then the LLM route with extracted parameters
- **Tool calls**: ordered list; each expandable to show input and output summary (JSON viewer), duration
- **Solver / simulator stats**: status, objective, wall time
- **Validator**: ok, violations, warnings
- **Proposal**: link to its card; final outcome and time
- **Open in Langfuse** button when `trace_url` is present; otherwise "Tracing disabled"

---

## 5. Components to Implement

- `DecisionsPage`, `DecisionRow`, `DecisionDrawer`, `ToolCallItem`, `JsonView`

---

## 6. Changes to backend

- None expected (`/decisions` from spec 14)

---

## 7. Files to Change

- `frontend/src/router.tsx`

---

## 8. Files to Create

- `frontend/src/pages/DecisionsPage.tsx`
- `frontend/src/features/decisions/*.tsx`
- `frontend/src/features/decisions/__tests__/DecisionDrawer.test.tsx`

---

## 9. Dependencies

- None new (simple collapsible JSON view, no library)

---

## 10. Empty State Copy

- "No decisions yet. Ask the agent something in Chat, or wait for the workflow to notice a missed session."

---

## 11. Rules for Implementation

- Read-only screen
- Long JSON collapsed by default; never render raw secrets (backend already excludes them)
- Langfuse link opens in a new tab

---

## 12. Expected Behavior

- Every chat turn and every workflow reschedule appears here
- Safety-route decisions show `filter_fired = pain_keyword` and no LLM tool calls
- Approving a proposal later updates the row's outcome on refetch

---

## 13. Error Handling Expectations

- Decision not found (bad `?id=`) → toast and list view
- Fetch error → `ErrorState`

---

## 14. Definition of Done

- [ ] Timeline with filters
- [ ] Drawer with routing, tool calls, stats, validator result, proposal outcome
- [ ] Langfuse link when tracing is on
- [ ] Component test for the drawer
