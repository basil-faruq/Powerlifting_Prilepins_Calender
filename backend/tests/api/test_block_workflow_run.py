"""Eleven weeks of a block, in a few seconds.

`WorkflowEnvironment.start_time_skipping()` runs a Temporal test server whose
clock jumps forward on demand. That is what makes it possible to assert on a
whole block: the workflow genuinely sleeps between sessions, and the sleeping
costs nothing.

Everything else is real -- a real worker, real activities, real Postgres, the
real `approve_proposal`. The only fiction is the clock.

**Two rules for anyone adding a test here.**

*Wait for the workflow, do not assume it.* A query is answered from whatever
the workflow has reached, and starting one does not mean it has finished its
first activity. `_until` polls; `_state` alone is only safe once something
has already settled.

*Touch the database only through the helpers below.* The activity executor
is a separate thread sharing this test's single Postgres connection, and a
connection serves one command at a time -- read it from both sides at once
and psycopg says "another command is already in progress", which looks like
anything but the race it is. `_DB_LOCK` makes the two take turns, and every
helper here holds it. A bare `db_session` call in a test body does not, and
will fail intermittently.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlmodel import Session
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from domains.strength import models, services
from domains.strength.workflows import contracts
from domains.strength.workflows.activities import StrengthActivities
from domains.strength.workflows.block_workflow import BlockWorkflow
from tests.api.conftest import propose
from tests.fakes import FakeEmbedder

pytestmark = pytest.mark.asyncio

TASK_QUEUE = "test-strength-planner"

# The workflow runs on its own thread and reaches the same connection this
# test does. Reentrant because a helper may call another.
_DB_LOCK = threading.RLock()

# How long to let the workflow work before calling it stuck. Activities run
# against a local database, so anything near this is a hang, not slowness.
SETTLE_TRIES = 300
SETTLE_PAUSE = 0.05


@pytest_asyncio.fixture(loop_scope="function")
async def env():
    """A Temporal test server, or a clean skip.

    `tests/conftest.py`'s standing promise is that `make test` is green with
    no Postgres, no workbook and no network. This fixture downloads and runs
    a server binary on first use, so it keeps that promise the same way the
    database fixtures do.
    """
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - any failure here means "not available"
        pytest.skip(f"Temporal test server unavailable: {exc}")
    try:
        yield environment
    finally:
        await environment.shutdown()


@pytest_asyncio.fixture(loop_scope="function")
async def worker(env, db_session: Session):
    """A real worker, with activities bound to this test's transaction.

    `max_workers=1` is load-bearing: a SQLAlchemy `Connection` is not
    thread-safe, and this test's own thread holds the same one.

    The workflow runner is unsandboxed here and only here. The sandbox exists
    to catch non-deterministic imports, and `tests/test_block_workflow.py`
    already does that statically against the source -- whereas running it
    here would re-import the module tree per workflow task and make these
    tests minutes long for no extra signal.
    """

    @contextmanager
    def factory():
        with (
            _DB_LOCK,
            Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ) as session,
        ):
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    client = Client(**{**env.client.config(), "data_converter": pydantic_data_converter})
    activities = StrengthActivities(factory, embedder_factory=FakeEmbedder)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-activity") as pool:
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[BlockWorkflow],
            activities=activities.all(),
            activity_executor=pool,
            workflow_runner=UnsandboxedWorkflowRunner(),
            max_concurrent_activities=1,
            graceful_shutdown_timeout=dt.timedelta(seconds=20),
        ):
            yield client


@pytest.fixture
def pending_block(api_client, db_session: Session, ready) -> tuple[uuid.UUID, uuid.UUID]:
    """A `new_block` proposal, not yet approved -- where a block really starts."""
    return propose(api_client, ready["athlete"], ready["template"])


# --- Talking to the workflow ---------------------------------------------


async def _state(handle) -> contracts.WorkflowStateView:
    """Query the workflow, typed.

    A query named by string has no return annotation to read from, so the
    data converter hands back a plain dict unless told what to build.
    """
    return await handle.query(contracts.CURRENT_STATE, result_type=contracts.WorkflowStateView)


async def _decide(handle, name: str, *args) -> contracts.CommitOutcome:
    return await handle.execute_update(name, args=list(args), result_type=contracts.CommitOutcome)


async def _until(handle, predicate, what: str = "the expected state"):
    """Poll until the workflow has got where the test is waiting for.

    Real sleeps, not `env.sleep`: advancing the clock here would trip
    deadlines the test has not asked to trip. Auto time-skipping only runs
    while a client waits on a workflow *result*, so polling is safe.
    """
    state = await _state(handle)
    for _ in range(SETTLE_TRIES):
        if predicate(state):
            return state
        await asyncio.sleep(SETTLE_PAUSE)
        state = await _state(handle)
    raise AssertionError(f"workflow never reached {what}; last state was {state}")


async def _started(handle) -> contracts.WorkflowStateView:
    """The block is approved, its sessions exist and one is next."""
    return await _until(handle, lambda s: s.next_session is not None, "a loaded plan")


async def _awaiting(handle, state_name: str) -> contracts.WorkflowStateView:
    return await _until(handle, lambda s: s.state == state_name, state_name)


async def _start(client: Client, block_id: uuid.UUID, proposal_id: uuid.UUID | None = None):
    return await client.start_workflow(
        BlockWorkflow.run,
        args=[block_id, proposal_id],
        id=contracts.workflow_id_for(block_id),
        task_queue=TASK_QUEUE,
    )


def _sessions(db_session: Session, block_id: uuid.UUID) -> list[models.PlannedSession]:
    with _DB_LOCK:
        return services.sessions_of(db_session, block_id)


def _block(db_session: Session, block_id: uuid.UUID) -> models.Block:
    """The block as the database now has it, not as the identity map cached it."""
    with _DB_LOCK:
        block = services.get_block(db_session, block_id)
        db_session.refresh(block)
        return block


def _proposal(db_session: Session, proposal_id: uuid.UUID) -> models.Proposal:
    with _DB_LOCK:
        proposal = services.get_proposal(db_session, proposal_id)
        db_session.refresh(proposal)
        return proposal


def _log(api_client, db_session: Session, row: models.PlannedSession) -> None:
    with _DB_LOCK:
        lifts = services.lifts_of(db_session, row.id)
    response = _post(
        api_client,
        f"/sessions/{row.id}/log",
        {
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
        },
    )
    assert response.status_code == 201, response.text


def _post(api_client, url: str, body: dict):
    """An API call holds the same connection the activities use."""
    with _DB_LOCK:
        return api_client.post(url, json=body)


async def _log_whole_block(handle, api_client, db_session, block_id) -> None:
    """Train every session as prescribed, then tell the workflow to look."""
    for row in _sessions(db_session, block_id):
        _log(api_client, db_session, row)
    await handle.signal(contracts.SESSION_LOGGED, uuid.uuid4())


# --- Starting -------------------------------------------------------------


async def test_approving_the_first_proposal_starts_the_block(
    worker, api_client, db_session, pending_block
):
    """The workflow commits the `new_block` proposal it was started with, so
    even the very first schedule goes through the one write path."""
    block_id, proposal_id = pending_block
    assert _sessions(db_session, block_id) == []

    handle = await _start(worker, block_id, proposal_id)
    state = await _started(handle)

    assert state.state == "ACTIVE"
    assert len(_sessions(db_session, block_id)) == 33
    assert _block(db_session, block_id).status == "active"


async def test_the_block_records_which_execution_owns_it(
    worker, api_client, db_session, pending_block
):
    """ADR-0007 left `block.workflow_id` unwritten and asked spec 15 to decide."""
    block_id, proposal_id = pending_block

    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    assert _block(db_session, block_id).workflow_id == contracts.workflow_id_for(block_id)


# --- A session that goes by -----------------------------------------------


async def test_an_unlogged_session_puts_repair_options_on_the_table(
    env, worker, api_client, db_session, pending_block
):
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    # Past the first session's day and its grace period.
    await env.sleep(dt.timedelta(days=2, hours=contracts.GRACE_HOURS + 1))
    state = await _awaiting(handle, "AWAITING_RESCHEDULE_APPROVAL")

    assert state.pending_proposal_ids != ()
    assert state.tier is not None


async def test_the_workflow_never_writes_a_missed_status(
    env, worker, api_client, db_session, pending_block
):
    """ADR-0010's rule, and the one whose breach stays invisible for a while.

    `missed` means written off: the calendar solver pins it as history and
    `approve_proposal` refuses to re-date it. A session the athlete may still
    make up has to stay `planned` with a date in the past, which is exactly
    what lets a reschedule move it.
    """
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    await env.sleep(dt.timedelta(days=2, hours=contracts.GRACE_HOURS + 1))
    await _awaiting(handle, "AWAITING_RESCHEDULE_APPROVAL")

    rows = _sessions(db_session, block_id)
    assert [r.seq for r in rows if r.status == "missed"] == []
    assert rows[0].status == "planned"


async def test_approving_a_repair_moves_the_dates_and_resumes(
    env, worker, api_client, db_session, pending_block
):
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)
    before = {r.seq: r.date for r in _sessions(db_session, block_id)}

    await env.sleep(dt.timedelta(days=2, hours=contracts.GRACE_HOURS + 1))
    state = await _awaiting(handle, "AWAITING_RESCHEDULE_APPROVAL")
    chosen = state.pending_proposal_ids[0]

    outcome = await _decide(handle, contracts.APPROVE, chosen)

    assert outcome.status == "approved"
    assert _proposal(db_session, chosen).status == "approved"
    after = {r.seq: r.date for r in _sessions(db_session, block_id)}
    assert after != before


async def test_rejecting_every_repair_does_not_ask_again_immediately(
    env, worker, api_client, db_session, pending_block
):
    """The loop the spec does not mention.

    A rejected session is still `planned` with a date in the past, so without
    a cooldown the next pass proposes again at once -- and again, filling the
    workflow history and the decision log with the same question.
    """
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    await env.sleep(dt.timedelta(days=2, hours=contracts.GRACE_HOURS + 1))
    state = await _awaiting(handle, "AWAITING_RESCHEDULE_APPROVAL")
    for pid in state.pending_proposal_ids:
        await _decide(handle, contracts.REJECT, pid, "not this week")

    await _awaiting(handle, "ACTIVE")
    asked_once = _decision_count(db_session, handle.result_run_id)
    assert asked_once >= 1

    await env.sleep(dt.timedelta(hours=contracts.MISSED_RETRY_COOLDOWN_HOURS - 2))
    await asyncio.sleep(SETTLE_PAUSE * 10)

    assert _decision_count(db_session, handle.result_run_id) == asked_once


def _decision_count(db_session: Session, run_id: str) -> int:
    with _DB_LOCK:
        return _count_decisions(db_session, run_id)


def _count_decisions(db_session: Session, run_id: str) -> int:
    return (
        db_session.scalar(
            sa.select(sa.func.count())
            .select_from(models.Decision)
            .where(models.Decision.run_id == run_id)
        )
        or 0
    )


# --- The signals the spec names -------------------------------------------


async def test_reporting_two_weeks_away_does_not_produce_an_illness_tier(
    env, worker, api_client, db_session, pending_block
):
    """Spec 15 §12 expects "illness tier" proposals. There is no illness tier.

    ADR-0010 modification 3 removed the four day-counted tiers and replaced
    them with four structural outcomes, so a fortnight away comes out as
    whichever of those the calendar allows -- not as a tier named after how
    long the athlete was ill.
    """
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    state = await _started(handle)
    first = state.next_session
    assert first is not None

    await handle.signal(
        contracts.REPORT_UNAVAILABLE,
        args=[first.date, first.date + dt.timedelta(days=13), False],
    )
    state = await _awaiting(handle, "AWAITING_RESCHEDULE_APPROVAL")

    assert state.tier in {"in_week_move", "week_shift", "fold", "restart"}


async def test_an_injury_proposes_a_restart_however_short(
    env, worker, api_client, db_session, pending_block
):
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    state = await _started(handle)
    first = state.next_session
    assert first is not None

    await handle.signal(
        contracts.REPORT_UNAVAILABLE,
        args=[first.date, first.date + dt.timedelta(days=3), True],
    )
    state = await _awaiting(handle, "AWAITING_RESCHEDULE_APPROVAL")

    assert state.tier == "restart"


async def test_pausing_and_resuming_moves_the_block_and_comes_back(
    env, worker, api_client, db_session, pending_block
):
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    await handle.signal(contracts.PAUSE, "travelling")
    await _awaiting(handle, "PAUSED")
    assert _block(db_session, block_id).status == "paused"

    await handle.signal(contracts.RESUME, None)
    await _until(handle, lambda s: s.state != "PAUSED", "anything but PAUSED")

    assert _block(db_session, block_id).status == "active"


async def test_an_unknown_proposal_is_ignored_and_the_block_lives(
    env, worker, api_client, db_session, pending_block
):
    """Spec §13. A stray id must not strand eleven weeks of training."""
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    outcome = await _decide(handle, contracts.APPROVE, uuid.uuid4())

    assert outcome.status == "ignored"
    state = await _awaiting(handle, "ACTIVE")
    assert state.next_session is not None


# --- Rolling over ---------------------------------------------------------


async def test_a_fully_logged_block_finishes_and_offers_the_next(
    env, worker, api_client, db_session, pending_block
):
    """The headline: eleven weeks, start to finish."""
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    await _log_whole_block(handle, api_client, db_session, block_id)
    state = await _awaiting(handle, "AWAITING_ROLLOVER_APPROVAL")

    assert len(state.pending_proposal_ids) == 1
    assert _block(db_session, block_id).status == "done"

    rollover = _proposal(db_session, state.pending_proposal_ids[0])
    assert rollover.kind == "new_block"
    assert rollover.source == "workflow"
    assert rollover.status == "pending"


async def test_approving_the_next_block_continues_as_new(
    env, worker, api_client, db_session, pending_block
):
    """`continue_as_new` keeps the history short across blocks (spec §11) and
    keeps the workflow id -- which is why `block.workflow_id` holds the
    chain's id rather than each block's own."""
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    await _log_whole_block(handle, api_client, db_session, block_id)
    state = await _awaiting(handle, "AWAITING_ROLLOVER_APPROVAL")

    await _decide(handle, contracts.APPROVE, state.pending_proposal_ids[0])
    after = await _until(handle, lambda s: s.block_id != block_id, "the next block")

    nxt = _block(db_session, after.block_id)
    assert nxt.status == "active"
    assert nxt.workflow_id == contracts.workflow_id_for(block_id)
    assert nxt.number > _block(db_session, block_id).number


async def test_declining_the_next_block_ends_the_chain(
    env, worker, api_client, db_session, pending_block
):
    block_id, proposal_id = pending_block
    handle = await _start(worker, block_id, proposal_id)
    await _started(handle)

    await _log_whole_block(handle, api_client, db_session, block_id)
    state = await _awaiting(handle, "AWAITING_ROLLOVER_APPROVAL")

    await _decide(handle, contracts.REJECT, state.pending_proposal_ids[0], "taking a break")
    # Typed from `BlockWorkflow.run`'s own annotation, unlike the
    # string-named queries above.
    final = await handle.result()

    assert final.state == "DONE"
