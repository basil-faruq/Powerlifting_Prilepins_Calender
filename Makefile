# Run every target from the repository root.
#
# Ollama is not a target: it runs natively on the host so it can use the GPU.
# Pull its models once, before anything that touches the agent:
#   ollama pull qwen2.5:14b
#   ollama pull nomic-embed-text
#
# Two ways to run the project. `make up` is the whole stack in containers
# (spec 22); `make infra` plus `make api`, `make worker` and `make web` is
# development with hot reload. They share one database and one Temporal, so
# never run `make worker` while `make up` is up -- two workers on the same task
# queue would both claim the same block's activities.

# Where a data target runs. On the host by default; `DOCKER=1` sends it into
# the running api container instead, which is what the quick start uses:
#
#   make migrate              uv run, host, against localhost:5432
#   make demo-data DOCKER=1   inside the container, against postgres:5432
#
# The container path is /data because docker-compose.yml mounts ./data there.
#
# DOCKER=1 applies to migrate, migrate-sql, seed, seed-dev-1rms, demo-notes,
# import and demo-data, and to nothing else. The three measurement targets stay
# on the host deliberately: `eval-router` reads tests/, which .dockerignore
# excludes, and `calibrate` writes gains.json into the source tree, which inside
# a container means into a layer the next `make up` throws away.
#
# `docker compose exec` obeys COMPOSE_PROJECT_NAME, so a second stack is
# addressed by exporting that -- NOT by `-p`, which make cannot see:
#
#   COMPOSE_PROJECT_NAME=scratch make demo-data DOCKER=1
ifdef DOCKER
PY := docker compose exec -T api
XLSX := /data/B1_Mine.xlsx
else
PY := cd backend && uv run
XLSX := ../data/B1_Mine.xlsx
endif

.PHONY: help up down logs ps infra infra-down infra-reset api worker migrate migrate-sql seed seed-dev-1rms demo-notes demo-data calibrate import check-llm eval-router test lint format web gen-api test-web

help:
	@echo "make up          THE WHOLE STACK on http://localhost:8080 (builds images)"
	@echo "make down        Stop it, KEEPING data and workflow history"
	@echo "make logs        Follow the api and worker logs"
	@echo "make ps          What is running"
	@echo ""
	@echo "make infra       Postgres (pgvector) + Temporal dev server in Docker"
	@echo "make infra-down  Stop everything in the project, KEEPING workflow history"
	@echo "make infra-reset Stop them and DELETE all data, including workflows"
	@echo "make api         FastAPI with hot reload on http://localhost:8000"
	@echo "make worker      Temporal worker (needs make infra)"
	@echo "make migrate     alembic upgrade head"
	@echo "make migrate-sql Print the migration SQL without running it"
	@echo "make seed        Create the athlete (1RMs are entered in the app)"
	@echo "make import      Import data/B1_Mine.xlsx as template B1 (pattern only)"
	@echo "make seed-dev-1rms  SYNTHETIC 1RMs for local testing (ARGS=--clear to remove)"
	@echo "make demo-notes  SYNTHETIC training notes for retrieval (ARGS=--clear to remove)"
	@echo "make demo-data   Athlete + template + synthetic notes, in one go"
	@echo "make calibrate   Simulator gains from the OpenPowerlifting CSV (ARGS required)"
	@echo "make check-llm   Tool-calling reliability of the configured model (target 5/5)"
	@echo "make eval-router Router accuracy on 30 labelled messages, with a confusion table"
	@echo "make test        pytest"
	@echo "make lint        ruff check + ruff format --check"
	@echo "make format      ruff format"
	@echo "make web         Vite dev server on http://localhost:5173 (needs make api)"
	@echo "make gen-api     Regenerate frontend/src/api/schema.d.ts from the running API"
	@echo "make test-web    Frontend tests (vitest, run once)"
	@echo ""
	@echo "Add DOCKER=1 to migrate, migrate-sql, seed, seed-dev-1rms, demo-notes,"
	@echo "import or demo-data to run them inside the api container instead."

# The whole stack: postgres, temporal, the api, the worker and the app behind
# Caddy on :8080 (spec 22). --build so a code change since the last `up` is
# actually in the image; the layers are cached, so it is quick when nothing
# changed.
up:
	docker compose --profile full up -d --build --remove-orphans

# Keeps both volumes, so the block and its running workflow survive. Use
# `make infra-reset` to throw them away.
down:
	docker compose --profile full down

# The two services whose logs are worth watching: migrations and requests in
# one, activities and timer wake-ups in the other. The profile is needed for
# compose to know the two services exist at all.
logs:
	docker compose --profile full logs -f api worker

ps:
	docker compose --profile full ps

infra:
	docker compose up -d postgres temporal

# `down` is project-wide whatever profile is named, so this stops the full stack
# too if it is up. That is the honest behaviour and not worth hiding: both of
# them keep the volumes.
infra-down:
	docker compose --profile full down

