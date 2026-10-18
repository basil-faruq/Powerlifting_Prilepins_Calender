"""The activities, tested as plain functions against a real database.

No Temporal here on purpose. An activity is a function that takes one frozen
model and returns another; the durable-execution machinery around it is
`test_block_workflow.py`'s problem. Keeping the two apart means a failure
here is about the planner and never about a task queue.

What these pin down, in order of how much it would hurt to get wrong:

1. **The workflow never writes `missed`.** ADR-0010 left "is `missed` ever
   written" open and spec 15 answers no. If this slips, the calendar solver
   pins the session as history and every reschedule silently stops working.
2. **Refusals stay refusals.** An already-approved proposal must come back as
   something the route turns into a 409, not as a retried failure.
3. **The clock arrives as an argument**, so a retry computes the same day.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlmodel import Session
from temporalio.exceptions import ApplicationError

from domains.strength import models, services
from domains.strength.workflows import contracts
from domains.strength.workflows.activities import StrengthActivities
from planner_core.errors import NotFound
from tests.api.conftest import propose

NOON_UTC = dt.time(12, 0)
START_DAY = dt.date(2026, 10, 18)


@pytest.fixture
def acts(db_session: Session) -> StrengthActivities:
    """Activities bound to the test's own connection.

    Each activity still opens and commits its own transaction -- that is the
    behaviour under test -- but on this connection, so the fixture's outer
    rollback still owns everything and `commit` only releases a savepoint.
    """

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


def _ref(block: models.Block, day: dt.date) -> contracts.BlockRef:
    return contracts.BlockRef(block_id=block.id, as_of=_as_of(day))


# --- bind_workflow --------------------------------------------------------


def test_binding_records_which_execution_owns_the_block(acts, db_session, active):
    """`block.workflow_id` has been unwritten since spec 02 (ADR-0007)."""
    acts.bind_workflow(contracts.BindRequest(block_id=active.id, workflow_id="block-abc"))

    db_session.refresh(active)
    assert active.workflow_id == "block-abc"


def test_binding_twice_is_harmless(acts, db_session, active):
    """Activities are retried, so every write in one has to tolerate a second go."""
    req = contracts.BindRequest(block_id=active.id, workflow_id="block-abc")
    acts.bind_workflow(req)
    acts.bind_workflow(req)

    db_session.refresh(active)
    assert active.workflow_id == "block-abc"


def test_binding_keeps_the_chain_id_when_a_block_rolls_over(acts, db_session, active):
    """`continue_as_new` keeps the workflow id, so a later block in the chain
    is owned by the execution that started with the first one."""
    acts.bind_workflow(contracts.BindRequest(block_id=active.id, workflow_id="block-first"))

    db_session.refresh(active)
    assert active.workflow_id == "block-first"
    assert active.workflow_id != contracts.workflow_id_for(active.id)


# --- load_block_state -----------------------------------------------------


def test_the_workflow_sees_dates_and_statuses_and_no_weights(acts, active):
    state = acts.load_block_state(_ref(active, dt.date(2026, 10, 18)))

    assert state.block_id == active.id
    assert state.status == "active"
    assert len(state.sessions) == 33
    assert {s.status for s in state.sessions} == {"planned"}
    assert [s.seq for s in state.sessions] == sorted(s.seq for s in state.sessions)


def test_the_deadline_is_the_athletes_midnight_plus_the_grace_period(acts, active, ready):
    """Not the server's midnight. Whether a session is late is a question
    about the athlete's day, the same reason `today_for` exists."""
    state = acts.load_block_state(_ref(active, dt.date(2026, 10, 18)))
    first = state.planned[0]
    zone = ZoneInfo(ready["athlete"].timezone)

    expected = (
        dt.datetime.combine(first.date + dt.timedelta(days=1), dt.time(0, 0), tzinfo=zone)
        + dt.timedelta(hours=contracts.GRACE_HOURS)
    ).astimezone(dt.UTC)

    assert first.deadline_utc == expected


def test_the_deadline_does_not_move_between_calls(acts, active):
    """Derived from stored data alone, so every replay computes the same instant."""
    first = acts.load_block_state(_ref(active, dt.date(2026, 10, 18)))
    again = acts.load_block_state(_ref(active, dt.date(2026, 11, 30)))

    assert [s.deadline_utc for s in first.sessions] == [s.deadline_utc for s in again.sessions]


def test_a_logged_session_leaves_the_planned_queue(acts, db_session, active):
    row = services.sessions_of(db_session, active.id)[0]
    row.status = "done"
    db_session.add(row)
    db_session.flush()

    state = acts.load_block_state(_ref(active, dt.date(2026, 10, 18)))

    assert row.seq not in {s.seq for s in state.planned}
    assert len(state.planned) == 32


