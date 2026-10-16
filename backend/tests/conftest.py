"""Shared test fixtures.

Most of this suite is pure Python: the `/health` probes are patched and nothing
touches Temporal, Ollama or the network.

The exception is the schema tests, which need a real PostgreSQL with pgvector —
there is no way to check a `vector` column, an HNSW index or a foreign-key
violation without one. Those tests request `db_session`, which builds the
`planner_test` database from the migrations and **skips cleanly when Postgres is
unreachable**, so `make test` still passes on a machine with no Docker running.

The test database is built by `alembic upgrade head`, never by `create_all()`.
That is not only CLAUDE.md non-negotiable #7: `create_all()` would skip
`CREATE EXTENSION vector` (so `note_chunk` could not be created) and skip
`0002_seed_lifts` (so the seeded lifts could not be asserted), and it would test
the models against themselves while leaving the migrations — the thing that
actually ships — unexercised.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from domains.strength.schemas import BlockDraft, ParsedEntry
from planner_core import llm
from planner_core.config import get_settings
from tests import golden

TEST_DB_NAME = "planner_test"
# `CREATE DATABASE` cannot run from inside the database being created, so the
# admin connection goes to the always-present maintenance database.
MAINTENANCE_DB = "postgres"

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent

# The authored spreadsheet. Gitignored (`data/*.xlsx`, a CLAUDE.md guardrail),
# so a fresh clone does not have it.
WORKBOOK = _REPO_ROOT / "data" / "B1_Mine.xlsx"

# Same bound as planner_core.db: libpq waits indefinitely otherwise, and a test
# run must not hang because Postgres is absent.
CONNECT_TIMEOUT_S = 2

# Probed once per session rather than per test, so an absent Postgres costs one
# 2 s connect instead of one per DB test.
_postgres_reachable: bool | None = None


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings` is cached, so monkeypatched env vars need the cache dropped.

    The model cache goes with it. `get_model` caches per role, so a model built
    under one test's `LLM_PROVIDER` would otherwise be handed to the next test
    that asks -- an order-dependent failure, and the kind that is read as
    flakiness rather than as a missing `reset_models()` in some other file.
    """
    get_settings.cache_clear()
    llm.reset_models()
    yield
    get_settings.cache_clear()
    llm.reset_models()


@pytest.fixture(autouse=True, scope="session")
def _forbid_real_model_requests():
    """No test may call a real model (CLAUDE.md).

    Pydantic AI has a global switch for exactly this, and it is worth more than
    a convention: without it, a test that forgets to override the model would
    quietly call the developer's local Ollama, pass on their machine and fail in
    CI -- or worse, pass in both while costing money in `anthropic` mode. With
    it, that test raises instead.
    """
    import pydantic_ai.models

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def client() -> TestClient:
    from api.main import app

    return TestClient(app)


@pytest.fixture
def api_client(db_session: Session) -> Generator[TestClient, None, None]:
    """A client whose routes run against `db_session`, and roll back with it.

    The dependency override is what ADR-0002 modification 16 deferred to spec
    06: `api.main` builds its engine from `DATABASE_URL` at import, so without
    this a route test would write to the developer's real `planner` database.

    `raise_server_exceptions=False` so a test can assert on the status an
    exception *handler* produced rather than having the exception re-raised
    into the test -- which is the only way to check spec 06 §13's codes.
    """
    from api.main import app
    from planner_core.db import get_session

    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


# --- The golden block -----------------------------------------------------
# Shared by the generator suite (which proves the 77 weights) and the validator
# suite (which proves the author's own plan is accepted, CLAUDE.md #5). Built
# from the committed fixture, so neither skips without the workbook.


@pytest.fixture(scope="session")
def golden_rows() -> list[dict]:
    return golden.load_rows()


@pytest.fixture(scope="session")
def golden_entries(golden_rows) -> tuple[ParsedEntry, ...]:
    return golden.entries_from(golden_rows)


