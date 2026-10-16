# Powerlifting Block Planner

An agent that plans an 11-week strength block from the athlete's current 1RMs, pins each session to fixed weekdays, reschedules around missed sessions, and rolls into the next block when a new max is tested.

The spreadsheet this replaces knows "Day 1, Day 2, Day 3" — not Monday/Wednesday/Friday, not dates, and it has no answer for a missed session. The planner assigns dates, proposes repairs when a session is missed, and refuses plans that break the rules. A solver proposes, a validator rejects, and a human approves; the LLM routes and explains but never computes a number.

Solo portfolio project: one athlete, no authentication, local-first. Domain background is in [CONTEXT.md](CONTEXT.md); the implementation specs are in [.claude/specs/](.claude/specs/), built in the order given by [00-Spec_Index.md](.claude/specs/00-Spec_Index.md).

**Status:** specs 01 (project setup) and 02 (database schema) complete. Architecture diagram, factory ↔ gym mapping and demo GIF arrive with spec 22.

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
make migrate             # 14 tables + the pgvector extension
make seed                # the athlete (1RMs are entered in the app)
make import              # data/B1_Mine.xlsx -> template B1, 77 entries
make api                 # http://localhost:8000/docs
curl http://localhost:8000/health
```

`make migrate` and `make seed` are both idempotent, so re-running either is safe. `make import` refuses a name that already exists; pass `ARGS=--replace` to overwrite it.

**The spreadsheet supplies the block's pattern, not anyone's numbers.** What is imported is which lift trains on which day of which week, for how many sets and reps, at what intensity — stored as the exact authored fraction (`70/120`), never as a computed weight. The maxes on the workbook's `1RM` sheet are examples and are not read at all. Your own 1RMs, with the date you tested them, are entered in the app, and they are the only maxes the planner computes from.

### Synthetic 1RMs for local testing

The planner needs a max before it can compute anything, so a fresh database has no input. For development only:

```bash
make seed-dev-1rms                          # plausible fixed defaults
make seed-dev-1rms ARGS="--random"          # varied, reproducible via --seed
make seed-dev-1rms ARGS="--kg squat=150"    # override one lift
make seed-dev-1rms ARGS="--clear"           # remove them; real rows are kept
```

Every row it writes is marked twice — `is_synthetic = true` and `source = 'synthetic'` — and a database CHECK stops the two disagreeing. **`is_synthetic` is the authoritative "is this real?"**: filter on it, and clear these rows before trusting anything the planner says. It is deliberately not part of `make seed`.

## Training notes

A note typed when logging a session ("left knee felt tight at the bottom of squats") is chunked, embedded locally and stored in `note_chunk`. It is then retrievable by meaning rather than by keyword:

```bash
curl "localhost:8000/athletes/$ID/notes/search?q=how+has+my+knee+been"
```

"am I sleeping badly?" finds "slept badly again, everything was a grind" without sharing a word with it. Filters: `lift`, `since`, `k` (1–20, default 5).

Two things about this are deliberate and easy to undo by mistake.

**Failing to embed a note never fails the log.** The log is what the athlete did; the embedding is a convenience built on top of it. If Ollama is down the session is still saved, a warning is logged, and `POST /athletes/{id}/notes/reindex` indexes it later. That is also why there is no queue table: `session_log.note` is the source of truth, so a log with a note and no chunks *is* the backlog. Reindex deletes and rebuilds per log, so it is safe to run repeatedly.

**Pain screening does not use embeddings at all.** Whether a lift is flagged as painful is decided by keyword match with negation handling — "no pain today" and "didn't hurt" do not flag anything — because a safety rule whose answer depends on a similarity threshold is a safety rule that can be argued with. It feeds the validator, which refuses to plan a painful lift heavier than it has already been trained. The keyword list, the 7-day window and the chunk sizes are **starting values, not tuned numbers**, and are labelled as such in the code.

### Synthetic demo notes

```bash
make demo-notes            # ~40 synthetic notes across a block (needs Ollama)
make demo-notes ARGS=--clear
make demo-data             # athlete + template + these notes, in one go
```

Every row is flagged `is_synthetic`. Search **hides them** unless you pass `include_synthetic=true`, and pain screening ignores them entirely — a seeded note about a sore knee must never be able to block a real session. `make demo-data` deliberately does not seed 1RMs: a max is a measurement, and bundling an invented one into a convenience target is how it stops being obvious that it was invented.

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
| `make migrate` | `cd backend && uv run alembic upgrade head` |
| `make migrate-sql` | `cd backend && uv run alembic upgrade head --sql` |
| `make seed` | `cd backend && uv run python -m scripts.seed_athlete` |
| `make import` | `cd backend && uv run python -m scripts.import_template ../data/B1_Mine.xlsx $(ARGS)` |
| `make seed-dev-1rms` | `cd backend && uv run python -m scripts.seed_dev_one_rms $(ARGS)` |
| `make demo-notes` | `cd backend && uv run python -m scripts.seed_demo_notes $(ARGS)` |
| `make demo-data` | `seed` + `import` + `demo-notes` |
| `make calibrate` | `cd backend && uv run python -m scripts.calibrate_simulator $(ARGS)` |
| `make check-llm` | `cd backend && uv run python -m scripts.check_llm $(ARGS)` |
| `make test` | `cd backend && uv run pytest` |
| `make lint` | `cd backend && uv run ruff check . && uv run ruff format --check .` |
| `make format` | `cd backend && uv run ruff format .` |

## Layout

```
backend/
  planner_core/      domain-agnostic engine — config, db, errors, proposal, llm, (later) tracing
  domains/strength/  powerlifting plug-in — enums, models, generator, validator,
                     solvers, simulator, notes
  migrations/        Alembic; the only way the schema ever changes
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

