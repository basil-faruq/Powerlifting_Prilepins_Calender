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
| `/` | redirect → `/calendar` if a block exists, else `/setup` | 16 |
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
- Header shows athlete name, current block number and week (from `/blocks/{id}/state`)
- Pending-proposal badge on Chat when `useProposals("pending")` is non-empty

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

- Add `GET /athletes` (list) if not present
- CORS already allows `http://localhost:5173`

---

## 7. Files to Change

- `backend/api/routes/athletes.py` (list endpoint)
- `Makefile`: `web` (`cd frontend && npm run dev`), `gen-api` (regenerate types)

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

- `react`, `react-dom`, `react-router-dom`
- `@tanstack/react-query`, `@tanstack/react-query-devtools`
- `@chakra-ui/react` (v3) and its peer dependencies
- `openapi-fetch`; dev: `openapi-typescript`, `typescript`, `vite`, `@vitejs/plugin-react`, `eslint`, `vitest`, `@testing-library/react`

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
- Works at 375 px width without horizontal scroll

---

## 13. Error Handling Expectations

- API down → full-page `ErrorState` with "Backend not reachable at …" and a retry button
- 4xx/5xx from a mutation → toast with the server's message

---

## 14. Definition of Done

- [ ] App shell with responsive navigation
- [ ] Typed client generated from OpenAPI
- [ ] Athlete/block context available to all pages
- [ ] Lift colours and formatters shared
- [ ] `npm run build` and lint pass