@pytest.fixture(scope="session")
def golden_maxes(golden_rows) -> dict[str, Decimal]:
    return golden.maxes_from(golden_rows)


@pytest.fixture(scope="session")
def golden_draft(golden_entries, golden_maxes) -> BlockDraft:
    return golden.draft_from(golden_entries, golden_maxes)


@pytest.fixture(scope="session")
def workbook_path() -> Path:
    """The authored workbook, or a clean skip.

    `data/*.xlsx` is gitignored, so the file is absent on a fresh clone. By the
    same reasoning that keeps `make test` green with no database, it must stay
    green with no workbook -- the alternative is a suite that cannot run until
    someone is handed a private file.

    The committed golden fixture is read without a skip, because that file *is*
    version-controlled: its absence is a failure, not an environment difference.

    (Note `00-Spec_Index.md` §4 still calls the workbook "committed". That line
    is stale; `.gitignore` and CLAUDE.md agree it is not.)
    """
    if not WORKBOOK.exists():
        pytest.skip(f"{WORKBOOK} is not present (gitignored; see CONTEXT.md §4)")
    return WORKBOOK


# --- Database fixtures ----------------------------------------------------


def _urls() -> tuple[URL, URL]:
    """(admin URL, test URL), derived from the configured DATABASE_URL."""
    base = make_url(get_settings().database_url)
    return base.set(database=MAINTENANCE_DB), base.set(database=TEST_DB_NAME)


def _reachable(admin_url: URL) -> bool:
    global _postgres_reachable
    if _postgres_reachable is None:
        engine = sa.create_engine(
            admin_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
        )
        try:
            with engine.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
            _postgres_reachable = True
        except OperationalError:
            _postgres_reachable = False
        finally:
            engine.dispose()
    return _postgres_reachable


def _upgrade_head(url: URL) -> None:
    """Run every migration against `url`, in-process."""
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    # Absolute, so the run does not depend on pytest's working directory.
    config.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    # `set_main_option` goes through ConfigParser interpolation, so a literal
    # `%` in a password would raise InterpolationSyntaxError. This is the
    # override `migrations/env.py` reads first.
    rendered = url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def db_engine() -> Generator[sa.Engine, None, None]:
    """A `planner_test` database built from the migrations, dropped afterwards."""
    admin_url, test_url = _urls()
    if not _reachable(admin_url):
        pytest.skip(
            f"Postgres unreachable at {admin_url.host}:{admin_url.port} — "
            "start it with `make infra`"
        )

    # AUTOCOMMIT because CREATE/DROP DATABASE cannot run inside a transaction
    # block, and SQLAlchemy 2.x opens one by default.
    admin = sa.create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )

    # The database name is interpolated rather than bound: PostgreSQL does not
    # accept bind parameters in CREATE/DROP DATABASE. It is a module constant
    # quoted as an identifier, never runtime input.
    #
    # WITH (FORCE) terminates leftover connections, so a previously crashed run
    # cannot wedge this with "database is being accessed by other users".
    with admin.connect() as connection:
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        connection.execute(sa.text(f'CREATE DATABASE "{TEST_DB_NAME}"'))

    _upgrade_head(test_url)

    engine = sa.create_engine(
        test_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def db_session(db_engine: sa.Engine) -> Generator[Session, None, None]:
    """A session inside a transaction that is rolled back when the test ends.

    Tests therefore see the migrated schema and the seeded lifts, but never each
    other's writes.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    try:
        # `create_savepoint` so a `session.commit()` *inside* the code under
        # test releases a savepoint instead of committing this outer
        # transaction. Spec 06's routes commit (the services only flush), and
        # without this the first API test would leave its athlete behind for
        # every later test to trip over.
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
    finally:
        # A test that provoked an IntegrityError has already had its
        # transaction rolled back and deassociated, so rolling back again would
        # emit "transaction already deassociated from connection".
        if transaction.is_active:
            transaction.rollback()
        connection.close()