# Throws away the Postgres volume AND the Temporal one. Since spec 15 the dev
# server persists workflow history, so `infra-down` alone no longer resets it --
# this is the deliberate way to start a block over.
infra-reset:
	docker compose --profile full down -v

api:
	cd backend && uv run uvicorn api.main:app --reload

# The other half of the app: workflows sleep on timers between sessions and
# nothing advances a block without this running. `python -m` for the same reason
# as `seed` below -- it keeps backend/ on sys.path rather than the module's own
# directory.
worker:
	cd backend && uv run python -m domains.strength.workflows.worker

migrate:
	$(PY) alembic upgrade head

# Renders the DDL without connecting. Useful for reviewing a migration before
# it touches a database, and the only way to exercise one with Postgres down.
migrate-sql:
	$(PY) alembic upgrade head --sql

# `python -m`, not `python scripts/seed_athlete.py`: the latter puts scripts/ on
# sys.path instead of backend/, so `import planner_core` would fail.
seed:
	$(PY) python -m scripts.seed_athlete

# TEST DATA. Writes 1RMs flagged `is_synthetic` with `source = 'synthetic'`, so
# the planner has something to compute from locally. Deliberately NOT part of
# `make seed`: a real max is the athlete's own measurement, entered with the
# date they tested it, and must never arrive as a side effect of setting up.
#
#   make seed-dev-1rms                          plausible fixed defaults
#   make seed-dev-1rms ARGS="--random"          varied but reproducible
#   make seed-dev-1rms ARGS="--kg squat=150"    override one lift
#   make seed-dev-1rms ARGS="--clear"           remove them; real rows kept
seed-dev-1rms:
	$(PY) python -m scripts.seed_dev_one_rms $(ARGS)

# SYNTHETIC notes, so retrieval can be demonstrated on a fresh database. Needs
# Ollama running, because each note is embedded as it is written. Search hides
# these unless asked for them, and pain screening ignores them entirely.
#
#   make demo-notes ARGS=--clear
demo-notes:
	$(PY) python -m scripts.seed_demo_notes $(ARGS)

# Everything a demo needs except the 1RMs, which are the athlete's to enter.
# `seed-dev-1rms` is deliberately NOT part of this: a max is a measurement, and
# bundling an invented one into a convenience target is how it stops being
# obvious that it was invented (CLAUDE.md non-negotiable 9).
demo-data: seed import demo-notes

# Simulator gains (spec 10). The maxes are required arguments and are never read
# from the database, so a synthetic-seeded one cannot calibrate against invented
# numbers. The CSV (~1 GB, https://openpowerlifting.org/data) is never committed.
#
#   make calibrate ARGS="--csv ../openpowerlifting.csv --squat 145 --bench 100 --deadlift 180"
calibrate:
	cd backend && uv run python -m scripts.calibrate_simulator $(ARGS)

# Run this before building anything on the agent. Needs Ollama up and its models
# pulled; it makes real model calls, which is the whole point -- nothing else in
# this project does. Target is 5/5, because the agent is never allowed to compute
# a number and so has to reach a tool for every fact it states.
#
#   make check-llm ARGS="--runs 10"
check-llm:
	cd backend && uv run python -m scripts.check_llm $(ARGS)

# How often the router picks the right one of four actions, on the model that
# will actually run it (spec 13 §5E). Reads the provider from `.env` and prints
# it first; `anthropic` costs money and needs ARGS="--yes".
#
#   make eval-router ARGS="--verbose"
eval-router:
	cd backend && uv run python -m scripts.eval_router $(ARGS)

# Imports the block's pattern only -- sets, reps and intensity. Never the
# weights Excel computed, and never its `1RM` sheet: those maxes are examples.
#
# The workbook is not committed, so it has to be yours: put it at
# data/B1_Mine.xlsx. $(XLSX) resolves to ../data from backend/ on the host and
# to /data in the container, where docker-compose.yml mounts it read-only.
# Extra flags pass through: make import ARGS="--replace"
#
# No target regenerates tests/fixtures/b1_expected_weights.json on purpose --
# it is the oracle for the 77-weight golden test, so overwriting it stays a
# deliberately typed `uv run python -m scripts.build_golden_fixture`.
import:
	$(PY) python -m scripts.import_template $(XLSX) $(ARGS)

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

format:
	cd backend && uv run ruff format .

# The frontend runs on the host with hot reload, like the API (ADR-0001). Approving
# a block needs `make worker` as well as `make api` -- approval is a Temporal update.
web:
	cd frontend && npm run dev

# Reads the schema from the RUNNING API, so `make api` first. The generated file is
# committed: a reader should not need the backend up to type-check the frontend.
gen-api:
	cd frontend && npm run gen:api

test-web:
	cd frontend && npm run test -- --run
