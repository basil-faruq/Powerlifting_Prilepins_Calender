"""Finishing a block, and starting the next one from what it produced.

The sharp edge here is the 1RM, and it is sharp because two pieces of code
decide the same thing in different places. `log_session` banks a made test
single the moment it is logged; `generator.next_block_one_rms` works out,
afterwards, what every lift's max became. They agree by construction -- same
rule, same inputs -- which means `finish_block` must write only what the
first one did *not*, or the athlete ends up with two rows for one single.

ADR-0005's Action Required says precisely that. These tests are what stops it
becoming a comment nobody checks.

The rest is idempotence. Temporal retries activities, `one_rep_max` has no
unique constraint on `(athlete_id, lift_code, set_on)` -- deliberately, since
a corrected max on the same day is legitimate -- so nothing may rely on the
database refusing a duplicate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import generator, models, services
from domains.strength.workflows import contracts
from domains.strength.workflows.activities import StrengthActivities, _draft_of
from tests.api.conftest import propose

NOON_UTC = dt.time(12, 0)


@pytest.fixture
def acts(db_session: Session) -> StrengthActivities:
    @contextmanager
    def factory():
        with Session(
            bind=db_session.connection(), join_transaction_mode="create_savepoint"
        ) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return StrengthActivities(factory)


@pytest.fixture
def active(api_client, db_session: Session, ready) -> models.Block:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200
    block = services.get_block(db_session, block_id)
    db_session.refresh(block)
    return block


def _as_of(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, NOON_UTC, tzinfo=dt.UTC)


def _log_everything(api_client, db_session: Session, block: models.Block) -> dt.date:
    """Train the whole block exactly as prescribed, and return the last date.

    Every weight is read back from the plan rather than written here, so this
    helper cannot quietly disagree with the generator about what was due --
    and so no 1RM in this file is a number a test made up.
    """
    last = block.start_date
    for row in services.sessions_of(db_session, block.id):
        lifts = services.lifts_of(db_session, row.id)
        body = {
            "performed_on": row.date.isoformat(),
            "entries": [
                {
                    "lift_code": lift.lift_code,
                    "sets_done": lift.sets,
                    "reps_done": lift.reps,
                    "weight_kg": str(lift.weight_kg),
                }
                for lift in lifts
            ],
        }
        response = api_client.post(f"/sessions/{row.id}/log", json=body)
        assert response.status_code == 201, response.text
        last = max(last, row.date)
    return last


def _maxes(db_session: Session, athlete_id: uuid.UUID) -> list[models.OneRepMax]:
    return list(
        db_session.scalars(
            sa.select(models.OneRepMax)
            .where(models.OneRepMax.athlete_id == athlete_id)
            .order_by(models.OneRepMax.lift_code, models.OneRepMax.set_on)
        )
    )


# --- The two predicates that must not drift ------------------------------


def test_the_log_and_the_generator_agree_on_what_counts_as_a_made_test(
    api_client, db_session, active
):
    """The assumption `finish_block` rests on, tested directly.

    If these two ever disagree -- one counting a single the other does not --
    then either a max is written twice or a real one is dropped. Neither is
    visible until an athlete looks at their history months later.
    """
    _log_everything(api_client, db_session, active)
    state = services.load_block_state(db_session, active.id)
    news = generator.next_block_one_rms(
        _draft_of(state),
        state.logs,
    )

    tested_by_generator = {lift for lift, new in news.items() if new.source == "tested"}
    tested_by_log = {
        row.lift_code for row in _maxes(db_session, active.athlete_id) if row.source == "tested"
    }

    # Every lift the generator calls tested was banked at log time, and the
    # test singles are the only `tested` rows this block produced.
    assert tested_by_generator <= tested_by_log


# --- finish_block ---------------------------------------------------------


def test_finishing_writes_no_second_row_for_a_test_already_banked(
    acts, api_client, db_session, active
):
    """ADR-0005's Action Required, as an assertion.

    `log_session` wrote the tested maxes. `finish_block` must add nothing for
    them -- one single, one row.
    """
    last = _log_everything(api_client, db_session, active)
    before = _maxes(db_session, active.athlete_id)
    tested_before = [row for row in before if row.source == "tested"]

    out = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    after = _maxes(db_session, active.athlete_id)
    tested_after = [row for row in after if row.source == "tested"]
    assert len(tested_after) == len(tested_before)
    assert "squat" not in out.written


def test_finishing_marks_the_block_done(acts, api_client, db_session, active):
    """`BlockStatus` has carried `done` unused since spec 02. This sets it."""
    last = _log_everything(api_client, db_session, active)

    acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    db_session.refresh(active)
    assert active.status == "done"


def test_the_maxes_it_reports_are_the_ones_the_generator_computed(
    acts, api_client, db_session, active
):
    last = _log_everything(api_client, db_session, active)
    state = services.load_block_state(db_session, active.id)
    expected = {
        lift: new.kg
        for lift, new in generator.next_block_one_rms(_draft_of(state), state.logs).items()
    }

    out = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    assert out.new_one_rms == expected
    assert all(isinstance(kg, Decimal) for kg in out.new_one_rms.values())


def test_an_unchanged_max_is_not_written_at_all(acts, api_client, db_session, active):
    """A missed or failed test keeps the old number, and a carried-over value
    was not measured on any new day -- writing it would invent a measurement
    (CLAUDE.md non-negotiable #9)."""
    last = _log_everything(api_client, db_session, active)
    state = services.load_block_state(db_session, active.id)
    news = generator.next_block_one_rms(_draft_of(state), state.logs)
    unchanged = {lift for lift, new in news.items() if new.source is None}

    out = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    assert unchanged.isdisjoint(out.written)


def test_finishing_twice_writes_nothing_the_second_time(acts, api_client, db_session, active):
    """The retry case. Without this, a redelivered activity doubles every
    estimated max -- and nothing in the schema would stop it."""
    last = _log_everything(api_client, db_session, active)
    ref = contracts.BlockRef(block_id=active.id, as_of=_as_of(last))

    first = acts.finish_block(ref)
    rows_after_first = len(_maxes(db_session, active.athlete_id))
    second = acts.finish_block(ref)

    assert first.already_done is False
    assert second.already_done is True
    assert second.written == ()
    assert len(_maxes(db_session, active.athlete_id)) == rows_after_first
    # The numbers are still reported, because the caller needs them even on
    # a retry -- they are recomputed from a pure function.
    assert second.new_one_rms == first.new_one_rms


def test_the_clock_comes_from_the_caller_not_from_the_machine(acts, api_client, db_session, active):
    """`record_one_rep_maxes` refuses a `set_on` after today, and in a
    time-skipping test the real clock would call every logged date a future
    one and refuse the whole block."""
    last = _log_everything(api_client, db_session, active)

    out = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    assert out.block_id == active.id


def test_the_rows_it_writes_are_attributed_to_the_block(acts, api_client, db_session, active):
    last = _log_everything(api_client, db_session, active)

    out = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    for lift in out.written:
        row = db_session.scalars(
            sa.select(models.OneRepMax)
            .where(
                models.OneRepMax.athlete_id == active.athlete_id,
                models.OneRepMax.lift_code == lift,
                models.OneRepMax.source == "estimated",
            )
            .order_by(models.OneRepMax.created_at.desc())
        ).first()
        assert row is not None
        assert row.block_id == active.id
        assert row.is_synthetic is False


# --- create_next_block ----------------------------------------------------


def test_the_next_block_starts_from_the_maxes_the_last_one_produced(
    acts, api_client, db_session, active
):
    last = _log_everything(api_client, db_session, active)
    finished = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    out = acts.create_next_block(
        contracts.NextBlockRequest(
            block_id=active.id, as_of=_as_of(last), one_rms=finished.new_one_rms
        )
    )

    nxt = services.get_block(db_session, out.next_block_id)
    assert nxt.status == "draft"
    assert {k: Decimal(str(v)) for k, v in nxt.one_rm_snapshot.items()} == finished.new_one_rms


def test_the_next_block_proposal_is_pending_and_the_workflows(acts, api_client, db_session, active):
    """The first proposal in the codebase with `source = "workflow"`."""
    last = _log_everything(api_client, db_session, active)
    finished = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    out = acts.create_next_block(
        contracts.NextBlockRequest(
            block_id=active.id, as_of=_as_of(last), one_rms=finished.new_one_rms
        )
    )

    proposal = services.get_proposal(db_session, out.proposal_id)
    assert proposal.status == "pending"
    assert proposal.source == "workflow"
    assert proposal.kind == "new_block"


def test_the_payload_records_the_maxes_it_was_built_from(acts, api_client, db_session, active):
    """`StrengthPayload.new_one_rms` was defined in spec 05 and produced by
    nothing (ADR-0007 "Deliberately not done 6"). This is its producer.

    It is provenance for the trace view, not an instruction: the rows were
    already written by `finish_block` with the dates the athlete lifted on,
    and `approve_proposal` still ignores this field.
    """
    last = _log_everything(api_client, db_session, active)
    finished = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    out = acts.create_next_block(
        contracts.NextBlockRequest(
            block_id=active.id, as_of=_as_of(last), one_rms=finished.new_one_rms
        )
    )

    payload = services.read_payload(services.get_proposal(db_session, out.proposal_id))
    assert payload.new_one_rms is not None
    assert {k: Decimal(str(v)) for k, v in payload.new_one_rms.items()} == finished.new_one_rms


def test_the_next_block_starts_on_a_monday_after_the_last_session(
    acts, api_client, db_session, active
):
    """Derived from the plan, not from a clock -- which is what makes this
    sensible in a time-skipped run as well as a real one."""
    last = _log_everything(api_client, db_session, active)
    finished = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    out = acts.create_next_block(
        contracts.NextBlockRequest(
            block_id=active.id, as_of=_as_of(last), one_rms=finished.new_one_rms
        )
    )

    nxt = services.get_block(db_session, out.next_block_id)
    assert nxt.start_date.weekday() == 0
    assert nxt.start_date > last


def test_the_next_block_keeps_the_same_lifts_and_weekdays(acts, api_client, db_session, active):
    last = _log_everything(api_client, db_session, active)
    finished = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    out = acts.create_next_block(
        contracts.NextBlockRequest(
            block_id=active.id, as_of=_as_of(last), one_rms=finished.new_one_rms
        )
    )

    nxt = services.get_block(db_session, out.next_block_id)
    assert set(nxt.lifts) == set(active.lifts)
    payload = services.read_payload(services.get_proposal(db_session, out.proposal_id))
    assert {s.date.weekday() for s in payload.plan} == {0, 2, 4}


def test_the_next_block_is_not_scheduled_until_someone_approves_it(
    acts, api_client, db_session, active
):
    """The approval gate still holds across a rollover (CLAUDE.md #2)."""
    last = _log_everything(api_client, db_session, active)
    finished = acts.finish_block(contracts.BlockRef(block_id=active.id, as_of=_as_of(last)))

    out = acts.create_next_block(
        contracts.NextBlockRequest(
            block_id=active.id, as_of=_as_of(last), one_rms=finished.new_one_rms
        )
    )

    assert services.sessions_of(db_session, out.next_block_id) == []
