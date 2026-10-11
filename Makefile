# Run every target from the repository root.
#
# Ollama is not a target: it runs natively on the host so it can use the GPU.
# Pull its models once, before anything that touches the agent:
#   ollama pull qwen2.5:14b
#   ollama pull nomic-embed-text

.PHONY: help infra infra-down api test lint format

help:
	@echo "make infra       Postgres (pgvector) + Temporal dev server in Docker"
	@echo "make infra-down  Stop them"
	@echo "make api         FastAPI with hot reload on http://localhost:8000"
	@echo "make test        pytest"
	@echo "make lint        ruff check + ruff format --check"
	@echo "make format      ruff format"

infra:
	docker compose up -d postgres temporal

infra-down:
	docker compose down

api:
	cd backend && uv run uvicorn api.main:app --reload

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

format:
	cd backend && uv run ruff format .
