# Run every target from the repository root.
#
# Ollama is not a target: it runs natively on the host so it can use the GPU.
# Pull its models once, before anything that touches the agent:
#   ollama pull qwen2.5:14b
#   ollama pull nomic-embed-text

.PHONY: help infra infra-down api migrate migrate-sql seed seed-dev-1rms calibrate import test lint format

help:
	@echo "make infra       Postgres (pgvector) + Temporal dev server in Docker"
	@echo "make infra-down  Stop them"
	@echo "make api         FastAPI with hot reload on http://localhost:8000"
	@echo "make migrate     alembic upgrade head"
	@echo "make migrate-sql Print the migration SQL without running it"
	@echo "make seed        Create the athlete (1RMs are entered in the app)"
	@echo "make import      Import data/B1_Mine.xlsx as template B1 (pattern only)"
	@echo "make seed-dev-1rms  SYNTHETIC 1RMs for local testing (ARGS=--clear to remove)"
	@echo "make calibrate   Simulator gains from the OpenPowerlifting CSV (ARGS required)"
	@echo "make test        pytest"
	@echo "make lint        ruff check + ruff format --check"
	@echo "make format      ruff format"

infra:
	docker compose up -d postgres temporal

infra-down:
	docker compose down

api:
	cd backend && uv run uvicorn api.main:app --reload

migrate:
	cd backend && uv run alembic upgrade head

# Renders the DDL without connecting. Useful for reviewing a migration before
# it touches a database, and the only way to exercise one with Postgres down.
migrate-sql:
	cd backend && uv run alembic upgrade head --sql

# `python -m`, not `python scripts/seed_athlete.py`: the latter puts scripts/ on
# sys.path instead of backend/, so `import planner_core` would fail.
seed:
	cd backend && uv run python -m scripts.seed_athlete

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
	cd backend && uv run python -m scripts.seed_dev_one_rms $(ARGS)

# Simulator gains (spec 10). The maxes are required arguments and are never read
# from the database, so a synthetic-seeded one cannot calibrate against invented
# numbers. The CSV (~1 GB, https://openpowerlifting.org/data) is never committed.
#
#   make calibrate ARGS="--csv ../openpowerlifting.csv --squat 145 --bench 100 --deadlift 180"
calibrate:
	cd backend && uv run python -m scripts.calibrate_simulator $(ARGS)

# Imports the block's pattern only -- sets, reps and intensity. Never the
# weights Excel computed, and never its `1RM` sheet: those maxes are examples.
#
# ../data, because the recipe cds into backend/ first, the same as migrate.
# Extra flags pass through: make import ARGS="--replace"
#
# No target regenerates tests/fixtures/b1_expected_weights.json on purpose --
# it is the oracle for the 77-weight golden test, so overwriting it stays a
# deliberately typed `uv run python -m scripts.build_golden_fixture`.
import:
	cd backend && uv run python -m scripts.import_template ../data/B1_Mine.xlsx $(ARGS)

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

format:
	cd backend && uv run ruff format .
