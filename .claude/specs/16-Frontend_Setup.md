> **Revised 2026-10-19 (ADR-0018).** Implemented and accepted with six
> modifications. The ones that change this document most:
>
> - **`/` keys off the *active* block, not "a block exists".** `GET
>   /athletes/{id}/blocks/current` answers 404 for a `draft` (a wizard whose
>   block is not yet approved), and that 404 means "go to `/setup`". A draft has
>   no sessions; sending someone mid-setup to an empty calendar would strand them.
> - **`GET /blocks/{id}/state` can say `none`, `unknown` or `error`**, and the
>   header renders each. `none` is a 200 (ADR-0017), not a failure. When the
>   workflow cannot be queried -- no worker running -- the header reads "status
>   unavailable" and the app carries on.
> - **`react-router-dom` is `react-router`.** v8 moved everything into one
>   package; the spec's dependency list is wrong for it.
>
> Read `docs/adr/0018-frontend-setup.md` before changing anything here.

## 1. Overview

Scaffold the **React + TypeScript + Vite** frontend with **TanStack Query** for server state and **Chakra UI** for components, plus a typed API client generated from FastAPI's OpenAPI schema.

This step delivers the app shell: navigation, layout, theme, API wiring and a current-athlete context. Feature screens come in specs 17–21.

---

## 2. Depends on

- 06-Core_API

---

## 3. Routes (frontend pages)

| Path | Page | Spec |
| --- | --- | --- |
| `/` | redirect → `/calendar` if an **active** block exists, else `/setup` | 16 |
| `/setup` | Setup wizard | 17 |
| `/calendar` | Block calendar | 18 |
| `/chat` | Chat panel + approvals | 19 |
| `/log` | Session logger | 20 |
| `/progress` | 1RM progress | 20 |
| `/decisions` | Decision trace | 21 |

---

## 4. Data Layer

- **Types:** `openapi-typescript` generates `src/api/schema.d.ts` from `http://localhost:8000/openapi.json`
- **Client:** `openapi-fetch` instance in `src/api/client.ts`, base URL from `VITE_API_URL` (default `http://localhost:8000`)
- **Queries:** one hook file per area in `src/api/hooks/` (e.g. `useBlockSessions(blockId, range)`, `useProposals(status)`), with query keys in `src/api/keys.ts`
- **Mutations** invalidate the related keys (e.g. approving a proposal invalidates `sessions`, `proposals`, `blockState`)

---

## 5. Components to Implement

---

### A. `AppShell`

- Desktop: left sidebar with nav items (Calendar, Chat, Log, Progress, Decisions, Setup)
- Phone (< 768 px): bottom tab bar with Calendar, Log, Chat; others under a "More" menu
- Header shows athlete name, current block number and week (from `/blocks/{id}/state`). The week is the **next session's** week. `state` may be `none` ("not started"), `unknown`, `error` or a workflow state; each has a label, none of them is an error page
- Pending-proposal badge on Chat when `useProposals("pending")` is non-empty. The status is **always passed**: the route defaults an omitted one to `pending`, so omitting it never means "all"
- A **synthetic-data banner**, shown for as long as the athlete's `has_synthetic_one_rms` is true (ADR-0004 rule 4)

---

### B. `AthleteProvider`

- Single-user app: loads the first athlete (`GET /athletes` — add a list endpoint if missing) and current block
- Exposes `athleteId`, `currentBlockId` via context

---

### C. Shared UI

- `ErrorState`, `EmptyState`, `LoadingState`
- `LiftBadge` (consistent colour per lift used everywhere: squat, bench, deadlift, press, row)
- `formatKg`, `formatPct`, `formatDate` helpers (`Intl`, locale `en-GB`, timezone `Europe/Berlin`)

---

## 6. Changes to backend

- Added `GET /athletes` (list, oldest first, `[]` on a fresh database)
- Added `GET /templates` (list, newest first, entry *count* rather than rows) -- needed by spec 17 step 1, which had no way to learn a template's id
- CORS already allows `http://localhost:5173` -- and only that: `127.0.0.1:5173` is refused, so use `localhost`

---

## 7. Files to Change

- `backend/api/routes/athletes.py` (list endpoint), `backend/api/routes/templates.py` (list endpoint), `backend/api/schemas.py` (`TemplateSummaryOut`)
- `Makefile`: `web` (`cd frontend && npm run dev`), `gen-api` (regenerate types), `test-web` (`vitest --run`)

---

## 8. Files to Create

- `frontend/package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `.env.example`
- `frontend/src/main.tsx`, `App.tsx`, `router.tsx`, `theme.ts`
- `frontend/src/api/client.ts`, `schema.d.ts`, `keys.ts`, `hooks/*.ts`
- `frontend/src/components/AppShell.tsx`, `AthleteProvider.tsx`, `LiftBadge.tsx`, `states/*.tsx`
- `frontend/src/lib/format.ts`
- `frontend/src/pages/*.tsx` (placeholders)

---

## 9. Dependencies

- `react`, `react-dom`, `react-router` (v8; `react-router-dom` is not used)
- `@tanstack/react-query`, `@tanstack/react-query-devtools`
- `@chakra-ui/react` (v3) and its peer dependencies
- `openapi-fetch`, `next-themes` (Chakra v3's colour mode); dev: `openapi-typescript`, `typescript` (**5.9.3, not 7** -- `typescript-eslint` and `openapi-typescript` do not yet support it), `vite` (8), `@vitejs/plugin-react`, `eslint` (10, flat config only), `vitest`, `jsdom`, `@testing-library/react`, `@testing-library/dom`, `@testing-library/jest-dom`
- Every version is pinned exactly. Node >= 24.15 (`.nvmrc`, `engines`)

---

## 10. Lift Colours

| Lift | Token |
| --- | --- |
| Squat | blue |
| Bench | orange |
| Deadlift | green |
| Press | navy (dark blue) |
| Row | gray |

Defined once in `theme.ts`; match the chart in the project spec. Supports light and dark mode.

---

## 11. Rules for Implementation

- All API calls go through generated types; no hand-written response types
- Server state only in TanStack Query; local UI state in components (no Redux)
- Every page handles loading, empty and error states
- Strict TypeScript (`"strict": true`)

---

## 12. Expected Behavior

- `make web` opens the app at `http://localhost:5173`
- With no athlete/block → `/` goes to `/setup`
- With an active block → `/` goes to `/calendar`; header shows "Block 1 · Week 3"
- With only a draft block → `/` still goes to `/setup`
- No worker running → the header reads "Block 1 · status unavailable" and everything else works
- Works at 375 px width without horizontal scroll

---

## 13. Error Handling Expectations

- API down → full-page `ErrorState` with "Backend not reachable at …" and a retry button
- 4xx/5xx from a mutation → toast with the server's message

---

## 14. Definition of Done

- [x] App shell with responsive navigation
- [x] Typed client generated from OpenAPI
- [x] Athlete/block context available to all pages
- [x] Lift colours and formatters shared
- [x] `npm run build` and lint pass