## Models

Ollama runs natively on the host, never in Compose, because it needs the GPU. Pull both models once:

```bash
ollama pull qwen2.5:14b        # the agent and the router
ollama pull nomic-embed-text   # embeddings, 768 dimensions
```

`LLM_PROVIDER` switches the agent between local Qwen and Claude and changes nothing else — no other module imports a provider SDK, so the router, the tools and the tests are identical either way. **Embeddings ignore that switch and always run on Ollama.** That is not an omission: the vectors stored in `note_chunk` are only comparable to each other, so letting a demo flag change the embedding model would silently invalidate every note already indexed.

Before building anything on the agent, measure it:

```bash
make check-llm
```

It asks five questions that cannot be answered without calling a tool and reports how many actually did. **Target is 5/5.** The design gives the model no authority to compute anything — every number it reports has to come back from a tool it chose to call — so unreliable tool calling does not degrade the output, it invents weights. Below 5/5, move up to `qwen2.5:32b` before going further.

### Changing the embedding model

`OLLAMA_EMBED_MODEL` cannot be changed on its own. The three have to move together:

1. Set `EMBED_DIM` to the new model's width. `OllamaEmbedder` refuses a vector of any other size rather than letting it reach the database.
2. Migrate `note_chunk.embedding`, which is `VECTOR(768)` — a different width is a schema change, not a setting.
3. Re-embed everything with `POST /athletes/{id}/notes/reindex`. Vectors from two different models are not comparable, so a half-migrated table returns confident nonsense rather than an error.

## Schema

Fourteen tables, created only by Alembic — there is no `create_all()` anywhere in application code. `make migrate-sql` prints the DDL without touching a database if you want to read it first.

Three things about it are deliberate and easy to "fix" by mistake:

- **Percentages are stored as a numerator/denominator pair of `NUMERIC(10,4)`, not as a rounded float and not as integers.** The authored template contains `112.5/105` and `72.5/70`, so integers cannot express it. Exactness is preserved by `Fraction(Decimal(num)) / Fraction(Decimal(den))` — note the single-argument constructor; `Fraction(Decimal, Decimal)` raises `TypeError`.
- **Every enum-like column has a database `CHECK`** built from the `Literal` in [backend/domains/strength/enums.py](backend/domains/strength/enums.py). This is not belt-and-braces: SQLModel `table=True` classes bypass Pydantic validation entirely, so until spec 06 adds request schemas the `CHECK` is the only thing that rejects a bad status.
- **`weekday` is `0 = Monday`**, matching Python's `date.weekday()`. PostgreSQL's `EXTRACT(DOW)` is `0 = Sunday` and `ISODOW` is `1 = Monday`, so comparing the column against either in raw SQL without converting is off by a day.

## Data

`data/B1_Mine.xlsx` is the athlete's own training template and is **not** committed. Its contents, including the two known spreadsheet errors that are worked around in code, are documented in CONTEXT.md §4. Demo notes are synthetic and flagged as such in the database.
