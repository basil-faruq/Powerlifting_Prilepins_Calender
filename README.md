# Powerlifting Block Planner

An agent that plans an 11-week strength block from the athlete's current 1RMs, pins each session to fixed weekdays, reschedules around missed sessions, and rolls into the next block when a new max is tested.

The spreadsheet this replaces knows "Day 1, Day 2, Day 3" — not Monday/Wednesday/Friday, not dates, and it has no answer for a missed session. The planner assigns dates, proposes repairs when a session is missed, and refuses plans that break the rules. A solver proposes, a validator rejects, and a human approves; the LLM routes and explains but never computes a number.

Solo portfolio project: one athlete, no authentication, local-first. Domain background is in [CONTEXT.md](CONTEXT.md); the implementation specs are in [.claude/specs/](.claude/specs/), built in the order given by [00-Spec_Index.md](.claude/specs/00-Spec_Index.md).

**Status:** spec 01 (project setup) complete. Architecture diagram, factory ↔ gym mapping and demo GIF arrive with spec 22.

---

## Prerequisites

| Need | Why | Install |
| --- | --- | --- |
| Python 3.12 + [uv](https://docs.astral.sh/uv/) | backend | `winget install -e --id astral-sh.uv` |
| Docker Desktop | Postgres + Temporal | `winget install -e --id Docker.DockerDesktop` |
| GNU Make | the targets below | `winget install -e --id ezwinports.make` |
| [Ollama](https://ollama.com) on the **host** | the agent needs the GPU, so it is never in Compose | `ollama pull qwen2.5:14b && ollama pull nomic-embed-text` |

Node 20+ is needed from spec 16 onward for the frontend.

## Quick start

```bash
cp .env.example .env     # optional; every default is already local-safe
make infra               # Postgres on 5432, Temporal on 7233 (UI 8233)
make api                 # http://localhost:8000/docs
curl http://localhost:8000/health
```

`/health` reports each dependency separately and always answers 200, so "the API is up but Postgres is gone" is distinguishable from "the API is gone":

```json
{"status": "ok", "db": "ok", "temporal": "ok", "ollama": "ok"}
```

## Make targets

Each is a thin wrapper; the raw command is given so the Makefile is never the only way in.

| Target | Runs |
| --- | --- |
| `make infra` | `docker compose up -d postgres temporal` |
| `make infra-down` | `docker compose down` |
| `make api` | `cd backend && uv run uvicorn api.main:app --reload` |
| `make test` | `cd backend && uv run pytest` |
| `make lint` | `cd backend && uv run ruff check . && uv run ruff format --check .` |
| `make format` | `cd backend && uv run ruff format .` |

## Layout

```
backend/
  planner_core/      domain-agnostic engine — config, db, (later) proposal, llm, tracing
  domains/strength/  powerlifting plug-in
  api/               FastAPI app
  scripts/           operational scripts
  tests/
data/B1_Mine.xlsx    the authored template everything derives from — not committed
.claude/specs/       numbered implementation specs, in build order
docs/adr/            architecture decision records
```

`planner_core/` must not import from `domains/`, and the domains must not import each other.

## Configuration

Everything is read through `get_settings()` in [backend/planner_core/config.py](backend/planner_core/config.py); nothing else touches `os.environ`. [.env.example](.env.example) lists every variable with its default.

Two notes that cost time otherwise:

- `CORS_ORIGINS` is a list, so it must be JSON: `CORS_ORIGINS=["http://localhost:5173"]`.
- `LLM_PROVIDER=anthropic` without `ANTHROPIC_API_KEY` fails at startup, by design. The default is `ollama`, and embeddings stay local in either mode.

## Data

`data/B1_Mine.xlsx` is the athlete's own training template and is **not** committed. Its contents, including the two known spreadsheet errors that are worked around in code, are documented in CONTEXT.md §4. Demo notes are synthetic and flagged as such in the database.
