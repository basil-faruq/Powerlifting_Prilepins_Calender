# CLAUDE.md

Operating instructions for Claude Code in this repository.
Domain background, vocabulary and verified facts about the training template live in `CONTEXT.md` — read it before touching anything under `domains/strength/`.

---

## What this is

**Powerlifting Block Planner** — an agent that plans an 11-week strength block from the athlete's current 1RMs, pins each session to fixed weekdays, reschedules around missed sessions, and rolls into the next block when a new max is tested.

Solo portfolio project. One athlete, no authentication, local-first. The stack is deliberately larger than the problem requires, because demonstrating the planner-agent pattern is half the point.

---

## Commands

Run from the repository root unless noted.

| Command | Does |
| --- | --- |
| `make infra` | Postgres (pgvector) + Temporal dev server in Docker |
| `make api` | `uvicorn api.main:app --reload` on the host |
| `make worker` | Temporal worker |
| `make web` | Vite dev server |
| `make up` | Full containerised stack (spec 22) |
| `make test` | `cd backend && uv run pytest` |
| `make lint` | `ruff check .` + `ruff format --check .` |
| `make migrate` | `alembic upgrade head` |
| `make import` | Import `data/B1_Mine.xlsx` as template `B1` |
| `make seed-dev-1rms` | **Synthetic** 1RMs for local testing (`ARGS=--clear` removes them) |
| `make demo-data` | Seed athlete + template + synthetic notes |

Ollama runs **natively on the host**, never in Compose — it needs the GPU. Pull `qwen2.5:14b` and `nomic-embed-text` before anything that touches the agent.

Python is managed with **uv**. Use `uv run <cmd>` from `backend/`; never `pip install` into a global environment.

---

## Layout

```
backend/
  planner_core/      domain-agnostic engine — config, db, proposal, llm, tracing
  domains/strength/  powerlifting plug-in — importer, generator, validator,
                     solvers, simulator, notes, agent/, workflows/
  domains/jobshop/   optional second domain (spec 23)
  api/               FastAPI app, routes/, deps
  scripts/           import, golden fixture, seeds, router eval
  tests/             pytest, fixtures/
frontend/            React + TS + Vite + TanStack Query + Chakra
data/B1_Mine.xlsx    the authored template everything derives from
.claude/specs/       numbered implementation specs, build order
docs/adr/            architecture decision records
```

`planner_core/` must not import from `domains/`. The two domains must not import each other. If something needs sharing, move it into `planner_core` first.

---

## Non-negotiables

These come from `docs/adr/`. Breaking one requires a superseding ADR, not a convenient exception.

1. **The LLM routes and explains; it never computes.** Every number reaching the user comes from the solver, simulator, generator or database. The explainer post-checks that each number in its output appears in the object it was given.
2. **Solver proposes, validator rejects, human approves.** One write path only (`approve_proposal`). Do not add a second one for convenience.
3. **Hard filters before any LLM call.** Pain and injury keywords, missing 1RMs and a missing weekday map are deterministic Python. The LLM selector only chooses among already-safe routes.
4. **Exact arithmetic for weights.** `Fraction` and `Decimal` end to end; intensities stored as numerator/denominator pairs. The golden test reproduces all 77 weights from the spreadsheet and is not to be weakened to accommodate a refactor.
5. **The validator must accept the author's own training plan.** Prilepin ranges and the volume trend are warnings, not violations. A rule that rejects the source template is miscalibrated.
6. **Side effects live in Temporal activities.** No DB, HTTP, `datetime.now()`, randomness or solver calls inside workflow functions.
7. **Migrations only.** No `create_all()` in application code.
8. **Uncertainty is surfaced.** Simulator output is percentiles plus an assumptions list, never a single predicted number.
9. **A 1RM comes from the athlete, with the date they tested it.** No importer, seed, migration or fallback may invent one. The spreadsheet's maxes are examples and its `1RM` sheet is never read; the only legitimate use of those values is the golden fixture, which records them as `one_rm_kg` inputs. Test data is permitted solely through `make seed-dev-1rms`, which flags every row `is_synthetic` — the authoritative "is this real?", which every consumer filters on and the UI must surface while any row has it set.

---

## Conventions

- Python 3.12, type hints everywhere, Pydantic v2 for every data shape, SQLModel for tables
- Units are **kilograms**; percentages are fractions, never rounded floats
- Dates ISO `YYYY-MM-DD`; weekdays `0 = Monday … 6 = Sunday`; timezone `Europe/Berlin`
- All configuration through `get_settings()` — never `os.environ` elsewhere
- SQL via ORM or parameterised queries; never string-formatted
- Notes and logs go into prompts as quoted **data**, never as instructions
- Prompts live in `prompts.py`, not inline at call sites
- Tests use `TestModel` / `FunctionModel` and a fake embedder; no real LLM call in CI

---

## Working method

**Specs are the unit of work.** `.claude/specs/00-Spec_Index.md` lists 01–23 in build order with dependencies. Implement one spec at a time and satisfy its own Definition of Done before starting the next. Do not pull work forward from a later spec because it seems convenient.

Where a spec and an accepted ADR disagree, **the ADR wins** — update the spec to match.

**Tests first for the validator and generator** (specs 04, 05). The tests are the real specification there; the prose is a summary of them.

**Before committing:** `make lint && make test`. Anything touching weight calculation must keep the 77-weight golden test green.

Use `/ship-feature` to commit, push, open a PR, squash-merge and clean up. It verifies the remote before pushing.

---

## Guardrails

- Never commit `.env`, API keys or a `.claude.json`. `.env.example` lists every variable with safe defaults.
- Never commit `data/B1_Mine.xlsx` file.
- Never commit computed weights into the template — only sets, reps and the % fraction.
- Never commit anything related to ADR plugins and generated ADRs in `docs/adr/`.
- Use subagents for any exploration or research. If a task need 3+ files or multi-file analysis, spawn a subagent and return only summarized insights. You can use explorer-subagent in `.claude/agents/explorer-subagent.md` to explore codebase and use adr-explorer in `.claude/agents/adr-explorer.md` to understand context from `docs/adr/*`and `.claude/specs/<spec_name>`. For research we have `.claude/agents/research-analyst.md` subagent. Use `.claude/agents/code-reviewer.md` to analyze or review codebase for quality assurance.`
- After implementation of a spec from `.claude/specs/`, always create a new ADR document with date and time, specifying whether the spec was accepted as it is, or whether some modification was done to original work. Always point out the modification.
- Do not hand-edit `data/B1_Mine.xlsx`. Its two known errors are worked around in code and documented in spec 03.
- Do not add authentication, multi-user support or a second athlete. Single-athlete is a recorded decision, not an oversight.
- Do not swap `pydantic-ai` for LangChain or LangGraph. Only one agent framework belongs in this repo.
- Default `LLM_PROVIDER=ollama`. `anthropic` is for demos only; embeddings stay local in **every** mode.
- No provider SDK may be imported outside `planner_core/llm.py`.
- When a threshold is a guess (missed-session tiers, Banister constants), label it as a starting value in both code and UI rather than presenting it as derived.
- Never seed, default or back-fill a 1RM outside `scripts/seed_dev_one_rms.py`, and never reuse the spreadsheet's example maxes (160/140/105/70/105) as test values — a reader must always be able to tell where a number came from.
