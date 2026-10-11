## 1. Overview

Create the repository skeleton, local infrastructure and configuration for the **Powerlifting Block Planner**.

This step gives every later spec a place to put code, a running Postgres (with pgvector) and Temporal dev server, and one settings object that all modules read from.

During development the **backend and frontend run on the host** (hot reload); only the infrastructure runs in Docker. The full containerised stack comes in spec 22.

---

## 2. Depends on

Nothing — this is the first step.

---

## 3. Routes

- `GET /health` → `{"status": "ok", "db": "ok" | "down", "temporal": "ok" | "down", "ollama": "ok" | "down"}`
- Nothing else yet

---

## 4. Infrastructure (`docker-compose.yml`, dev profile)

| Service | Image | Port | Notes |
| --- | --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg16` | 5432 | Volume `pgdata`; db `planner`, user `planner` |
| `temporal` | Temporal CLI dev server (`temporal server start-dev --ip 0.0.0.0`) | 7233, UI 8233 | Pin an exact image tag when implementing |

- **Ollama is not in compose.** It runs natively on the host (already installed) so it can use the GPU. Backend reaches it at `http://localhost:11434` in dev, `http://host.docker.internal:11434` from containers (spec 22).
- Langfuse is **not** in compose (Cloud free tier, spec 14).

---

## 5. Functions to Implement

---

### A. `planner_core/config.py` → `Settings`

`pydantic-settings` `BaseSettings`, loaded from `.env`:

| Field | Default | Notes |
| --- | --- | --- |
| `database_url` | `postgresql+psycopg://planner:planner@localhost:5432/planner` | |
| `temporal_address` | `localhost:7233` | |
| `temporal_task_queue` | `strength-planner` | |
| `timezone` | `Europe/Berlin` | |
| `llm_provider` | `ollama` | `Literal["ollama", "anthropic"]` |
| `ollama_base_url` | `http://localhost:11434` | |
| `ollama_chat_model` | `qwen2.5:14b` | |
| `ollama_router_model` | `qwen2.5:14b` | can be smaller later |
| `ollama_embed_model` | `nomic-embed-text` | |
| `embed_dim` | `768` | must match the embed model |
| `anthropic_api_key` | `None` | `SecretStr`, only needed when provider = anthropic |
| `anthropic_model` | `claude-sonnet-5-5` | |
| `anthropic_router_model` | `claude-haiku-5-5` | |
| `langfuse_public_key` / `langfuse_secret_key` | `None` | tracing disabled when missing |
| `langfuse_host` | `https://cloud.langfuse.com` | |
| `langfuse_project_id` | `None` | used to build trace links |
| `cors_origins` | `["http://localhost:5173"]` | |

- `get_settings()` cached with `functools.lru_cache`
- Validator: if `llm_provider == "anthropic"` and no key → raise at startup with a clear message

---

### B. `planner_core/db.py`

- `engine` created from `settings.database_url`
- `get_session()` generator yielding a SQLModel `Session` (FastAPI dependency)
- `ping_db()` → `bool`

---

### C. `api/main.py`

- FastAPI app with title "Powerlifting Block Planner"
- CORS from settings
- `/health` route checking DB, Temporal (connect with 2 s timeout) and Ollama (`GET /api/tags`)
- Health returns 200 even if a dependency is down (status per dependency)

---

## 6. Changes to existing files

- None (new repository)

---

## 7. Files to Change

- None

---

## 8. Files to Create

- `docker-compose.yml`, `.env.example`, `.gitignore`, `Makefile`, `README.md` (stub)
- `backend/pyproject.toml`
- `backend/planner_core/__init__.py`, `config.py`, `db.py`
- `backend/domains/__init__.py`, `backend/domains/strength/__init__.py`
- `backend/api/__init__.py`, `main.py`, `deps.py`, `routes/__init__.py`, `routes/health.py`
- `backend/tests/conftest.py`, `backend/tests/test_health.py`
- `data/B1_Mine.xlsx` (copy of your file)

---

## 9. Dependencies

Backend (`pyproject.toml`), versions pinned when implementing:

- `fastapi`, `uvicorn[standard]`
- `sqlmodel`, `alembic`, `psycopg[binary]`, `pgvector`
- `pydantic-settings`
- `httpx`
- Dev: `pytest`, `pytest-asyncio`, `ruff`

Later specs add their own packages.

---

## 10. Makefile Targets

| Target | Does |
| --- | --- |
| `make infra` | `docker compose up -d postgres temporal` |
| `make api` | `cd backend && uv run uvicorn api.main:app --reload` |
| `make test` | `cd backend && uv run pytest` |
| `make lint` | `cd backend && uv run ruff check . && uv run ruff format --check .` |

---

## 11. Rules for Implementation

- No secrets committed; `.env` in `.gitignore`, `.env.example` lists every variable with safe defaults
- All configuration read through `get_settings()`, never `os.environ` directly elsewhere
- `planner_core` must not import anything from `domains/` (dependency points one way)
- Python package layout must work with `uv run pytest` from `backend/`

---

## 12. Expected Behavior

- `make infra` brings up Postgres and Temporal; Temporal UI reachable at `http://localhost:8233`
- `make api` starts the API; `GET /health` returns JSON with each dependency's status
- With Ollama running and `nomic-embed-text` pulled, health shows `"ollama": "ok"`

---

## 13. Error Handling Expectations

- Missing `.env` → defaults are used; app still starts
- `LLM_PROVIDER=anthropic` without `ANTHROPIC_API_KEY` → startup fails with: `ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic`
- A dependency down → `/health` reports `"down"` for it, does not crash

---

## 14. Definition of Done

- [ ] Repository layout matches `00-Spec_Index.md`
- [ ] `make infra` starts Postgres (pgvector) and Temporal
- [ ] `make api` serves `/health` with per-dependency status
- [ ] `.env.example` lists all settings
- [ ] `make test` and `make lint` pass
