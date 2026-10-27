"""Logging a session, and the one place a tested 1RM enters through the API.

The rule under test is ADR-0005 modification 5: a test single counts when it is
logged **at or above** the prescribed weight, and the new max is what was
actually lifted. Everything else -- a miss, a failure, a lift with no test --
records nothing, because a test that did not happen is not evidence of anything
(CLAUDE.md non-negotiable #9).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from tests.api.conftest import API_MAXES, propose


@pytest.fixture
def active(api_client, ready, athlete, template) -> dict:
    block_id, proposal_id = propose(api_client, athlete, template)
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200
    sessions = api_client.get(f"/blocks/{block_id}/sessions").json()
    return {"block_id": block_id, "sessions": sessions, "athlete": athlete}


def _test_single(sessions: list[dict], lift_code: str) -> tuple[dict, Decimal]:
    for session in sessions:
        for lift in session["lifts"]:
            if lift["lift_code"] == lift_code and lift["is_test"]:
                return session, Decimal(lift["weight_kg"])
    raise AssertionError(f"the authored block tests {lift_code}")


def _log(api_client, session: dict, **entry):
    body = {
        "performed_on": session["date"],
        "entries": [{"sets_done": 1, "reps_done": 1, **entry}],
    }
    return api_client.post(f"/sessions/{session['id']}/log", json=body)


def _current(api_client, athlete_id, lift_code) -> Decimal:
    detail = api_client.get(f"/athletes/{athlete_id}").json()
    row = next(r for r in detail["one_rep_maxes"] if r["lift_code"] == lift_code)
    return Decimal(row["kg"])


def test_a_logged_session_is_done_and_readable(api_client, active):
    session = active["sessions"][0]
    response = _log(
        api_client, session, lift_code="squat", weight_kg="75", sets_done=5, reps_done=5
    )
    assert response.status_code == 201, response.text

    detail = api_client.get(f"/sessions/{session['id']}").json()
    assert detail["status"] == "done"
    assert detail["log"]["performed_on"] == session["date"]
    assert Decimal(detail["log"]["entries"][0]["weight_kg"]) == Decimal("75")


def test_a_session_cannot_be_logged_twice(api_client, active):
    session = active["sessions"][0]
    assert _log(api_client, session, lift_code="squat", weight_kg="75").status_code == 201
    second = _log(api_client, session, lift_code="squat", weight_kg="80")
    assert second.status_code == 409
    assert "already logged" in second.json()["detail"]


def test_a_test_single_at_the_prescribed_weight_becomes_the_new_max(api_client, active):
    session, prescribed = _test_single(active["sessions"], "squat")
    response = _log(api_client, session, lift_code="squat", weight_kg=str(prescribed))
    assert response.status_code == 201, response.text
    assert _current(api_client, active["athlete"].id, "squat") == prescribed


def test_going_heavier_than_prescribed_counts_in_full(api_client, active):
    """The new max is what they lifted, not what the plan asked for."""
    session, prescribed = _test_single(active["sessions"], "squat")
    heavier = prescribed + Decimal("2.5")
    assert _log(api_client, session, lift_code="squat", weight_kg=str(heavier)).status_code == 201
    assert _current(api_client, active["athlete"].id, "squat") == heavier


def test_a_test_single_under_the_prescribed_weight_records_nothing(api_client, active):
    session, prescribed = _test_single(active["sessions"], "squat")
    assert (
        _log(
            api_client, session, lift_code="squat", weight_kg=str(prescribed - Decimal("5"))
        ).status_code
        == 201
    )
    # The old max carries over untouched: the safe direction.
    assert _current(api_client, active["athlete"].id, "squat") == API_MAXES["squat"]


def test_a_failed_test_single_records_nothing(api_client, active):
    """Logged at the weight but with no rep completed -- an attempt, not a max."""
    session, prescribed = _test_single(active["sessions"], "squat")
    assert (
        _log(
            api_client,
            session,
            lift_code="squat",
            weight_kg=str(prescribed),
            sets_done=1,
            reps_done=0,
        ).status_code
        == 201
    )
    assert _current(api_client, active["athlete"].id, "squat") == API_MAXES["squat"]


def test_a_working_set_never_becomes_a_max(api_client, active):
    """Only a session's `is_test` lift can produce one, however heavy the day."""
    session = active["sessions"][0]
    assert (
        _log(
            api_client, session, lift_code="squat", weight_kg="200", sets_done=5, reps_done=5
        ).status_code
        == 201
    )
    assert _current(api_client, active["athlete"].id, "squat") == API_MAXES["squat"]


