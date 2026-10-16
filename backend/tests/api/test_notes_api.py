"""The two notes routes, end to end against real Postgres.

`api_client` overrides `get_embedder` with the fake (see `tests/conftest.py`),
so these exercise the whole path -- route, service, SQL, pgvector -- without a
model anywhere. The two 422s are FastAPI's own validation, which is the point:
spec 12 §13's bounds are declared on the query parameters rather than
hand-checked in the body of the route.
"""

from __future__ import annotations

import uuid

import pytest

from tests.api.conftest import propose


@pytest.fixture
def logged_note(api_client, ready, athlete, template) -> dict:
    """An approved block with one session logged, carrying a knee note."""
    block_id, proposal_id = propose(api_client, athlete, template)
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200
    sessions = api_client.get(f"/blocks/{block_id}/sessions").json()
    first = sessions[0]
    response = api_client.post(
        f"/sessions/{first['id']}/log",
        json={
            "performed_on": first["date"],
            "entries": [
                {
                    "lift_code": lift["lift_code"],
                    "sets_done": lift["sets"],
                    "reps_done": lift["reps"],
                    "weight_kg": lift["weight_kg"],
                }
                for lift in first["lifts"]
            ],
            "note": "Left knee felt tight at the bottom of squats",
        },
    )
    assert response.status_code == 201, response.text
    return {"athlete_id": athlete.id, "block_id": block_id, "session": first}


def test_a_logged_note_is_searchable(api_client, logged_note):
    response = api_client.get(
        f"/athletes/{logged_note['athlete_id']}/notes/search",
        params={"q": "how has my knee been?"},
    )

    assert response.status_code == 200, response.text
    hits = response.json()
    assert hits
    assert "knee" in hits[0]["text"]


def test_a_hit_carries_everything_the_ui_needs(api_client, logged_note):
    """`is_synthetic` included: ADR-0004 requires the UI to be able to say so."""
    response = api_client.get(
        f"/athletes/{logged_note['athlete_id']}/notes/search", params={"q": "knee"}
    )

    hit = response.json()[0]
    assert set(hit) == {"text", "noted_on", "lift_codes", "distance", "is_synthetic"}
    assert hit["lift_codes"] == ["squat"]
    assert hit["is_synthetic"] is False
    assert 0.0 <= hit["distance"] <= 2.0


def test_the_lift_filter_is_applied(api_client, logged_note):
    athlete_id = logged_note["athlete_id"]

    squat = api_client.get(
        f"/athletes/{athlete_id}/notes/search", params={"q": "knee", "lift": "squat"}
    )
    row = api_client.get(
        f"/athletes/{athlete_id}/notes/search", params={"q": "knee", "lift": "row"}
    )

    assert squat.json()
    assert row.json() == []


