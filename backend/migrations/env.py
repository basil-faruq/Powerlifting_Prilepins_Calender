"""Alembic environment.

This module imports from both `planner_core` and `domains`, which no
application module may do. That is correct here and worth stating plainly:
`env.py` is build tooling, the same category as `scripts/`, not part of the
engine. The invariant is that **`planner_core` must not import `domains`**, and
nothing below inverts it — the arrow still points one way.

It deliberately does **not** import `planner_core.db`. That module builds its
engine as an import side effect from `get_settings().database_url`, so importing
it here would construct a second engine pointed at the wrong database during a
test run against `planner_test`.
"""

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool

# `prepend_sys_path = .` in alembic.ini puts `backend/` on sys.path, so these
# resolve when alembic is invoked from that directory.
from domains.strength import models  # noqa: F401  (imported for its side effect)
from planner_core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Importing `models` registers all 14 tables on this metadata. `--autogenerate`
# and the drift test in tests/test_models.py both compare against it.
target_metadata = models.SQLModel.metadata

# Longer than planner_core.db's 2 s. That bound exists to keep `/health`
# responsive (ADR-0001 rule #4); a migration may legitimately wait longer for a
# database that is still starting up.
CONNECT_TIMEOUT_S = 5


def _database_url() -> str:
    """Resolve the target URL, most explicit source first.

    1. `sqlalchemy.url` set on the config object — how `tests/conftest.py`
       points migrations at `planner_test`. In-process and immune to both the
       `get_settings` cache and env-var ordering.
    2. `-x db_url=...` on the command line, for a one-off manual run.
    3. `get_settings().database_url`, the normal path for `make migrate`.

    `os.environ` is never read here; all configuration flows through
    `get_settings()` (CLAUDE.md conventions).
    """
    from_config = config.get_main_option("sqlalchemy.url", None)
    if from_config:
        return from_config

    from_argument = context.get_x_argument(as_dictionary=True).get("db_url")
    if from_argument:
        return from_argument

    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade head --sql`).

    `literal_binds` is what lets the data migration in 0002 render its values
    inline. It is also why those values are wrapped in an explicit cast there —
    a bare Python list has no literal renderer for JSONB.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run the migrations in a transaction.

    NullPool so `upgrade` leaves no idle connection behind — a pooled one would
    block the `DROP DATABASE` that the test fixture issues on teardown.
    """
    engine = sa.create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
