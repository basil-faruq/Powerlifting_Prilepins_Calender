> **Revised 2026-10-21 (ADR-0020).** Implemented and accepted with seven
> modifications. The ones that change this document most:
>
> - **The shade reads `rel_intensity`, not "%1RM".** The template's own denominator is
>   the max the block was programmed against, not the athlete's max (ADR-0003), so
>   there is no "%1RM" in the payload to shade by. The five steps take their edges
>   from the Prilepin bands the validator already uses (spec 05 §10) plus the crossing
>   of the max itself.
> - **A past session nobody logged reads "not logged", never "missed".** ADR-0017:
>   "`missed` is never written … `missed` means *written off*, and only a human writes
>   a session off." The state the system actually produces had no rendering here.
> - **Rows are keyed on the Monday of the date, not on `week`.** A repeated week reuses
>   its week number, so keying on `week` collapsed two real training weeks into one row.
>
> Read `docs/adr/0020-block-calendar.md` before changing anything here.

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
    - **Keyed on the Monday, not on `week`.** A `repeat_week` writes fresh seqs carrying the *same* week number at later dates, so `week` is not unique and keying on it loses a row. "W4" may therefore appear on two rows, which is correct
- Columns: Mon–Sun; your training weekdays get a subtle column highlight
- Today: outlined cell; current week row highlighted
    - **"Current week" is the week containing today in the athlete's timezone**, read off the session dates. Deliberately not `state.next_session`: `/blocks/{id}/state` 503s with no Temporal worker running, and the grid must render in full without one (ADR-0018 mod 2). `next_session` is the fallback when today falls outside the block, and the first row the last resort

---

### B. Session cell

- One line per lift: `LiftBadge` + `sets×reps @ kg` (e.g. `SQ 3×10 @ 81`). `LiftBadge` gained a `short` variant for this; the full name stays in its `title`
- Background shade = the session's **highest `rel_intensity`**, on a 5-step sequential scale
    - Not "%1RM": the stored `pct_num/pct_den` is a fraction of the max the block was *programmed against* (ADR-0003), so the payload holds no percentage of the athlete's current max. `rel_intensity` is the authored fraction against what the reps themselves imply, which is also why the scale runs past 100%
    - Steps: `<0.70`, `0.70–0.80`, `0.80–0.90`, `0.90–1.00`, `≥1.00`. The edges are the Prilepin bands of spec 05 §10 plus the crossing of the max; the scale's stated 55% floor and 110% cap fall inside the outer steps, so nothing is clamped. **No bin here is invented**
    - A legend states the basis, because a shade alone cannot
- Status:
    - `done` → check mark + "done"
    - **past, still `planned` or `moved`, unlogged → amber dashed outline + "not logged"**. Derived in the UI, never read from the API. Logging is what writes `done` (`services.log_session`), so a past session still reading `planned` is one nobody logged. This is **not** `missed`: ADR-0017 says `missed` is never written and means *written off*, which only a human does
    - `missed` → red outline + "missed". Kept for a human write-off; no route produces one today
    - `moved` → arrow icon, tooltip "Moved from Mon 12 Oct". `original_date` is the first plan and never the previous hop, so a twice-moved session names where it started
    - `skipped` → greyed, struck through
- Test single → small "TEST" tag
- Every status carries a glyph **and a word**: colour is never the only signal (§11), and there is no icon library in the project

---

### C. Ghost overlay (pending proposal)

- Banner whenever **any** proposal is pending for this block; toggle "Show proposed changes" only when one of them parses into actual moves
- Moved sessions drawn dashed at their new date, faded at the old one
- Clicking the banner opens the proposal in the chat panel (spec 19). Until then it navigates to `/chat`
- `ProposalOut.payload` is `{ [key: string]: unknown }` in the generated schema — the one place the frontend narrows an API shape by hand. It reads `payload.changes[]` for `action: "move"` entries and joins them to live sessions by `seq`. A payload it cannot read yields no moves and never throws
- Only `reschedule` and `weekday_change` draw. A restart arrives as `new_block` carrying a **different** `block_id` and must not be drawn over this block

---

### D. Session drawer

- Click a cell → side drawer: full prescription per lift (sets, reps, %, kg, rel. intensity), log if done, notes, buttons "Log this session" (→ `/log?session=`) and "I can't make it" (→ unavailable dialog, spec 20)

