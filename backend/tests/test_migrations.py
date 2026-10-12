"""Migration tests.

The offline tests render the migrations to SQL without a database, which is
what catches an ordering mistake (the extension after the table that needs it)
or a value that cannot be rendered under `literal_binds`.

`test_the_migrations_match_the_models` is the one that matters most: the
migrations are hand-finished, so a column added to a model and not to
`0001_init` would otherwise go unnoticed until runtime.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from domains.strength import models

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _config() -> Config:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    return config


@pytest.fixture(scope="module")
def rendered_sql() -> str:
    """The whole migration history rendered as SQL, with no database involved."""
    config = _config()
    # A URL is still required to pick a dialect, but offline mode never connects.
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost:5432/unused")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


# --- Revision graph -------------------------------------------------------


def test_the_revision_graph_is_linear_with_a_single_head():
    """One head, one unbroken chain.

    Deliberately not pinned to a revision id: the head moves with every
    migration, so asserting a literal would mean editing this test each time
    without catching anything. What matters is that the history never branches.
    """
    script = ScriptDirectory.from_config(_config())
    assert len(script.get_heads()) == 1

    revisions = list(script.walk_revisions())
    assert revisions[-1].down_revision is None, "the oldest revision must have no parent"
    for revision in revisions[:-1]:
        assert isinstance(revision.down_revision, str), f"{revision.revision} branches or merges"


# --- Rendered SQL ---------------------------------------------------------


def test_the_vector_extension_is_created_before_the_table_that_needs_it(rendered_sql: str):
    extension_at = rendered_sql.index("CREATE EXTENSION IF NOT EXISTS vector")
    note_chunk_at = rendered_sql.index("CREATE TABLE note_chunk")
    assert extension_at < note_chunk_at


def test_every_table_is_created(rendered_sql: str):
    for table in models.SQLModel.metadata.tables:
        assert f"CREATE TABLE {table} " in rendered_sql or (
            f"CREATE TABLE {table}(" in rendered_sql
        ), table


def test_the_hnsw_index_renders_with_cosine_operators(rendered_sql: str):
    assert "USING hnsw (embedding vector_cosine_ops)" in rendered_sql


def test_the_one_rep_max_index_renders_descending(rendered_sql: str):
    assert "set_on DESC" in rendered_sql


def test_the_monday_constraint_renders(rendered_sql: str):
    assert "EXTRACT(ISODOW FROM start_date) = 1" in rendered_sql


def test_the_lift_seed_is_idempotent_and_casts_its_jsonb(rendered_sql: str):
    """A bare list would raise CompileError under literal_binds."""
    assert "ON CONFLICT (code) DO NOTHING" in rendered_sql
    assert 'CAST(\'["close_grip", "wide_grip"]\' AS JSONB)' in rendered_sql


def test_no_column_is_rendered_as_varchar(rendered_sql: str):
    """Spec 02 uses TEXT throughout.

    Alembic's own `alembic_version.version_num` is VARCHAR(32) and is not part
    of the schema under test, so it is excluded.
    """
    without_alembic_table = rendered_sql.replace("version_num VARCHAR(32)", "")
    assert "VARCHAR" not in without_alembic_table


def test_every_model_column_is_created_by_some_migration(rendered_sql: str):
    """Catches a column present in the models but missing from the migrations.

    A column may arrive either in its table's `CREATE TABLE` or, if a later
    revision added it, in an `ALTER TABLE ... ADD COLUMN`. Both are checked:
    looking only at `CREATE TABLE` would have failed the moment 0003 added
    `one_rep_max.is_synthetic`, which is a real column correctly migrated.
    """
    for name, table in models.SQLModel.metadata.tables.items():
        start = rendered_sql.index(f"CREATE TABLE {name} ")
        created = rendered_sql[start : rendered_sql.index(");", start)]
        altered = "\n".join(
            line
            for line in rendered_sql.splitlines()
            if line.startswith(f"ALTER TABLE {name} ADD COLUMN")
        )
        for column in table.c:
            assert column.name in created or column.name in altered, f"{name}.{column.name}"


# --- Model/migration drift (needs a database) -----------------------------


def test_the_migrations_match_the_models(db_engine: sa.Engine):
    """`planner_test` was built by the migrations; diffing it against the models
    must find nothing. This is the real guard on hand-finished migrations.
    """
    with db_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "target_metadata": models.SQLModel.metadata}
        )
        diff = compare_metadata(context, models.SQLModel.metadata)
    assert diff == []


def test_running_the_migrations_a_second_time_is_a_no_op(db_engine: sa.Engine):
    config = _config()
    rendered = db_engine.url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered)
    command.upgrade(config, "head")

    with db_engine.connect() as connection:
        version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    # Compared against the script directory rather than a literal, so adding a
    # revision does not require editing this test.
    assert version == ScriptDirectory.from_config(_config()).get_current_head()