def test_logging_needs_at_least_one_entry(api_client, active):
    session = active["sessions"][0]
    response = api_client.post(
        f"/sessions/{session['id']}/log",
        json={"performed_on": session["date"], "entries": []},
    )
    assert response.status_code == 422


def test_an_unknown_session_is_a_404(api_client):
    assert api_client.get(f"/sessions/{uuid.uuid4()}").status_code == 404
    response = api_client.post(
        f"/sessions/{uuid.uuid4()}/log",
        json={
            "performed_on": "2026-10-19",
            "entries": [{"lift_code": "squat", "sets_done": 1, "reps_done": 1, "weight_kg": "100"}],
        },
    )
    assert response.status_code == 404


# --- Spec 20 §10: the bounds the logger's inputs promise ------------------
#
# The form offers sets 0-10, reps 0-20, weight up to 500 kg and RPE 6-10, and the
# spec says the API mirrors them. It did not: the domain `LoggedLift` bounds only
# `sets_done >= 0` and `weight_kg > 0`, so a typo in a phone number field reached
# the database. These bound the WRITE path only -- a log already stored, and the
# simulator reading one back, are unaffected by a limit introduced later.


def _entry(**over) -> dict:
    return {"lift_code": "squat", "sets_done": 1, "reps_done": 1, "weight_kg": "100", **over}


def _post(api_client, session, entry, note=None) -> int:
    body = {"performed_on": session["date"], "entries": [entry]}
    if note is not None:
        body["note"] = note
    return api_client.post(f"/sessions/{session['id']}/log", json=body).status_code


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_entry(sets_done=11), id="more sets than the form offers"),
        pytest.param(_entry(sets_done=-1), id="negative sets"),
        pytest.param(_entry(reps_done=21), id="more reps than the form offers"),
        pytest.param(_entry(reps_done=-1), id="negative reps"),
        pytest.param(_entry(reps_done=[5, 21]), id="a per-set list with one over"),
        pytest.param(_entry(reps_done=[]), id="an empty per-set list"),
        pytest.param(_entry(weight_kg="500.1"), id="heavier than any plate loaded here"),
        pytest.param(_entry(weight_kg="0"), id="no weight at all"),
        pytest.param(_entry(rpe="5.5"), id="an RPE below the scale"),
        pytest.param(_entry(rpe="10.5"), id="an RPE above the scale"),
    ],
)
def test_a_log_outside_the_form_s_own_bounds_is_refused(api_client, active, entry):
    assert _post(api_client, active["sessions"][0], entry) == 422


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_entry(sets_done=0, reps_done=0), id="attempted and not completed"),
        pytest.param(_entry(sets_done=10, reps_done=20), id="the top of each range"),
        pytest.param(_entry(reps_done=[5, 4, 3]), id="a per-set list"),
        pytest.param(_entry(weight_kg="500"), id="exactly the ceiling"),
        pytest.param(_entry(rpe="6"), id="the bottom of the RPE scale"),
        pytest.param(_entry(rpe="10"), id="the top of the RPE scale"),
        pytest.param(_entry(rpe="8.5"), id="a half point, which lifters do use"),
    ],
)
def test_a_log_inside_them_is_accepted(api_client, active, entry):
    assert _post(api_client, active["sessions"][0], entry) == 201


def test_a_note_longer_than_the_form_allows_is_refused(api_client, active):
    assert _post(api_client, active["sessions"][0], _entry(), note="x" * 2001) == 422


def test_a_note_at_the_limit_is_kept(api_client, active):
    assert _post(api_client, active["sessions"][0], _entry(), note="x" * 2000) == 201
