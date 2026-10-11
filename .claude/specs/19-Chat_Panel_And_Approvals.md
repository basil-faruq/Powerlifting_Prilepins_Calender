## 1. Overview

The **chat panel** where you talk to the agent, and the **approval cards** where proposals from chat or from the workflow wait for your decision: Approve, Reject or Edit.

This is the human-approval step of the design: nothing reaches your calendar until you press Approve here (or in the setup wizard).

---

## 2. Depends on

- 13-Agent_Router_And_Tools
- 15-Temporal_Workflow
- 16-Frontend_Setup

---

## 3. Routes

Frontend: `/chat` (also opened as a drawer from the calendar banner)

Backend calls used:

- `POST /chat`, `GET /conversations/{id}/messages`
- `GET /proposals?status=pending`
- `POST /proposals/{id}/approve`, `POST /proposals/{id}/reject`, `PATCH /proposals/{id}`

---

## 4. Layout

---

### A. Message list

- User and assistant bubbles; assistant messages show a small **route badge** (`answer`, `plan`, `reschedule`, `simulate`, `safety`, `clarify`)
- Assistant messages with a decision show "View trace" (→ `/decisions?id=`)
- Simulation results render as a small card: finish date p10–p90, per-lift chance of making the test, new 1RM range, assumptions (collapsed)

---

### B. Input

- Text box + send; Enter sends, Shift+Enter new line
- Suggested prompts when empty: "What's on this week?", "I can't train tomorrow", "What if I'm sick for a week in week 6?"
- Disabled with spinner while waiting

---

### C. `ProposalCard`

- Title by kind: "New block", "Reschedule", "Change training days"
- Source tag: "from chat" / "missed session" (workflow)
- **Reasons** (plain sentences from the proposal)
- **Changes** list: `Squat + Bench · Mon 12 Oct → Tue 13 Oct`
- **Cost** line: delay days, sessions off their weekday, block end change
- **Warnings** (collapsible, e.g. Prilepin)
- Buttons: **Approve**, **Reject** (optional reason), **Edit**
- Several options for the same event are shown side by side (stacked on phone) with "Option 1/2/3"; approving one supersedes the others

---

### D. Edit dialog

- For each changed session: date picker; save → `PATCH /proposals/{id}` → card re-renders with the re-validated plan or the violations

---

### E. Pending tray

- Pinned at the top of the chat: all pending proposals, including workflow ones that arrived while you were away

---

## 5. Components to Implement

- `ChatPage`, `MessageList`, `MessageBubble`, `RouteBadge`, `ChatInput`
- `ProposalCard`, `ProposalOptions`, `EditProposalDialog`, `PendingTray`
- `SimulationCard`

---

## 6. Changes to backend

- None expected

---

## 7. Files to Change

- `frontend/src/router.tsx`, `frontend/src/components/AppShell.tsx` (badge)

---

## 8. Files to Create

- `frontend/src/pages/ChatPage.tsx`
- `frontend/src/features/chat/*.tsx`
- `frontend/src/features/proposals/*.tsx`
- `frontend/src/features/proposals/__tests__/ProposalCard.test.tsx`

---

## 9. Dependencies

- None new

---

## 10. Polling

- Pending proposals refetch every 30 s and on window focus, so workflow-created proposals appear without reload

---

## 11. Rules for Implementation

- Approve / Reject buttons disable after click until the request finishes (no double approval)
- After approval, invalidate sessions, proposals and block state queries
- The UI never edits sessions directly; only through proposals

---

## 12. Expected Behavior

- "I can't train tomorrow" → reply + 1–3 option cards; approving one updates the calendar and greys out the others as superseded
- A missed session detected by the workflow → card appears in the pending tray within 30 s
- Edit a proposal to an invalid date → card shows the violation, Approve disabled

---

## 13. Error Handling Expectations

- 409 on approve (stale or already decided) → toast "This proposal is out of date" and refetch
- 503 from `/chat` (LLM down) → message bubble with the error and a retry button

---

## 14. Definition of Done

- [ ] Chat round-trip with route badges
- [ ] Proposal cards with reasons, changes, cost, warnings
- [ ] Approve / Reject / Edit wired to the API
- [ ] Workflow proposals show up via polling
- [ ] Simulation card renders ranges and assumptions
