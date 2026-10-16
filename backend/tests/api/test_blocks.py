"""Previewing and proposing a block.

The claim these tests exist to make is spec 06's third Definition of Done item:
`POST /blocks` writes a block row and nothing else. The schedule is the
proposal's to carry until a human approves it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from domains.strength import models
from tests.api.conftest import ALL_LIFTS, START, TESTED_ON, block_body, propose


def test_a_preview_is_thirty_three_sessions_and_a_clean_verdict(
    api_client, ready, athlete, template
):
    """CLAUDE.md non-negotiable #5, over HTTP: the author's own plan is accepted."""
    response = api_client.post("/blocks/preview", json=block_body(athlete, template))
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["draft"]["sessions"]) == 33
    assert body["validation"]["ok"] is True
    assert body["validation"]["violations"] == []
    # Prilepin and the volume trend fire on the authored block, as warnings.
    assert body["validation"]["warnings"]


def test_a_preview_writes_nothing(api_client, ready, athlete, template, db_session):
    api_client.post("/blocks/preview", json=block_body(athlete, template))
    assert db_session.scalars(models.Block.__table__.select()).first() is None


def test_a_preview_flags_a_plan_built_on_synthetic_maxes(
    api_client, athlete, template, weekday_map, db_session
):
    """ADR-0004 rule 4: every weight below came from the snapshot."""
    for lift in ALL_LIFTS:
        db_session.add(
            models.OneRepMax(
                athlete_id=athlete.id,
                lift_code=lift,
                kg=Decimal("100"),
                set_on=TESTED_ON,
                source="synthetic",
                is_synthetic=True,
            )
        )
    db_session.flush()

    response = api_client.post("/blocks/preview", json=block_body(athlete, template))
    assert response.status_code == 200, response.text
    assert response.json()["has_synthetic_one_rms"] is True


def test_a_missing_max_is_a_422_naming_the_lift(
    api_client, athlete, template, weekday_map, db_session
):
    """No 1RM is ever invented, so the plan simply cannot be built."""
    db_session.add(
        models.OneRepMax(
            athlete_id=athlete.id,
            lift_code="squat",
            kg=Decimal("125"),
            set_on=TESTED_ON,
            source="tested",
        )
    )
    db_session.flush()

    response = api_client.post("/blocks/preview", json=block_body(athlete, template))
    assert response.status_code == 422
    assert "no 1RM for" in response.json()["detail"]


def test_an_unset_weekday_map_is_a_422_about_the_map(api_client, athlete, template, maxes):
    response = api_client.post("/blocks/preview", json=block_body(athlete, template))
    assert response.status_code == 422
    rules = {violation["rule"] for violation in response.json()["violations"]}
    assert rules == {"weekday_map"}
    assert "has no weekday" in " ".join(response.json()["problems"])


def test_a_block_must_start_on_a_monday(api_client, ready, athlete, template):
    tuesday = START + dt.timedelta(days=1)
    response = api_client.post(
        "/blocks/preview", json=block_body(athlete, template, start_date=tuesday.isoformat())
    )
    assert response.status_code == 422
    assert "Monday" in response.json()["detail"]


def test_proposing_a_block_writes_no_sessions(api_client, ready, athlete, template, db_session):
    """Spec 06 §14: approval is the only path that writes sessions."""
    block_id, proposal_id = propose(api_client, athlete, template)

    assert api_client.get(f"/blocks/{block_id}").json()["status"] == "draft"
    assert api_client.get(f"/blocks/{block_id}/sessions").json() == []
    assert api_client.get(f"/proposals/{proposal_id}").json()["status"] == "pending"
    # And nothing slipped in under another block either.
    assert db_session.scalars(models.PlannedSession.__table__.select()).first() is None


def test_a_proposed_block_records_the_maxes_it_was_computed_from(
    api_client, ready, athlete, template
):
    block_id, _ = propose(api_client, athlete, template)
    snapshot = api_client.get(f"/blocks/{block_id}").json()["one_rm_snapshot"]
    assert Decimal(snapshot["squat"]) == Decimal("125")


def test_there_is_no_current_block_until_one_is_approved(api_client, ready, athlete, template):
    assert api_client.get(f"/athletes/{athlete.id}/blocks/current").status_code == 404

    propose(api_client, athlete, template)
    # A draft is not current: it has no sessions and may never be approved.
    assert api_client.get(f"/athletes/{athlete.id}/blocks/current").status_code == 404


def test_the_session_list_can_be_windowed(api_client, ready, athlete, template):
    _, proposal_id = propose(api_client, athlete, template)
    block_id = api_client.post(f"/proposals/{proposal_id}/approve").json()["block_id"]

    first_week = api_client.get(
        f"/blocks/{block_id}/sessions",
        params={"from": START.isoformat(), "to": (START + dt.timedelta(days=6)).isoformat()},
    ).json()
    assert [row["seq"] for row in first_week] == [1, 2, 3]
    assert {row["week"] for row in first_week} == {1}


def test_an_unknown_block_is_a_404(api_client):
    assert api_client.get(f"/blocks/{uuid.uuid4()}").status_code == 404
    assert api_client.get(f"/blocks/{uuid.uuid4()}/sessions").status_code == 404
