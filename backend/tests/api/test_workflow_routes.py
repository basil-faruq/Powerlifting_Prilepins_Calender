"""The API's half of spec 15: where a decision goes, and what comes back.

The durable behaviour is `test_block_workflow_run.py`'s subject. This file is
about the routing and the translation -- which execution a decision is sent
to, and whether the answer still looks the way it did when approval was a
direct database write.

That second half is the one that would rot quietly. `api/main.py` is the only
place that knows an error is also a status code (ADR-0007 rule 2), and it
only ever sees the exception it is handed. If a `ProposalStale` raised inside
an activity arrives as something else, an honest 409 becomes a 500 and the
client is told the server is broken when the real answer is "re-propose".
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session

from api import temporal_client
from domains.strength import models, services
from domains.strength.workflows import contracts
from planner_core.errors import TemporalUnavailable
from tests.api.conftest import propose


@pytest.fixture
def active(api_client, db_session: Session, ready) -> models.Block:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200
    block = services.get_block(db_session, block_id)
    db_session.refresh(block)
    return block


# --- Which execution hears about a decision -------------------------------


def test_approving_a_first_block_starts_its_workflow(
    api_client, db_session, ready, _in_process_workflows
):
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])

    response = api_client.post(f"/proposals/{proposal_id}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert contracts.workflow_id_for(block_id) in _in_process_workflows.started


def test_the_block_is_bound_to_the_execution_that_owns_it(api_client, db_session, ready):
    """ADR-0007 left `block.workflow_id` unwritten; spec 15 writes it."""
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])

    api_client.post(f"/proposals/{proposal_id}/approve")

    block = services.get_block(db_session, block_id)
    db_session.refresh(block)
    assert block.workflow_id == contracts.workflow_id_for(block_id)


def test_a_restart_is_approved_against_the_block_it_replaces(
    api_client, db_session, ready, active, monkeypatch
):
    """The routing decision that is easy to get wrong.

    A restart is a `new_block` proposal, but there is already an execution
    running -- the one for the block being replaced, sitting on its own
    reschedule question. Starting a second workflow would leave the first
    waiting for an answer that never comes, so the approval goes to the old
    one, which retires its block and continues as new into the replacement.
    """
    replacement = services.create_block_proposal(
        db_session,
        active.athlete_id,
        active.template_id,
        list(active.lifts),
        _next_monday_after(active),
        source="workflow",
    )
    payload = services.read_payload(replacement)
    stored = payload.model_copy(update={"supersedes_block_id": active.id})
    replacement.payload = stored.model_dump(mode="json")
    db_session.add(replacement)
    db_session.flush()

    sent: list[tuple[str, str]] = []

    async def _record(workflow_id, name, *args, result_type=None):
        sent.append((workflow_id, name))
        return contracts.CommitOutcome(proposal_id=args[0], status="approved")

    monkeypatch.setattr(temporal_client, "update", _record)

    api_client.post(f"/proposals/{replacement.id}/approve")

    assert sent == [(active.workflow_id, contracts.APPROVE)]


def _next_monday_after(block: models.Block):
    import datetime as dt

    day = block.start_date + dt.timedelta(weeks=12)
    return day + dt.timedelta(days=(7 - day.weekday()) % 7)


def test_rejecting_a_block_that_never_started_is_a_plain_write(
    api_client, db_session, ready, _in_process_workflows
):
    """Spec §3: "or rejects directly if no workflow yet".

    There is no execution to tell, and starting one in order to decline it
    would be absurd.
    """
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])

    response = api_client.post(
        f"/proposals/{proposal_id}/reject", json={"reason": "wrong start date"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert _in_process_workflows.started == {}
    assert services.get_block(db_session, block_id).status == "abandoned"


# --- The status codes must not change -------------------------------------


def test_approving_twice_is_still_a_conflict(api_client, ready):
    """Spec 06 §12, unchanged by the workflow going in between.

    The route is strict where the workflow is lenient, and both are right: a
    human clicking a stale button should be told, while a workflow handed an
    id it does not recognise should shrug and keep running rather than
    strand eleven weeks of training.
    """
    _, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200

    again = api_client.post(f"/proposals/{proposal_id}/approve")

    assert again.status_code == 409
    assert "already approved" in again.json()["detail"]


def test_approving_something_that_does_not_exist_is_still_a_404(api_client, ready):
    response = api_client.post(f"/proposals/{uuid.uuid4()}/approve")

    assert response.status_code == 404


def test_a_workflow_service_that_is_down_is_a_503(api_client, ready, monkeypatch):
    """The one new failure mode, and ADR-0007 predicted the handler for it."""
    _, proposal_id = propose(api_client, ready["athlete"], ready["template"])

    async def _down(*_args, **_kwargs):
        raise TemporalUnavailable("localhost:7233", "connection refused")

    monkeypatch.setattr(temporal_client, "start_block_workflow", _down)

    response = api_client.post(f"/proposals/{proposal_id}/approve")

    assert response.status_code == 503
    assert "make worker" in response.json()["detail"]


def test_logging_a_session_still_succeeds_with_the_workflow_down(
    api_client, db_session, active, monkeypatch
):
    """The asymmetry that matters most in this spec.

    Spec §13 says "503 on approve / log routes". It is wrong about the log:
    the record is committed before the signal, so answering 503 would be a
    lie about work already done -- and a training log must never be lost to
    infrastructure (ADR-0014, and `log_session`'s own docstring).
    """

    async def _down(*_args, **_kwargs):
        raise TemporalUnavailable("localhost:7233", "connection refused")

    monkeypatch.setattr(temporal_client, "signal", _down)
    row = services.sessions_of(db_session, active.id)[0]
    lifts = services.lifts_of(db_session, row.id)

    response = api_client.post(
        f"/sessions/{row.id}/log",
        json={
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

    assert response.status_code == 201
    db_session.expire_all()
    assert services.get_session_row(db_session, row.id).status == "done"


# --- The four new endpoints -----------------------------------------------


def test_a_block_with_no_workflow_reports_none_rather_than_failing(api_client, db_session, ready):
    """A draft awaiting its first approval is an expected condition, not an
    error, and a 404 would make the UI branch on a failure."""
    block_id, _ = propose(api_client, ready["athlete"], ready["template"])

    response = api_client.get(f"/blocks/{block_id}/state")

    assert response.status_code == 200
    assert response.json()["state"] == "none"


def test_a_running_block_reports_what_the_workflow_says(api_client, active):
    response = api_client.get(f"/blocks/{active.id}/state")

    assert response.status_code == 200
    assert response.json()["state"] == "ACTIVE"


def test_reporting_an_absence_reaches_the_workflow(api_client, active, _in_process_workflows):
    response = api_client.post(
        f"/blocks/{active.id}/unavailable",
        json={"from": "2026-11-02", "until": "2026-11-09", "injury": False},
    )

    assert response.status_code == 202
    names = [name for _, name, _ in _in_process_workflows.signals]
    assert contracts.REPORT_UNAVAILABLE in names


def test_pause_and_resume_reach_the_workflow(api_client, active, _in_process_workflows):
    assert api_client.post(f"/blocks/{active.id}/pause", json={"reason": "flu"}).status_code == 202
    assert api_client.post(f"/blocks/{active.id}/resume", json={}).status_code == 202

    names = [name for _, name, _ in _in_process_workflows.signals]
    assert names[-2:] == [contracts.PAUSE, contracts.RESUME]


def test_signalling_a_block_that_was_never_started_is_a_conflict(api_client, db_session, ready):
    """Telling an execution that does not exist that you are ill achieves
    nothing, and the athlete should hear that their block never started."""
    block_id, _ = propose(api_client, ready["athlete"], ready["template"])

    response = api_client.post(f"/blocks/{block_id}/pause", json={"reason": "never started"})

    assert response.status_code == 409


def test_the_absence_window_accepts_the_word_the_athlete_would_use(api_client, active):
    """`from` on the wire, `from_date` in Python. The alias is not cosmetic:
    `from` is a keyword, and the frontend should not have to care."""
    response = api_client.post(f"/blocks/{active.id}/unavailable", json={"from": "2026-11-02"})

    assert response.status_code == 202


def test_an_unknown_field_in_an_absence_is_refused(api_client, active):
    response = api_client.post(
        f"/blocks/{active.id}/unavailable",
        json={"from": "2026-11-02", "untill": "2026-11-09"},
    )

    assert response.status_code == 422
