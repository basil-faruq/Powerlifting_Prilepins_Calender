> **Revised 2026-10-27 (ADR-0022).** Implemented and accepted with modifications. The
> ones that change this document most:
>
> - **The plate increment is a warning, not a refusal** (§10). The increment is a
>   setting (ADR-0008) and microplates exist; the record must say what was actually
>   lifted. The other bounds are mirrored by a new API-layer `LoggedLiftIn`.
> - **There is no made/missed field.** A miss is logged as zero reps, which is exactly
>   what stops the backend recording a max (ADR-0005 mod 5).
> - **`UnavailableDialog` lives in `components/`**, not `features/log/`, because the
>   calendar's drawer opens it too and no feature may import another.
> - **The block table is headed "planned against", not "1RM"** — a restart plans at
>   reduced maxes on purpose (ADR-0010 mod 6), so it will disagree with the chart.
> - **`router.tsx` needed no change** (§7); spec 16 already registered both routes.
>
> Running it found three backend defects under the one action this spec exists for:
> three workflows registered on no worker (so every log **hung**), a worker with no
> embedder (so indexing succeeded having written nothing), and indexing awaited rather
> than started. Saving a session went from never returning, to 10.3 s, to 0.73 s.
>
> Read `docs/adr/0022-session-logger-and-progress.md` before changing anything here.

## 1. Overview

Two screens you use at and after the gym:

- **Session logger** — phone-first form prefilled with today's prescription; log what you did plus a note; or tell the app you can't train.
- **Progress** — your 1RM per lift across blocks.

Logging is what feeds everything else: the workflow (signal), new 1RMs after tests, and the notes RAG.

---

## 2. Depends on

- 15-Temporal_Workflow
- 16-Frontend_Setup

---

## 3. Routes

Frontend: `/log` (optional `?session=`), `/progress`

Backend calls used:

- `GET /blocks/{id}/sessions?from=today&to=today` (or `?session=`)
- `POST /sessions/{id}/log`
- `POST /blocks/{id}/unavailable`
- `GET /athletes/{id}/one-rep-maxes/history`

---

## 4. Screens

---

### A. Session logger

- Picks today's session; if none today, shows the next one with "Log anyway" (for a session done a day early or late)
- Per lift: prescribed `sets × reps @ kg` shown; inputs prefilled with the prescription:
    - sets done, reps per set (single value or per-set list), weight, optional RPE (6–10)
- Test single: big "Made it" / "Missed it" buttons instead of inputs
- Note textarea: placeholder "How did it feel? Sleep, bar speed, anything sore?"
- **Save** → `POST /sessions/{id}/log` → toast "Logged" → back to calendar
- **I can't make it** → dialog: from date (default today), until date (optional), "injury" checkbox → `POST /blocks/{id}/unavailable`; explains that a reschedule proposal will appear in Chat

---

### B. Progress

- Line chart: 1RM per lift over time (one line per lift, lift colours), points marked by source (tested = filled, estimated = hollow, imported = square)
- Table below: block number, start date, 1RM per lift at start, change vs previous block
- Lift filter chips

---

## 5. Components to Implement

- `LogPage`, `LiftLogRow`, `TestSingleButtons`, `UnavailableDialog`
- `ProgressPage`, `OneRmChart`, `BlockHistoryTable`

---

## 6. Changes to backend

- If not present: `GET /athletes/{id}/blocks` (list with snapshot) for the history table

---

## 7. Files to Change

- `frontend/src/router.tsx`
- `backend/api/routes/athletes.py` (blocks list, if needed)

---

## 8. Files to Create

- `frontend/src/pages/LogPage.tsx`, `ProgressPage.tsx`
- `frontend/src/features/log/*.tsx`, `frontend/src/features/progress/*.tsx`
- `frontend/src/features/log/__tests__/LiftLogRow.test.tsx`

---

## 9. Dependencies

- `recharts` (progress chart)

---

## 10. Validation (client side, mirrored by the API)

- Sets 0–10, reps 0–20, weight 0–500 kg in steps of the athlete's plate increment
- Note ≤ 2,000 characters

---

## 11. Rules for Implementation

- Logger usable one-handed on a 375 px screen: large tap targets, numeric keyboards
- Prefill means a session done exactly as prescribed is one tap: **Save**
- Pain words in the note are not blocked in the UI; the backend flags them (spec 12)

---

## 12. Expected Behavior

- Log today's session as prescribed → calendar cell shows done; workflow receives `session_logged`
- Tap "Made it" on squat W11 test at 151 kg → progress chart gains a new tested point at 151
- "I can't make it" for 9 days → pending reschedule proposal appears in Chat

---

## 13. Error Handling Expectations

- Session already logged → form opens in read-only mode with "Edit log" (PUT later; v1 read-only)
- API error on save → inputs kept, retry button

---

## 14. Definition of Done

- [ ] Logger prefilled, one-tap save for as-prescribed sessions
- [ ] Test single made / missed flow
- [ ] Unavailable dialog sends the signal
- [ ] Progress chart and block history table
- [ ] Works at phone width