---

## 5. Components to Implement

- `BlockCalendar`, `CalendarWeekRow`, `SessionCell`, `SessionDrawer`, `ProposalOverlayToggle`
- Added while building: `CalendarHeader` (the Mon–Sun headings), `WeekList` (the phone layout of §10), `IntensityLegend` (states the shade's basis)
- `intensityStep(relIntensity)` plus `shadeBg`/`shadeFg`, in place of the spec's `intensityShade(pct)`. It takes the **exact decimal string** and compares scaled `BigInt`s: `rel_intensity` must never pass through `Number()` (CLAUDE.md non-negotiable 4), so the helper cannot take a number

---

## 6. Changes to backend

- None expected (`/blocks/{id}/sessions` returns lifts per session)

---

## 7. Files to Change

- ~~`frontend/src/router.tsx`~~ — already registers `/calendar` from spec 16; no change was needed
- `frontend/src/pages/CalendarPage.tsx` — the `Placeholder` becomes a thin `<BlockCalendar />`
- `frontend/src/theme.ts` — **five `intensity.N.bg` / `intensity.N.fg` semantic token pairs**. §11 requires the scale to come from the theme and to work in dark mode, and only the lift palettes and `synthetic.*` existed. Purple, because the five lifts take blue/orange/green/navy/gray and red carries the written-off and test markers
- `frontend/src/api/types.ts` — alias `SessionDetail`, `SessionLogOut`, `PlannedLiftOut`, `WeekdaySlotOut`, `LoggedLift`
- `frontend/src/api/keys.ts` — a `sessions` namespace, so spec 20's log mutation can invalidate the drawer's copy as well as the grid's list
- `frontend/src/lib/lifts.ts`, `frontend/src/components/LiftBadge.tsx` — `LIFT_SHORT` and a `short` prop, for the narrow day columns
- `frontend/src/lib/format.ts` — `formatDayMonth`, since a cell has room for neither the weekday nor the year
- `frontend/src/lib/date.ts` (**new**) — `todayIso`, `addDays` and `weekdayOf` lifted out of `features/setup/model.ts`, which re-exports them so nothing that used them changed. A feature importing from another feature would have been the alternative

---

## 8. Files to Create

- `frontend/src/features/calendar/*.tsx` — `BlockCalendar`, `CalendarHeader`, `CalendarWeekRow`, `SessionCell`, `WeekList`, `SessionDrawer`, `ProposalOverlayToggle`, `IntensityLegend`
- `frontend/src/features/calendar/model.ts` (week rows, current week, cell state), `intensity.ts` (the shade), `ghost.ts` (reading a proposal's moves)
- `frontend/src/api/hooks/sessions.ts` — `GET /sessions/{id}`, which had no hook; the drawer's notes live on the log
- `frontend/src/features/calendar/__tests__/` — `SessionCell.test.tsx` plus `model.test.ts`, `intensity.test.ts` and `ghost.test.ts` for the pure logic

---

## 9. Dependencies

- None new (CSS grid, no Gantt library)

---

## 10. Phone Layout

- Below 768 px: switch to a **week list** — one card per week with its sessions stacked; current week expanded, others collapsed
- Both layouts live in the DOM and are switched with `display={{ base, md }}`, the idiom `AppShell` and `MobileTabs` already use. Not `useBreakpointValue`: the test setup stubs `matchMedia` to `matches: false`, so it would always resolve to the desktop value under test

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

- No **current** block → `EmptyState` with "Set up a block" button. "Current" means `blocks/current` answered 404; a **paused** block is current and still renders its calendar (ADR-0018 mod 1, ADR-0010 mod 9)
- A current block with no sessions yet → its own `EmptyState`, since sessions are written only when the block is approved
- Sessions request fails → `ErrorState` with retry
- `/blocks/{id}/state` fails (no worker) → "Workflow status unavailable"; the grid is unaffected (ADR-0018 mod 2)
- A query failure is never a toast. Toasts are for mutations, and this screen has none

---

## 14. Definition of Done

- [ ] Grid renders a full block with statuses and shading
- [ ] Ghost overlay for pending proposals
- [ ] Session drawer with full prescription
- [ ] Phone week-list layout
- [ ] Cell component test
