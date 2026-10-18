"""`GET /decisions`, and the join that stands in for two missing columns.

Spec 14 §3 filters by athlete, and `decision` has no `athlete_id`. D1 takes no
migration, so the athlete and the input message come from the turn's own
`chat_message` row. The two tests that matter are the ones proving that join
behaves: **one row per decision** despite a turn writing two messages, and a
workflow decision -- which has no chat message at all -- still listed, with
nulls, under `?source=workflow`.

Needs Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session

from domains.strength import models, services
from domains.strength.agent.schemas import ChatModels, RouterChoice
from tests.api.conftest import propose
from tests.fakes import replying_model, router_model


@pytest.fixture
def active(api_client, db_session: Session, ready) -> models.Block:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    return services.get_block(db_session, block_id)


def _turn(chat_client, athlete, message: str = "my knee hurts") -> dict:
    response = chat_client.post("/chat", json={"athlete_id": str(athlete.id), "message": message})
    assert response.status_code == 200, response.text
    return response.json()


# --- The listing --------------------------------------------------------


def test_a_turn_appears_once_despite_writing_two_messages(chat_client, ready):
    """`role = 'user'` in the join is what reduces a turn to one row.

    Without it a LEFT JOIN on `decision_id` alone returns the decision twice
    -- once for the question and once for the reply -- and the trail reads as
    though every turn happened twice.
    """
    turn = _turn(chat_client, ready["athlete"])

    response = chat_client.get("/decisions", params={"athlete_id": str(ready["athlete"].id)})

    assert response.status_code == 200, response.text
    body = response.json()
    matching = [row for row in body if row["id"] == turn["decision_id"]]
    assert len(matching) == 1


def test_the_listing_carries_the_athlete_and_the_question(chat_client, ready):
    """Neither has a column on `decision`; both come from the join (D1)."""
    _turn(chat_client, ready["athlete"], "my shoulder hurts")

    (row, *_rest) = chat_client.get(
        "/decisions", params={"athlete_id": str(ready["athlete"].id)}
    ).json()

    assert row["athlete_id"] == str(ready["athlete"].id)
    assert row["message"] == "my shoulder hurts"


def test_the_listing_is_newest_first(chat_client, ready):
    _turn(chat_client, ready["athlete"], "my knee hurts")
    second = _turn(chat_client, ready["athlete"], "my elbow hurts")

    body = chat_client.get("/decisions", params={"athlete_id": str(ready["athlete"].id)}).json()

    assert body[0]["id"] == second["decision_id"]


def test_filtering_by_route(chat_client, ready):
    _turn(chat_client, ready["athlete"], "my knee hurts")

    safety = chat_client.get("/decisions", params={"route": "safety"}).json()
    plans = chat_client.get("/decisions", params={"route": "plan"}).json()

    assert safety
    assert all(row["route"] == "safety" for row in safety)
    assert plans == []


def test_filtering_by_outcome(chat_client, ready):
    _turn(chat_client, ready["athlete"], "my knee hurts")

    answered = chat_client.get("/decisions", params={"outcome": "answered"}).json()

    assert answered
    assert all(row["outcome"] == "answered" for row in answered)


def test_a_pending_proposal_leaves_a_null_outcome(chat_client, db_session, ready, active):
    """D1: `outcome IS NULL` is what spec 21 renders as `pending`, with no new
    enum value and no migration."""
    import datetime as dt

    from api.deps import get_chat_models
    from api.main import app

    today = services.today_for(ready["athlete"])
    monday = today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)
    app.dependency_overrides[get_chat_models] = lambda: ChatModels(
        router=router_model(RouterChoice(kind="plan", lifts=["squat", "bench"], start_date=monday)),
        agent=replying_model("Ready for your approval."),
    )

    response = chat_client.post(
        "/chat",
        json={
            "athlete_id": str(ready["athlete"].id),
            "message": "new block with squat and bench on Monday",
            "template_id": str(ready["template"].id),
        },
    )
    assert response.status_code == 200, response.text

    (row, *_rest) = chat_client.get(
        "/decisions", params={"athlete_id": str(ready["athlete"].id), "route": "plan"}
    ).json()

    assert row["outcome"] is None
    assert row["proposal_id"] is not None


def test_the_limit_is_honoured(chat_client, ready):
    for _ in range(3):
        _turn(chat_client, ready["athlete"])

    body = chat_client.get("/decisions", params={"limit": 2}).json()

    assert len(body) == 2


def test_an_out_of_range_limit_is_422(chat_client, ready):
    assert chat_client.get("/decisions", params={"limit": 0}).status_code == 422
    assert chat_client.get("/decisions", params={"limit": 500}).status_code == 422


def test_an_unknown_route_value_is_422(chat_client, ready):
    assert chat_client.get("/decisions", params={"route": "banana"}).status_code == 422


# --- Workflow decisions -------------------------------------------------


def test_a_workflow_decision_is_listed_with_nulls(chat_client, db_session, ready):
    """Spec 15 writes these, and they have no chat message to join to.

    `?athlete_id=` deliberately will not match one -- there is nothing linking
    it to an athlete until spec 15 decides how. `?source=workflow` is the way
    in, which is the intended consequence of D1 rather than an oversight.
    """
    decision = models.Decision(
        run_id="workflow:abc123",
        source="workflow",
        route="reschedule",
        tool_calls=[],
    )
    db_session.add(decision)
    db_session.flush()

    by_source = chat_client.get("/decisions", params={"source": "workflow"}).json()
    by_athlete = chat_client.get(
        "/decisions", params={"athlete_id": str(ready["athlete"].id)}
    ).json()

    assert [row["id"] for row in by_source] == [str(decision.id)]
    assert by_source[0]["athlete_id"] is None
    assert by_source[0]["message"] is None
    assert str(decision.id) not in [row["id"] for row in by_athlete]


# --- One decision -------------------------------------------------------


def test_one_decision_carries_its_tool_calls_in_order(chat_client, ready, active):
    turn = _turn(chat_client, ready["athlete"])

    response = chat_client.get(f"/decisions/{turn['decision_id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == turn["decision_id"]
    assert body["tool_calls"]
    # D1: the router is always first, and is where spec 21 reads the extracted
    # route parameters from.
    assert body["tool_calls"][0]["name"] == "router"
    assert body["tool_calls"][0]["output_summary"]["filter_fired"] == "pain_keyword"


def test_an_unknown_decision_is_404(chat_client, ready):
    response = chat_client.get(f"/decisions/{uuid.uuid4()}")

    assert response.status_code == 404
    assert "detail" in response.json()


def test_the_trace_url_is_null_with_tracing_off(chat_client, ready):
    """Spec 14 §12: without keys, everything works and the link is null."""
    turn = _turn(chat_client, ready["athlete"])

    body = chat_client.get(f"/decisions/{turn['decision_id']}").json()

    assert body["trace_url"] is None
    assert body["run_id"] == f"chat:{turn['conversation_id']}"


def test_the_routes_are_documented(client):
    spec = client.get("/openapi.json").json()

    assert "get" in spec["paths"]["/decisions"]
    assert "get" in spec["paths"]["/decisions/{decision_id}"]


def test_decisions_are_read_only(client):
    """A decision records something that already happened."""
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]["/decisions"]) == {"get"}
    assert set(spec["paths"]["/decisions/{decision_id}"]) == {"get"}