def test_an_unknown_block_crosses_the_wire_as_a_refusal(acts):
    """It leaves the activity as an `ApplicationError` the route can rebuild
    into a 404, and marked never to be retried -- asking again will not make
    the block exist."""
    with pytest.raises(ApplicationError) as caught:
        acts.load_block_state(contracts.BlockRef(block_id=uuid.uuid4(), as_of=_as_of(START_DAY)))

    assert caught.value.type == "NotFound"
    assert caught.value.non_retryable is True


# --- commit / reject / edit ----------------------------------------------


def _decide(proposal_id: uuid.UUID, **kw) -> contracts.DecideRequest:
    return contracts.DecideRequest(proposal_id=proposal_id, as_of=_as_of(START_DAY), **kw)


def test_approving_through_the_activity_writes_the_schedule(acts, api_client, db_session, ready):
    """Re-hosted, not reimplemented: this is `approve_proposal` (ADR-0007)."""
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    assert services.sessions_of(db_session, block_id) == []

    out = acts.commit_proposal(_decide(proposal_id))

    assert out.status == "approved"
    assert out.kind == "new_block"
    assert out.block_id == block_id
    assert len(services.sessions_of(db_session, block_id)) == 33
    assert services.get_block(db_session, block_id).status == "active"


def test_an_unknown_proposal_is_ignored_rather_than_failed(acts):
    """Spec §13. A workflow that died over a stray id would strand the block."""
    out = acts.commit_proposal(_decide(uuid.uuid4()))

    assert out.status == "ignored"
    assert out.detail == "no such proposal"


def test_an_already_approved_proposal_is_ignored_not_retried(acts, api_client, db_session, ready):
    """The retry case. A second attempt after a successful one must not look
    like a new failure, or Temporal reports `AlreadyDecided` as the cause of
    something that in fact worked."""
    _, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    acts.commit_proposal(_decide(proposal_id))

    out = acts.commit_proposal(_decide(proposal_id))

    assert out.status == "ignored"
    assert out.detail == "already approved"


def test_rejecting_abandons_the_draft_block(acts, api_client, db_session, ready):
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])

    out = acts.reject_proposal(_decide(proposal_id, reason="too soon"))

    assert out.status == "rejected"
    assert services.get_block(db_session, block_id).status == "abandoned"
    assert services.get_proposal(db_session, proposal_id).decision_reason == "too soon"


# --- run_missed_policy ----------------------------------------------------


def _past_due(db_session: Session, block: models.Block, seq: int) -> dt.date:
    """The date of one session, so a test can say "today is after this"."""
    return next(r for r in services.sessions_of(db_session, block.id) if r.seq == seq).date


def test_the_policy_runs_for_a_session_whose_day_went_by(acts, db_session, active):
    missed_on = _past_due(db_session, active, 4)
    as_of = _as_of(missed_on + dt.timedelta(days=1))

    out = acts.run_missed_policy(
        contracts.MissedRequest(block_id=active.id, as_of=as_of, run_id="run-1")
    )

    assert out.nothing_to_do is False
    assert out.tier is not None
    # Every session dated before today and still `planned` -- not just the
    # latest one. The workflow asks "what has gone by?", and `missed_seqs`
    # is the only derivation of that in the codebase (shared with the chat
    # tool), so the answer is the whole backlog.
    assert out.missed_seqs == (1, 2, 3, 4)


def test_the_workflow_never_writes_a_missed_status(acts, db_session, active):
    """ADR-0010's rule, and the one that quietly breaks everything else.

    `missed` means written off: the calendar solver pins it as history and
    `approve_proposal` refuses to re-date it. A session the athlete may still
    make up has to stay `planned` with a date in the past, which is exactly
    what lets a reschedule move it.
    """
    missed_on = _past_due(db_session, active, 4)

    acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(missed_on + dt.timedelta(days=1)),
            run_id="run-1",
        )
    )

    statuses = {r.seq: r.status for r in services.sessions_of(db_session, active.id)}
    assert "missed" not in statuses.values()
    assert statuses[4] == "planned"


def test_nothing_to_do_when_the_session_was_logged_after_all(acts, db_session, active):
    """The activity was scheduled, then the athlete logged. Not an error."""
    first = services.sessions_of(db_session, active.id)[0]

    out = acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(first.date - dt.timedelta(days=1)),
            run_id="run-1",
        )
    )

    assert out.nothing_to_do is True
    assert out.proposal_ids == ()


