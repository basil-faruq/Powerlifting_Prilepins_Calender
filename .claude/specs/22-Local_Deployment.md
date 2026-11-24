> **Revised 2026-10-28 (ADR-0024).** Implemented and accepted with nine
> modifications. The ones that change this document most:
>
> - **§12 is overridden.** `make up && make demo-data` cannot end with a block on
>   the calendar: `demo-data` writes no 1RMs, by design (ADR-0003 #7, ADR-0004,
>   ADR-0014 mod 10), and nothing can be planned without one. The quick start ends
>   at the setup wizard, where the athlete enters their own maxes;
>   `make seed-dev-1rms` is documented beside it as the labelled synthetic
>   shortcut.
> - **§6's `.env.example` rewrite is rejected.** Container hostnames there would
>   break `make infra` + `make api` on the host. The three addresses that differ
>   (`postgres`, `temporal:7233`, `host.docker.internal:11434`) are set in the
>   compose `environment:` block, which overrides `env_file`, so one `.env`
>   serves both modes.
> - **§5C's "run inside the api container" becomes opt-in.** The data targets
>   still run on the host by default, so the commands in CLAUDE.md and the older
>   README stay true; `DOCKER=1` sends any of them into the container instead.
> - **§3's `/api/docs` needs `--root-path /api`.** Caddy strips the prefix, so
>   without it the docs page loads and then fails to fetch `/openapi.json`.
> - **§4's Temporal persistence is now stated**: SQLite at
>   `/home/temporal/temporal.db` on the `temporaldata` volume, mounted on the
>   image's own home and not a subdirectory. This closes the question ADR-0017
>   left to this spec.
> - **The api's host port 8000 is deliberately not published.** Caddy is the only
>   way in, which is also what lets `CORS_ORIGINS` be `[]` under `make up`.
>
> Read `docs/adr/0024-local-deployment.md` before changing anything here.

## 1. Overview

Run the **whole stack on your own machine** with one command, and finish the repo for others: README, architecture diagram, factory ↔ gym mapping, demo GIF.

Hosting is **local-first**. A public link (Cloudflare Tunnel or a VPS) is an optional, later step described in section 10 — it is not needed to build, use or show the project.

---

## 2. Depends on

- All specs 01–21

---

## 3. Routes

- No new API routes
- Local URLs:
    - App: `http://localhost:8080`
    - API docs: `http://localhost:8080/api/docs`
    - Temporal UI: `http://localhost:8233`

---

## 4. Services (`docker-compose.yml`, full profile)

| Service | Image / build | Notes |
| --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg16` | volume `pgdata` |
| `temporal` | Temporal CLI dev server | pinned tag |
| `api` | `backend/Dockerfile` | runs `alembic upgrade head` then `uvicorn` |
| `worker` | same image, command `python -m domains.strength.workflows.worker` | |
| `web` | `frontend/Dockerfile` (build → Caddy serving static files) | Caddy also reverse-proxies `/api/*` to `api:8000` |

- **Ollama stays on the host** (GPU access). Containers reach it via `http://host.docker.internal:11434`; on Linux add `extra_hosts: ["host.docker.internal:host-gateway"]` to `api` and `worker`
- Langfuse: Cloud free tier via env vars (no container)
- Dev profile from spec 01 (`postgres` + `temporal` only) still works for hot-reload development

---

## 5. Files to Implement

---

### A. `backend/Dockerfile`

- `python:3.12-slim`, install with `uv sync --frozen --no-dev`, non-root user, `EXPOSE 8000`

---

### B. `frontend/Dockerfile` + `frontend/Caddyfile`

- Stage 1: `node` build with `VITE_API_URL=/api`
- Stage 2: `caddy` serving `dist/`, SPA fallback to `index.html`, `/api/*` → `api:8000` (strip prefix)

---

### C. `Makefile` targets

| Target | Does |
| --- | --- |
| `make up` | `docker compose --profile full up -d --build` |
| `make down` | stop everything |
| `make migrate` / `make import` / `make seed` | run inside the `api` container |
| `make demo-data` | seed athlete, import template, seed synthetic notes |
| `make logs` | follow api + worker logs |

---

### D. `README.md`

1. One-paragraph pitch + screenshot
2. Architecture diagram (from the project spec) and the **factory ↔ gym mapping** table
3. Quick start: prerequisites (Docker, Ollama), `ollama pull qwen2.5:14b nomic-embed-text`, `cp .env.example .env`, `make up`, `make demo-data`
4. Switching to Claude for a demo: `LLM_PROVIDER=anthropic` + key
5. Router eval results (Ollama vs Claude) from spec 13
6. Design decisions: hard filters → selector, validator, human approval, Temporal, local-first
7. Demo GIF: setup → calendar → "I can't train tomorrow" → approve → decision trace
8. Note that demo notes are synthetic; data sources and licences (OpenPowerlifting public domain)

---

## 6. Changes to existing files

- `docker-compose.yml` → add `full` profile services
- `.env.example` → container-friendly defaults (`DATABASE_URL` host `postgres`, `TEMPORAL_ADDRESS=temporal:7233`, `OLLAMA_BASE_URL=http://host.docker.internal:11434`)

---

## 7. Files to Change

- `docker-compose.yml`, `.env.example`, `Makefile`, `README.md`

---

## 8. Files to Create

- `backend/Dockerfile`, `backend/.dockerignore`
- `frontend/Dockerfile`, `frontend/Caddyfile`, `frontend/.dockerignore`
- `docs/architecture.png` (export of the spec diagram), `docs/demo.gif`

---

## 9. Dependencies

- Docker + Docker Compose v2
- Ollama on the host

---

## 10. Optional Later: a Public Link

**There is no login in this app.** Before any public URL, add HTTP basic auth in Caddy (`basic_auth` with a bcrypt hash from `caddy hash-password`).

| Option | Cost | When |
| --- | --- | --- |
| **Cloudflare Tunnel** (`cloudflared tunnel --url http://localhost:8080`) | Free | Live interview walkthroughs; only reachable while your machine and the tunnel run |
| **Small VPS** (Hetzner, or Oracle Cloud free ARM tier if capacity allows) | ~€5/month or free | Always-on CV link. Check the GitHub Student Developer Pack first for hosting credits (it doesn't include DigitalOcean) |

- On a VPS without a GPU, run with `LLM_PROVIDER=anthropic` (and a spending cap) or a small CPU model; embeddings still need Ollama (`nomic-embed-text` runs fine on CPU)

---

## 11. Rules for Implementation

- No secrets in images; everything via `.env`
- Containers run as non-root
- `make up` on a clean machine must work with only Docker, Ollama and the README steps

---

## 12. Expected Behavior

- `make up && make demo-data` → app at `http://localhost:8080` with a block on the calendar
- Restarting containers keeps data (volume) and resumes workflows
- Turning off Wi-Fi: everything except Langfuse keeps working (Ollama is local)

---

## 13. Error Handling Expectations

- Ollama not reachable from containers → `/health` shows `ollama: down`; README troubleshooting entry for `host-gateway`
- Migrations fail → `api` container exits with the Alembic error in logs

---

## 14. Definition of Done

- [ ] One-command local start
- [ ] Data persists across restarts; workflows resume
- [ ] README complete with diagram, mapping, quick start, eval table, GIF
- [ ] Basic-auth instructions in place for any future public link
