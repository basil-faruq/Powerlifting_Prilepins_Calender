"""`POST /chat` and the conversation history, through the real app.

The dispatch tests cover what a turn does. These cover what the *route* adds:
the status codes, the response shape, and the one ordering that is easy to get
wrong -- the commit that has to happen before the 503, so the decision row
recording the failure survives it.

Needs Postgres.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import models, services
from domains.strength.agent.schemas import ChatModels, RouterChoice
from tests.api.conftest import propose
from tests.fakes import RefusingModel, replying_model, router_model


def _models(router, agent=None) -> ChatModels:
    return ChatModels(router=router, agent=agent or replying_model("Noted."))


@pytest.fixture
def active(api_client, db_session: Session, ready) -> models.Block:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    return services.get_block(db_session, block_id)


def _next_monday(after: dt.date) -> dt.date:
    return after + dt.timedelta(days=(7 - after.weekday()) % 7 or 7)


def _post(client, athlete, message: str, **extra) -> object:
    body = {"athlete_id": str(athlete.id), "message": message}
    body.update(extra)
    return client.post("/chat", json=body)


# --- The happy paths ----------------------------------------------------


def test_a_pain_message_answers_200_with_no_model(chat_client, ready):
    """The models refuse on use, so a 200 here proves neither was called."""
    response = _post(chat_client, ready["athlete"], "my knee hurts on squats")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route"]["kind"] == "safety"
    assert body["route"]["filter_fired"] == "pain_keyword"
    assert body["proposals"] == []
    assert body["simulation"] is None
    assert body["decision_id"]
    assert body["conversation_id"]


def test_the_response_carries_the_synthetic_flag(chat_client, ready):
    """ADR-0007 rule 5 / ADR-0004 rule 4. Spec 13 §3 omits it (ADR-0015)."""
    response = _post(chat_client, ready["athlete"], "my knee hurts")

    assert "has_synthetic_one_rms" in response.json()


def test_the_trace_url_is_null_with_tracing_off(chat_client, ready):
    """Spec 14 §12: without keys everything works and the link is absent."""
    response = _post(chat_client, ready["athlete"], "my knee hurts")

    assert response.json()["trace_url"] is None


@pytest.mark.parametrize(
    "chat_client",
    [None],
    indirect=True,
)
def test_a_plan_turn_returns_a_pending_proposal(chat_client, db_session, ready, active):
    """The proposal comes back `pending`; approving is a separate route."""
    from api.deps import get_chat_models
    from api.main import app

    monday = _next_monday(services.today_for(ready["athlete"]))
    app.dependency_overrides[get_chat_models] = lambda: _models(
        router_model(RouterChoice(kind="plan", lifts=["squat", "bench"], start_date=monday)),
        replying_model("I've prepared a block for you to approve."),
    )

    response = _post(
        chat_client,
        ready["athlete"],
        "new block with squat and bench on Monday",
        template_id=str(ready["template"].id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route"]["kind"] == "plan"
    assert len(body["proposals"]) == 1
    assert body["proposals"][0]["status"] == "pending"


def test_a_simulate_turn_returns_percentiles(chat_client, db_session, ready, active):
    from api.deps import get_chat_models
    from api.main import app

    app.dependency_overrides[get_chat_models] = lambda: _models(
        router_model(
            RouterChoice(
                kind="simulate",
                scenario_kind="illness",
                scenario_start_week=6,
                scenario_days=10,
            )
        ),
        replying_model("It should still finish."),
    )

    response = _post(chat_client, ready["athlete"], "what if I'm sick for 10 days in week 6?")

    assert response.status_code == 200, response.text
    simulation = response.json()["simulation"]
    assert simulation is not None
    assert simulation["block_id"] == str(active.id)
    assert simulation["assumptions"]
    assert simulation["missed_sessions"]["p10"] <= simulation["missed_sessions"]["p90"]


def test_the_turn_is_committed(chat_client, db_session, ready):
    """The route owns the transaction; the service only flushes."""
    response = _post(chat_client, ready["athlete"], "my knee hurts")
    decision_id = uuid.UUID(response.json()["decision_id"])

    assert db_session.get(models.Decision, decision_id) is not None


# --- Failures -----------------------------------------------------------


def test_an_unknown_athlete_is_404(chat_client):
    response = chat_client.post("/chat", json={"athlete_id": str(uuid.uuid4()), "message": "hello"})

    assert response.status_code == 404
    assert "detail" in response.json()


def test_a_blank_message_is_fastapis_own_422(chat_client, ready):
    """`ChatText` strips before the length check, so spaces are as empty as ""
    -- the trap `routes/notes.py` documents."""
    response = _post(chat_client, ready["athlete"], "   ")

    assert response.status_code == 422


def test_an_unknown_field_is_refused(chat_client, ready):
    response = chat_client.post(
        "/chat",
        json={"athlete_id": str(ready["athlete"].id), "message": "hi", "approve": True},
    )

    assert response.status_code == 422


def test_a_dead_model_is_503_and_the_decision_row_survives_it(
    chat_client, db_session, ready, active
):
    """The crux of the whole write path.

    `handle_message` may not commit (ADR-0007 rule 2), so it hands the failure
    back and the route commits *before* raising. If that order were reversed
    the row would be rolled back on its way to the handler, and spec 13's DoD
    -- every turn writes a decision -- would be quietly false exactly when the
    trail matters most.
    """
    before = len(list(db_session.scalars(sa.select(models.Decision))))

    response = _post(chat_client, ready["athlete"], "plan me a new block please")

    assert response.status_code == 503, response.text
    assert "detail" in response.json()

    rows = list(db_session.scalars(sa.select(models.Decision)))
    assert len(rows) == before + 1
    newest = max(rows, key=lambda row: row.created_at)
    assert newest.outcome == "failed"
    assert newest.route == "clarify"


def test_a_borrowed_conversation_id_is_404(chat_client, db_session, ready):
    """`conversation_id` is not a foreign key, so any UUID is valid syntax."""
    other = models.Athlete(name="someone-else", plate_increment_kg=1, rounding="floor")
    db_session.add(other)
    db_session.flush()
    theirs = uuid.uuid4()
    db_session.add(
        models.ChatMessage(
            conversation_id=theirs, athlete_id=other.id, role="user", content="private"
        )
    )
    db_session.flush()

    response = _post(chat_client, ready["athlete"], "what did I say?", conversation_id=str(theirs))

    assert response.status_code == 404


# --- Conversation history ------------------------------------------------


def test_the_history_returns_both_messages_in_order(chat_client, ready):
    first = _post(chat_client, ready["athlete"], "my knee hurts").json()

    response = chat_client.get(
        f"/conversations/{first['conversation_id']}/messages",
        params={"athlete_id": str(ready["athlete"].id)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["role"] for row in body] == ["user", "assistant"]
    assert body[0]["content"] == "my knee hurts"
    assert body[0]["decision_id"] == first["decision_id"]


def test_a_second_turn_continues_the_conversation(chat_client, ready):
    first = _post(chat_client, ready["athlete"], "my knee hurts").json()

    # Also a pain message, so this turn stays on layer 1 too -- "my elbow too"
    # carries no pain stem and would correctly fall through to the model.
    raw = _post(
        chat_client,
        ready["athlete"],
        "my elbow hurts too",
        conversation_id=first["conversation_id"],
    )
    assert raw.status_code == 200, raw.text
    second = raw.json()

    assert second["conversation_id"] == first["conversation_id"]
    assert second["decision_id"] != first["decision_id"]

    history = chat_client.get(
        f"/conversations/{first['conversation_id']}/messages",
        params={"athlete_id": str(ready["athlete"].id)},
    ).json()
    assert len(history) == 4


def test_the_history_requires_an_athlete_id(chat_client, ready):
    """Required rather than optional: without it a guessed conversation id
    reads back whatever that conversation holds."""
    response = chat_client.get(f"/conversations/{uuid.uuid4()}/messages")

    assert response.status_code == 422


def test_the_history_of_someone_elses_conversation_is_404(chat_client, db_session, ready):
    other = models.Athlete(name="someone-else-2", plate_increment_kg=1, rounding="floor")
    db_session.add(other)
    db_session.flush()
    theirs = uuid.uuid4()
    db_session.add(
        models.ChatMessage(
            conversation_id=theirs, athlete_id=other.id, role="user", content="private"
        )
    )
    db_session.flush()

    response = chat_client.get(
        f"/conversations/{theirs}/messages",
        params={"athlete_id": str(ready["athlete"].id)},
    )

    assert response.status_code == 404


def test_an_unknown_conversation_is_an_empty_list(chat_client, ready):
    """Unknown and empty are the same thing here: there is no conversation
    table, so nothing distinguishes "never existed" from "no messages"."""
    response = chat_client.get(
        f"/conversations/{uuid.uuid4()}/messages",
        params={"athlete_id": str(ready["athlete"].id)},
    )

    assert response.status_code == 200
    assert response.json() == []


# --- The route is documented --------------------------------------------


def test_chat_is_in_the_openapi_schema(client):
    spec = client.get("/openapi.json").json()

    assert "post" in spec["paths"]["/chat"]
    assert "get" in spec["paths"]["/conversations/{conversation_id}/messages"]


def test_the_agent_has_no_approval_route(client):
    """Spec 13 §11: the agent cannot approve; only the user can.

    `POST /proposals/{id}/approve` stays the single write path (ADR-0007 rule
    1), and nothing under /chat shadows it.
    """
    spec = client.get("/openapi.json").json()
    chat_paths = [path for path in spec["paths"] if path.startswith("/chat")]

    assert chat_paths == ["/chat"]
    assert "post" in spec["paths"]["/proposals/{proposal_id}/approve"]


def test_refusing_model_is_actually_refusing(chat_client, ready, active):
    """Guards the guard: the no-LLM assertions above rest on this raising."""
    import anyio
    from pydantic_ai.exceptions import ModelAPIError

    model = RefusingModel()
    with pytest.raises(ModelAPIError):
        anyio.run(lambda: model.request())
