"""`unit_of_work` is what makes one activity one safe unit of retry.

The services flush and never commit (ADR-0007 rule 2) -- that is what lets the
same `approve_proposal` serve an HTTP request and a Temporal activity without
knowing which it is in. The route commits; for an activity, this does.

Which means the guarantee below is load-bearing in a way a session factory
usually is not. Temporal retries a failed activity, so a half-written
transaction left behind by a failure would be re-entered on top of itself:
`approve_proposal` would find sessions it had already half-persisted and
either duplicate them or refuse. All or nothing is the whole point.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from planner_core.db import unit_of_work


def _count(session: Session, code: str) -> int:
    return session.scalar(sa.text("SELECT count(*) FROM lift WHERE code = :code"), {"code": code})


def test_a_clean_exit_commits(db_session: Session) -> None:
    code = f"probe_{uuid.uuid4().hex[:8]}"

    with unit_of_work(bind=db_session.connection()) as work:
        work.execute(
            sa.text("INSERT INTO lift (code, name) VALUES (:code, :name)"),
            {"code": code, "name": "probe"},
        )

    # Visible from the outer session: the inner `commit` released its
    # savepoint, so the write is real as far as anything after it is
    # concerned -- and still inside the test's own transaction, which the
    # fixture rolls back.
    assert _count(db_session, code) == 1


def test_an_exception_rolls_the_whole_activity_back(db_session: Session) -> None:
    """The retry safety property, stated directly.

    A second attempt must start from the state the first one found, not from
    whatever it got halfway through writing.
    """
    code = f"probe_{uuid.uuid4().hex[:8]}"

    with (
        pytest.raises(RuntimeError, match="activity failed"),
        unit_of_work(bind=db_session.connection()) as work,
    ):
        work.execute(
            sa.text("INSERT INTO lift (code, name) VALUES (:code, :name)"),
            {"code": code, "name": "probe"},
        )
        work.flush()
        raise RuntimeError("activity failed")

    assert _count(db_session, code) == 0


def test_the_exception_is_re_raised_rather_than_swallowed(db_session: Session) -> None:
    """Temporal decides what to retry by watching the exception leave.

    A context manager that rolled back and returned would turn every activity
    failure into a silent success.
    """

    class Boom(Exception):
        pass

    with pytest.raises(Boom), unit_of_work(bind=db_session.connection()):
        raise Boom