def test_an_unknown_lift_is_refused(api_client, logged_note):
    """The query parameter is typed `LiftCode`, so FastAPI rejects it."""
    response = api_client.get(
        f"/athletes/{logged_note['athlete_id']}/notes/search",
        params={"q": "knee", "lift": "bananas"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("q", ["", "   "])
def test_an_empty_query_is_422(api_client, logged_note, q):
    response = api_client.get(
        f"/athletes/{logged_note['athlete_id']}/notes/search", params={"q": q}
    )

    assert response.status_code == 422


def test_the_search_query_constraint_is_actually_attached():
    """Guards a trap that cost a 500 here.

    `q: SearchQuery = Query(...)` type-checks, reads naturally and silently
    drops the `StringConstraints` -- a blank query then reaches the service and
    raises. Only `Annotated[SearchQuery, Query(...)]` applies it. This asserts
    the constraint is in the generated schema, so the signature cannot drift
    back to the form that looks identical and validates nothing.
    """
    from api.main import app

    for path, method in (("/athletes/{athlete_id}/notes/search", "get"),):
        operation = app.openapi()["paths"][path][method]
        q = next(p for p in operation["parameters"] if p["name"] == "q")
        assert q["schema"]["minLength"] == 1
        assert q["required"] is True


@pytest.mark.parametrize("k", [0, -1, 21, 100])
def test_a_k_outside_one_to_twenty_is_422(api_client, logged_note, k):
    response = api_client.get(
        f"/athletes/{logged_note['athlete_id']}/notes/search",
        params={"q": "knee", "k": k},
    )

    assert response.status_code == 422


def test_k_bounds_the_results(api_client, logged_note):
    response = api_client.get(
        f"/athletes/{logged_note['athlete_id']}/notes/search",
        params={"q": "knee", "k": 1},
    )

    assert len(response.json()) <= 1


def test_searching_an_unknown_athlete_is_404(api_client, db_session):
    """Not an empty list: that would read as "no notes" and send the caller
    looking in the wrong place."""
    response = api_client.get(f"/athletes/{uuid.uuid4()}/notes/search", params={"q": "knee"})

    assert response.status_code == 404
    assert "detail" in response.json()


def test_reindexing_an_unknown_athlete_is_404(api_client, db_session):
    response = api_client.post(f"/athletes/{uuid.uuid4()}/notes/reindex")

    assert response.status_code == 404


def test_reindex_reports_how_many_chunks_exist(api_client, logged_note):
    response = api_client.post(f"/athletes/{logged_note['athlete_id']}/notes/reindex")

    assert response.status_code == 200, response.text
    assert response.json() == {"reindexed": 1}


def test_reindex_is_idempotent(api_client, logged_note):
    """Running it twice must not duplicate a note or return it twice."""
    athlete_id = logged_note["athlete_id"]
    first = api_client.post(f"/athletes/{athlete_id}/notes/reindex").json()
    second = api_client.post(f"/athletes/{athlete_id}/notes/reindex").json()

    assert first == second

    hits = api_client.get(
        f"/athletes/{athlete_id}/notes/search", params={"q": "knee", "k": 20}
    ).json()
    assert len(hits) == 1


def test_a_note_still_searchable_after_reindex(api_client, logged_note):
    athlete_id = logged_note["athlete_id"]
    api_client.post(f"/athletes/{athlete_id}/notes/reindex")

    hits = api_client.get(f"/athletes/{athlete_id}/notes/search", params={"q": "knee"}).json()

    assert hits
    assert "knee" in hits[0]["text"]


def test_a_log_with_no_note_indexes_nothing(api_client, ready, athlete, template):
    block_id, proposal_id = propose(api_client, athlete, template)
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200
    sessions = api_client.get(f"/blocks/{block_id}/sessions").json()
    first = sessions[0]

    api_client.post(
        f"/sessions/{first['id']}/log",
        json={"performed_on": first["date"], "entries": []},
    )

    hits = api_client.get(f"/athletes/{athlete.id}/notes/search", params={"q": "anything"}).json()
    assert hits == []


def test_synthetic_notes_are_hidden_unless_asked_for(api_client, db_session, athlete):
    """The route's default must match the service's (ADR-0004 rule 1)."""
    from domains.strength import models
    from tests.fakes import FakeEmbedder

    db_session.add(
        models.NoteChunk(
            athlete_id=athlete.id,
            lift_codes=["squat"],
            text="seeded note about squats",
            noted_on=ready_date(),
            embedding=FakeEmbedder().embed_documents(["seeded note about squats"])[0],
            is_synthetic=True,
        )
    )
    db_session.flush()

    hidden = api_client.get(f"/athletes/{athlete.id}/notes/search", params={"q": "squats"}).json()
    shown = api_client.get(
        f"/athletes/{athlete.id}/notes/search",
        params={"q": "squats", "include_synthetic": "true"},
    ).json()

    assert hidden == []
    assert [hit["is_synthetic"] for hit in shown] == [True]


def ready_date():
    import datetime as dt

    return dt.date.today()