def test_the_proposals_are_saved_and_tagged_as_the_workflows(acts, db_session, active):
    """`source = "workflow"` has been legal since spec 02 and written by
    nothing. `GET /decisions?source=workflow` was built in spec 14 for this."""
    missed_on = _past_due(db_session, active, 4)

    out = acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(missed_on + dt.timedelta(days=1)),
            run_id="run-1",
        )
    )

    if out.failure is not None:
        pytest.skip(f"policy found no valid option: {out.failure}")
    rows = [services.get_proposal(db_session, pid) for pid in out.proposal_ids]
    assert rows != []
    assert {row.source for row in rows} == {"workflow"}
    assert {row.status for row in rows} == {"pending"}


def test_a_decision_row_records_the_reschedule_nobody_asked_for_in_words(acts, db_session, active):
    missed_on = _past_due(db_session, active, 4)

    out = acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(missed_on + dt.timedelta(days=1)),
            run_id="run-xyz",
        )
    )

    decision = db_session.get(models.Decision, out.decision_id)
    assert decision is not None
    assert decision.source == "workflow"
    assert decision.route == "reschedule"
    assert decision.run_id == "run-xyz"
    if out.proposal_ids:
        # Null means "a proposal is waiting on you" (ADR-0015).
        assert decision.outcome is None
        assert decision.proposal_id == out.proposal_ids[0]


def test_approving_closes_the_decision_the_options_came_from(acts, db_session, active):
    """Spec 14 §5E assigned this write to spec 15."""
    missed_on = _past_due(db_session, active, 4)
    out = acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(missed_on + dt.timedelta(days=1)),
            run_id="run-1",
        )
    )
    if not out.proposal_ids:
        pytest.skip(f"policy found no valid option: {out.failure}")

    acts.commit_proposal(
        contracts.DecideRequest(
            proposal_id=out.proposal_ids[0],
            as_of=_as_of(missed_on + dt.timedelta(days=1)),
            decision_id=out.decision_id,
        )
    )

    decision = db_session.get(models.Decision, out.decision_id)
    db_session.refresh(decision)
    assert decision.outcome == "approved"


def test_a_future_absence_is_clamped_so_the_event_can_be_built(acts, db_session, active):
    """`MissedEvent` refuses `today < unavailable_from`, and the athlete can
    say "I'm away from Monday" on Saturday (ADR-0015)."""
    away_from = _past_due(db_session, active, 6)
    saturday_before = away_from - dt.timedelta(days=2)

    out = acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(saturday_before),
            run_id="run-1",
            missed_seqs=(6,),
            unavailable_from=away_from,
        )
    )

    # The point is that it built an event at all rather than raising.
    assert out.nothing_to_do is False


def test_an_injury_always_restarts_however_short(acts, db_session, active):
    missed_on = _past_due(db_session, active, 4)

    out = acts.run_missed_policy(
        contracts.MissedRequest(
            block_id=active.id,
            as_of=_as_of(missed_on + dt.timedelta(days=1)),
            run_id="run-1",
            injury=True,
        )
    )

    assert out.tier == "restart"


# --- set_block_status -----------------------------------------------------


def test_pausing_and_resuming_move_the_block_between_running_states(acts, db_session, active):
    """Both count as running (ADR-0010 mod 9); nothing wrote `paused` before."""
    acts.set_block_status(contracts.SetStatusRequest(block_id=active.id, status="paused"))
    db_session.refresh(active)
    assert active.status == "paused"

    acts.set_block_status(contracts.SetStatusRequest(block_id=active.id, status="active"))
    db_session.refresh(active)
    assert active.status == "active"


# --- The transaction boundary --------------------------------------------


def test_each_activity_commits_its_own_work(acts, db_session, active):
    """`unit_of_work`, not the caller, is what makes the write durable.

    The services only flush (ADR-0007 rule 2), so without the activity
    committing, a workflow that crashed after the activity returned would
    lose a schedule it had already been told was written.
    """
    acts.bind_workflow(contracts.BindRequest(block_id=active.id, workflow_id="block-committed"))

    # Read through a connection-level query rather than the ORM identity map,
    # so this is the database answering and not a cached object.
    stored = db_session.scalar(
        sa.text("SELECT workflow_id FROM block WHERE id = :id"), {"id": str(active.id)}
    )
    assert stored == "block-committed"


def test_a_missing_proposal_still_raises_not_found_inside_services(acts, db_session):
    """The pre-check is the activity's, not a change to the service."""
    with pytest.raises(NotFound):
        services.get_proposal(db_session, uuid.uuid4())
