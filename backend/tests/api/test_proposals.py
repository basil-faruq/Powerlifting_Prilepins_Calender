"""The approval gate: approve, reject, edit, supersede, and staleness.

No solver exists yet (spec 07), so the reschedule and weekday-change proposals
here are built by hand from the plan the `new_block` proposal already carries.
That is the honest way to test spec 06 in isolation: the shapes are spec 05's,
and what is under test is what approval *does* with them.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from domains.strength import models
from tests.api.conftest import START, propose


@pytest.fixture
def approved(api_client, ready, athlete, template) -> dict:
    """An active block with all 33 sessions written, and its plan."""
    block_id, proposal_id = propose(api_client, athlete, template)
    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 200, response.text
    return {
        "block_id": block_id,
        "proposal_id": proposal_id,
        "plan": response.json()["payload"]["plan"],
        "athlete": athlete,
    }


def _insert(db_session, block_id: uuid.UUID, kind: str, payload: dict) -> uuid.UUID:
    """A pending proposal of `kind`, as a solver would have left it."""
    row = models.Proposal(
        block_id=block_id,
        kind=kind,
        status="pending",
        payload=payload,
        source="chat",
    )
    db_session.add(row)
    db_session.flush()
    return row.id


def _shift(session: dict, days: int) -> dict:
    moved = dict(session)
    moved["date"] = (dt.date.fromisoformat(session["date"]) + dt.timedelta(days=days)).isoformat()
    return moved


# --- Approval -------------------------------------------------------------


def test_approval_writes_the_schedule_and_activates_the_block(api_client, approved):
    sessions = api_client.get(f"/blocks/{approved['block_id']}/sessions").json()
    assert len(sessions) == 33
    assert {row["status"] for row in sessions} == {"planned"}
    # 77 weights, the number the golden test pins (CLAUDE.md non-negotiable #4).
    assert sum(len(row["lifts"]) for row in sessions) == 77
    assert api_client.get(f"/blocks/{approved['block_id']}").json()["status"] == "active"


def test_a_second_approval_is_a_409(api_client, approved):
    response = api_client.post(f"/proposals/{approved['proposal_id']}/approve")
    assert response.status_code == 409
    assert "already approved" in response.json()["detail"]


def test_approving_a_new_block_twice_over_cannot_duplicate_sessions(
    api_client, ready, athlete, template, db_session
):
    """Two proposals for one draft block: the second is stale, not a duplicate."""
    block_id, first = propose(api_client, athlete, template)
    payload = api_client.get(f"/proposals/{first}").json()["payload"]
    second = _insert(db_session, block_id, "new_block", payload)

    assert api_client.post(f"/proposals/{first}/approve").status_code == 200
    response = api_client.post(f"/proposals/{second}/approve")
    assert response.status_code == 409
    # Superseded by the approval, so it never reaches the staleness check --
    # two guards, and the outer one fires first.
    assert "already superseded" in response.json()["detail"]
    assert len(api_client.get(f"/blocks/{block_id}/sessions").json()) == 33


def test_approval_supersedes_the_other_pending_proposals_for_the_block(
    api_client, ready, athlete, template, db_session
):
    block_id, first = propose(api_client, athlete, template)
    payload = api_client.get(f"/proposals/{first}").json()["payload"]
    sibling = _insert(db_session, block_id, "new_block", payload)

    api_client.post(f"/proposals/{first}/approve")
    assert api_client.get(f"/proposals/{sibling}").json()["status"] == "superseded"


def test_an_unknown_proposal_is_a_404(api_client):
    assert api_client.post(f"/proposals/{uuid.uuid4()}/approve").status_code == 404


# --- Rescheduling ---------------------------------------------------------


def test_approving_a_move_moves_the_session_and_keeps_its_original_date(
    api_client, approved, db_session
):
    plan = approved["plan"]
    last = plan[-1]
    was = last["date"]
    now = (dt.date.fromisoformat(was) + dt.timedelta(days=1)).isoformat()

    proposal_id = _insert(
        db_session,
        approved["block_id"],
        "reschedule",
        {
            "plan": [*plan[:-1], _shift(last, 1)],
            "changes": [
                {
                    "seq": last["seq"],
                    "week": last["week"],
                    "slot": last["slot"],
                    "action": "move",
                    "from_date": was,
                    "to_date": now,
                }
            ],
        },
    )
    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 200, response.text

    sessions = api_client.get(f"/blocks/{approved['block_id']}/sessions").json()
    moved = next(row for row in sessions if row["seq"] == last["seq"])
    assert moved["date"] == now
    assert moved["status"] == "moved"
    # The original plan stays visible, which is what spec 21's trace reads.
    assert moved["original_date"] == was
    # Nothing else was touched.
    assert [row["status"] for row in sessions].count("moved") == 1


def test_a_move_from_a_date_the_session_no_longer_has_is_stale(api_client, approved, db_session):
    """Staleness is about the premise, not the plan: re-propose, do not edit."""
    plan = approved["plan"]
    last = plan[-1]
    wrong = (dt.date.fromisoformat(last["date"]) - dt.timedelta(days=7)).isoformat()
    to = (dt.date.fromisoformat(last["date"]) + dt.timedelta(days=1)).isoformat()

    proposal_id = _insert(
        db_session,
        approved["block_id"],
        "reschedule",
        {
            "plan": [*plan[:-1], _shift(last, 1)],
            "changes": [
                {
                    "seq": last["seq"],
                    "week": last["week"],
                    "slot": last["slot"],
                    "action": "move",
                    "from_date": wrong,
                    "to_date": to,
                }
            ],
        },
    )
    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 409
    assert "this proposal was computed against" in response.json()["detail"]


def test_a_reschedule_against_a_draft_block_is_stale(
    api_client, ready, athlete, template, db_session
):
    block_id, first = propose(api_client, athlete, template)
    payload = api_client.get(f"/proposals/{first}").json()["payload"]
    proposal_id = _insert(db_session, block_id, "reschedule", payload)

    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 409
    assert "needs it active or paused" in response.json()["detail"]


def test_approving_a_skip_marks_the_session_skipped(api_client, approved, db_session):
    plan = approved["plan"]
    first = plan[0]
    proposal_id = _insert(
        db_session,
        approved["block_id"],
        "reschedule",
        {
            "plan": plan,
            "changes": [
                {
                    "seq": first["seq"],
                    "week": first["week"],
                    "slot": first["slot"],
                    "action": "skip",
                    "from_date": first["date"],
                }
            ],
        },
    )
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200

    sessions = api_client.get(f"/blocks/{approved['block_id']}/sessions").json()
    assert next(row for row in sessions if row["seq"] == first["seq"])["status"] == "skipped"
    assert len(sessions) == 33


def test_approving_a_created_session_writes_it_with_its_lifts(api_client, approved, db_session):
    """`create` and `repeat_week` both arrive as plan entries with fresh seqs."""
    plan = approved["plan"]
    last = plan[-1]
    # The Monday after the block's final Friday: three days' rest, and Day 1's
    # home weekday, so only the new session's own seq needs waiving.
    added = dict(plan[0])
    added["seq"] = 34
    added["week"] = 11
    added["date"] = (dt.date.fromisoformat(last["date"]) + dt.timedelta(days=3)).isoformat()

    proposal_id = _insert(
        db_session,
        approved["block_id"],
        "reschedule",
        {
            "plan": [*plan, added],
            "changes": [
                {
                    "seq": 34,
                    "week": 11,
                    "slot": added["slot"],
                    "action": "create",
                    "to_date": added["date"],
                }
            ],
        },
    )
    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 200, response.text

    sessions = api_client.get(f"/blocks/{approved['block_id']}/sessions").json()
    assert len(sessions) == 34
    fresh = next(row for row in sessions if row["seq"] == 34)
    assert fresh["date"] == added["date"]
    assert fresh["status"] == "planned"
    # A set: the response orders lifts by code, the plan by template order.
    assert {lift["lift_code"] for lift in fresh["lifts"]} == {
        lift["lift_code"] for lift in added["lifts"]
    }


def test_approving_a_weekday_change_installs_the_map_and_the_dates(
    api_client, approved, db_session
):
    """The payload spec 05 never defined, and spec 06 §11 requires."""
    plan = approved["plan"]
    proposal_id = _insert(
        db_session,
        approved["block_id"],
        "weekday_change",
        {
            # Tue / Thu / Sat, every session a day later to match.
            "slots": {"1": 1, "2": 3, "3": 5},
            "plan": [_shift(row, 1) for row in plan],
            "changes": [],
        },
    )
    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 200, response.text

    detail = api_client.get(f"/athletes/{approved['athlete'].id}").json()
    assert [row["weekday"] for row in detail["weekday_map"]] == [1, 3, 5]
    # The lock survives the change: this block is still running.
    assert {row["locked_from"] for row in detail["weekday_map"]} == {START.isoformat()}

    sessions = api_client.get(f"/blocks/{approved['block_id']}/sessions").json()
    assert sessions[0]["date"] == (START + dt.timedelta(days=1)).isoformat()
    assert {row["status"] for row in sessions} == {"moved"}


def test_a_weekday_change_that_does_not_match_its_own_plan_is_rejected(
    api_client, approved, db_session
):
    """The map and the dates have to agree, or every session is off-weekday."""
    proposal_id = _insert(
        db_session,
        approved["block_id"],
        "weekday_change",
        {
            "slots": {"1": 1, "2": 3, "3": 5},
            # The dates were never shifted.
            "plan": approved["plan"],
            "changes": [],
        },
    )
    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 422
    rules = {violation["rule"] for violation in response.json()["violations"]}
    assert rules == {"off_weekday"}


# --- Rejecting ------------------------------------------------------------


def test_rejecting_a_new_block_abandons_it_and_records_why(api_client, ready, athlete, template):
    block_id, proposal_id = propose(api_client, athlete, template)
    response = api_client.post(
        f"/proposals/{proposal_id}/reject", json={"reason": "starting a week later"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert response.json()["decision_reason"] == "starting a week later"
    assert response.json()["decided_at"] is not None

    assert api_client.get(f"/blocks/{block_id}").json()["status"] == "abandoned"
    assert api_client.get(f"/blocks/{block_id}/sessions").json() == []


def test_a_reject_needs_no_reason(api_client, ready, athlete, template):
    _, proposal_id = propose(api_client, athlete, template)
    response = api_client.post(f"/proposals/{proposal_id}/reject", json={})
    assert response.status_code == 200, response.text
    assert response.json()["decision_reason"] is None


def test_a_decided_proposal_cannot_be_rejected(api_client, approved):
    response = api_client.post(f"/proposals/{approved['proposal_id']}/reject", json={})
    assert response.status_code == 409


# --- Editing --------------------------------------------------------------


def test_an_edit_re_dates_the_plan_and_stays_pending(api_client, ready, athlete, template):
    block_id, proposal_id = propose(api_client, athlete, template)
    plan = api_client.get(f"/proposals/{proposal_id}").json()["payload"]["plan"]
    last = plan[-1]
    to = (dt.date.fromisoformat(last["date"]) + dt.timedelta(days=1)).isoformat()

    response = api_client.patch(
        f"/proposals/{proposal_id}",
        json={
            "changes": [
                {
                    "seq": last["seq"],
                    "week": last["week"],
                    "slot": last["slot"],
                    "action": "move",
                    "from_date": last["date"],
                    "to_date": to,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["payload"]["plan"][-1]["date"] == to
    # An edit is a counter-offer, not an approval: still no sessions.
    assert api_client.get(f"/blocks/{block_id}/sessions").json() == []


def test_an_edit_into_the_past_is_refused(api_client, ready, athlete, template):
    _, proposal_id = propose(api_client, athlete, template)
    plan = api_client.get(f"/proposals/{proposal_id}").json()["payload"]["plan"]
    first = plan[0]
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    response = api_client.patch(
        f"/proposals/{proposal_id}",
        json={
            "changes": [
                {
                    "seq": first["seq"],
                    "week": first["week"],
                    "slot": first["slot"],
                    "action": "move",
                    "from_date": first["date"],
                    "to_date": yesterday,
                }
            ]
        },
    )
    assert response.status_code == 422
    assert "past_date" in {v["rule"] for v in response.json()["violations"]}


def test_an_edit_cannot_invent_a_session(api_client, ready, athlete, template):
    """Sets, reps and weights are the generator's to compute, never an edit's."""
    _, proposal_id = propose(api_client, athlete, template)
    response = api_client.patch(
        f"/proposals/{proposal_id}",
        json={
            "changes": [
                {
                    "seq": 99,
                    "week": 11,
                    "slot": 1,
                    "action": "create",
                    "to_date": (START + dt.timedelta(days=90)).isoformat(),
                }
            ]
        },
    )
    assert response.status_code == 422
    assert "needs a new proposal" in response.json()["detail"]


# --- Listing --------------------------------------------------------------


def test_proposals_are_listed_by_athlete_and_status(api_client, ready, athlete, template):
    _, proposal_id = propose(api_client, athlete, template)

    pending = api_client.get("/proposals", params={"athlete_id": str(athlete.id)}).json()
    assert [row["id"] for row in pending] == [str(proposal_id)]

    api_client.post(f"/proposals/{proposal_id}/approve")
    assert api_client.get("/proposals", params={"athlete_id": str(athlete.id)}).json() == []
    approved_rows = api_client.get(
        "/proposals", params={"athlete_id": str(athlete.id), "status": "approved"}
    ).json()
    assert [row["id"] for row in approved_rows] == [str(proposal_id)]


def test_the_warnings_travel_with_the_proposal(api_client, ready, athlete, template):
    """Prilepin and the volume trend are advice, not refusals (non-negotiable #5)."""
    _, proposal_id = propose(api_client, athlete, template)
    warnings = api_client.get(f"/proposals/{proposal_id}").json()["warnings"]
    assert warnings
    assert {w["rule"] for w in warnings} <= {
        "prilepin_reps_per_set",
        "prilepin_total_reps",
        "volume_trend",
    }
