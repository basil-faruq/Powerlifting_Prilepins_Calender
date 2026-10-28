# syntax=docker/dockerfile:1

# One image for both backend services. The api and the worker differ only in
# their `command` (docker-compose.yml, profile `full`): same code, same
# dependencies, and the worker is the half that advances a block on a timer.

FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.12-slim-bookworm

# UV_PYTHON_DOWNLOADS=never: the base image already ships the 3.12 that
# `requires-python = ">=3.12,<3.13"` asks for, and letting uv fetch its own
# would add ~50 MB to say the same thing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /usr/local/bin/uv

# The user exists before anything is copied, so every COPY can set its owner.
# A `chown -R /app` afterwards would instead rewrite the whole virtualenv --
# ortools and numpy included -- into a second layer, on every source edit.
#
# uid 10001 sits above the host's normal user range, so a file this user writes
# to a bind mount cannot be mistaken for the host user's own (spec 22 §11).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
RUN chown app:app /app
USER app

# Dependencies in their own layer, before the source. `package = false` in
# pyproject.toml means the project itself is never installed, so uv needs only
# these two files -- and editing a route then does not re-resolve 89 packages.
# psycopg is the binary wheel and ortools ships its own libs, so there is no
# apt-get here at all.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache

COPY --chown=app:app . .

EXPOSE 8000

# /app is the working directory for a reason beyond tidiness: alembic.ini says
# `prepend_sys_path = .`, migrations/env.py reads the URL from get_settings(),
# and `python -m domains.strength.workflows.worker` needs this directory on
# sys.path rather than the module's own. All three break from anywhere else.
#
# Exec form, so uvicorn is PID 1 and receives the container's stop signal
# directly instead of through a shell that would swallow it.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
