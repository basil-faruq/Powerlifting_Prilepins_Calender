"""Database engine and sessions.

The schema is created by Alembic migrations only (spec 02). There is no
`create_all()` in application code.
"""

from collections.abc import Generator

from sqlalchemy import text
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

engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session per request."""
    with Session(engine) as session:
        yield session


def ping_db() -> bool:
    """True when the database answers a trivial query. Blocking."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True
