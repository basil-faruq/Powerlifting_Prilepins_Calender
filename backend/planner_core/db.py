"""Database engine and sessions.

The schema is created by Alembic migrations only (spec 02). There is no
`create_all()` in application code.
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, create_engine

from planner_core.config import get_settings

# `create_engine` does not connect, so importing this module works with Postgres
# down — which is what lets `/health` report "db": "down" instead of crashing.
#
# `connect_timeout` is not optional: libpq waits indefinitely by default, so a
# connect to a host that is up but not answering blocks the calling thread for
# minutes. 2 s is libpq's effective minimum, and it is applied per resolved
# address, so a `localhost` that resolves to both ::1 and 127.0.0.1 can take
# twice that before giving up.
CONNECT_TIMEOUT_S = 2


def make_engine(url: str) -> Engine:
    """An engine for `url`, configured the way this project needs one.

    Factored out of the module-level `engine` for the test suite (ADR-0002
    modification 16): route tests need an engine pointed at `planner_test`, and
    the alternative is duplicating the timeout and pre-ping settings in
    `conftest.py` where they would quietly drift.
    """
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


engine = make_engine(get_settings().database_url)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session per request."""
    with Session(engine) as session:
        yield session


@contextmanager
def unit_of_work(bind: Engine | None = None) -> Iterator[Session]:
    """One transaction, for a caller that is not a request.

    `get_session` above is a FastAPI dependency: it yields and the *route*
    commits. A Temporal activity has no route, so this is its twin -- it
    commits on a clean exit and rolls back on an exception, making one activity
    one transaction and therefore one safe unit of retry.

    The services themselves are unchanged by this: they flush and never commit
    (ADR-0007 rule 2), which is exactly what lets the same function serve a
    request and an activity without knowing which it is in.

    `bind` exists for the test suite, which runs activities against a session
    on its own already-open connection so the outer rollback still owns
    everything written.
    """
    with Session(bind or engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def ping_db() -> bool:
    """True when the database answers a trivial query. Blocking."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True
