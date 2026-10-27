"""The athlete, their maxes and their training days.

The 1RM tests here are the HTTP half of ADR-0004: the API is the boundary where
rule 3's single-writer convention stops being enforceable by convention, so
these assert that it holds by construction instead.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from domains.strength import models
from tests.api.conftest import API_MAXES, MON_WED_FRI, TESTED_ON


def test_a_second_post_returns_the_athlete_that_exists(api_client):
    """Get-or-create: single-athlete is a decision, and the wizard is re-runnable."""
    first = api_client.post("/athletes", json={"name": "Basil"})
    assert first.status_code == 201, first.text

    second = api_client.post("/athletes", json={"name": "Someone Else"})
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["name"] == "Basil"


def test_an_athlete_defaults_to_loadable_plates_and_berlin(api_client):
    """5 kg steps, rounded to nearest -- what a paired-plate bar can take."""
    body = api_client.post("/athletes", json={"name": "Basil"}).json()
    assert Decimal(body["plate_increment_kg"]) == Decimal("5.00")
    assert body["rounding"] == "nearest"
    assert body["timezone"] == "Europe/Berlin"


def test_patching_changes_only_what_was_sent(api_client, athlete):
    """Both values differ from the fixture's 1.0/floor, so a no-op cannot pass."""
    response = api_client.patch(
        f"/athletes/{athlete.id}", json={"plate_increment_kg": "1.25", "rounding": "nearest"}
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["plate_increment_kg"]) == Decimal("1.25")
    assert response.json()["rounding"] == "nearest"
    assert response.json()["timezone"] == "Europe/Berlin"


def test_an_unknown_athlete_is_a_404(api_client):
    response = api_client.get(f"/athletes/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "athlete" in response.json()["detail"]


# --- 1RMs -----------------------------------------------------------------


def test_a_max_is_recorded_with_its_date_and_source(api_client, athlete):
    response = api_client.put(
        f"/athletes/{athlete.id}/one-rep-maxes",
        json=[
            {
                "lift_code": "squat",
                "kg": "125",
                "set_on": TESTED_ON.isoformat(),
                "source": "tested",
            }
        ],
    )
    assert response.status_code == 200, response.text
    (row,) = response.json()
    assert Decimal(row["kg"]) == Decimal("125")
    assert row["set_on"] == TESTED_ON.isoformat()
    assert row["source"] == "tested"
    assert row["is_synthetic"] is False


def test_a_client_cannot_claim_a_max_is_synthetic(api_client, athlete):
    """ADR-0004 rule 3, enforced by the type rather than by a check.

    `synthetic` is not in `ClientOneRepMaxSource`, so Pydantic refuses it
    before any code runs -- there is no branch to forget.
    """
    response = api_client.put(
        f"/athletes/{athlete.id}/one-rep-maxes",
        json=[
            {
                "lift_code": "squat",
                "kg": "125",
                "set_on": TESTED_ON.isoformat(),
                "source": "synthetic",
            }
        ],
    )
    assert response.status_code == 422


def test_a_client_cannot_set_the_synthetic_flag(api_client, athlete):
    """`is_synthetic` is not in the request model at all, so it is rejected."""
    response = api_client.put(
        f"/athletes/{athlete.id}/one-rep-maxes",
        json=[
            {
                "lift_code": "squat",
                "kg": "125",
                "set_on": TESTED_ON.isoformat(),
                "source": "tested",
                "is_synthetic": True,
            }
        ],
    )
    assert response.status_code == 422


def test_a_max_cannot_have_been_measured_tomorrow(api_client, athlete):
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    response = api_client.put(
        f"/athletes/{athlete.id}/one-rep-maxes",
        json=[{"lift_code": "squat", "kg": "125", "set_on": tomorrow.isoformat()}],
    )
    assert response.status_code == 422
    assert "after today" in response.json()["detail"]


def test_the_synthetic_flag_is_surfaced_wherever_a_max_is(api_client, db_session, athlete):
    """ADR-0004 rule 4: the UI must be able to see that a number is invented."""
    db_session.add(
        models.OneRepMax(
            athlete_id=athlete.id,
            lift_code="squat",
            kg=Decimal("130"),
            set_on=TESTED_ON,
            source="synthetic",
            is_synthetic=True,
        )
    )
    db_session.flush()

    detail = api_client.get(f"/athletes/{athlete.id}").json()
    assert detail["has_synthetic_one_rms"] is True
    assert detail["one_rep_maxes"][0]["is_synthetic"] is True


def test_the_newest_max_per_lift_is_the_current_one(api_client, athlete, maxes):
    later = dt.date.today() - dt.timedelta(days=1)
    api_client.put(
        f"/athletes/{athlete.id}/one-rep-maxes",
        json=[{"lift_code": "squat", "kg": "130", "set_on": later.isoformat()}],
    )

    detail = api_client.get(f"/athletes/{athlete.id}").json()
    current = {row["lift_code"]: Decimal(row["kg"]) for row in detail["one_rep_maxes"]}
    assert current["squat"] == Decimal("130")
    # The older row is still there; nothing was overwritten.
    history = api_client.get(
        f"/athletes/{athlete.id}/one-rep-maxes/history", params={"lift": "squat"}
    ).json()
    assert [Decimal(row["kg"]) for row in history] == [Decimal("130"), API_MAXES["squat"]]


# --- Weekday map ----------------------------------------------------------


def test_a_dry_run_reports_problems_and_writes_nothing(api_client, athlete, template):
    # Friday, Wednesday, Monday: the slots run backwards through the week.
    response = api_client.put(
        f"/athletes/{athlete.id}/weekday-map",
        params={"dry_run": "true"},
        json={"slots": {"1": 4, "2": 2, "3": 0}},
    )
    assert response.status_code == 200, response.text
    assert "must come after" in " ".join(response.json()["problems"])
    assert api_client.get(f"/athletes/{athlete.id}").json()["weekday_map"] == []


def test_a_real_put_with_problems_is_a_422(api_client, athlete, template):
    """Two slots on one Monday: caught here, and also by the unique index."""
    response = api_client.put(
        f"/athletes/{athlete.id}/weekday-map", json={"slots": {"1": 0, "2": 0, "3": 4}}
    )
    assert response.status_code == 422
    assert "both on Mon" in " ".join(response.json()["problems"])
    # Refused before anything was written, so the athlete still has no days.
    assert api_client.get(f"/athletes/{athlete.id}").json()["weekday_map"] == []


def test_the_map_can_be_swapped_without_tripping_the_unique_weekday(api_client, athlete, template):
    """Delete-then-insert, because the constraint is not deferrable."""
    first = api_client.put(
        f"/athletes/{athlete.id}/weekday-map",
        json={"slots": {str(slot): day for slot, day in MON_WED_FRI.items()}},
    )
    assert first.status_code == 200, first.text

    # Tue / Thu / Sat: every slot takes a weekday one of the others just left.
    second = api_client.put(
        f"/athletes/{athlete.id}/weekday-map", json={"slots": {"1": 1, "2": 3, "3": 5}}
    )
    assert second.status_code == 200, second.text
    assert [row["weekday"] for row in second.json()["slots"]] == [1, 3, 5]


def test_the_map_is_locked_while_a_block_is_running(api_client, ready, athlete, template):
    from tests.api.conftest import propose

    _, proposal_id = propose(api_client, athlete, template)
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 200

    response = api_client.put(
        f"/athletes/{athlete.id}/weekday-map", json={"slots": {"1": 1, "2": 3, "3": 5}}
    )
    assert response.status_code == 409
    assert "weekday_change proposal" in response.json()["detail"]


# --- listing --------------------------------------------------------------


def test_listing_athletes_on_a_fresh_database_is_empty_not_an_error(api_client):
    """How spec 16's provider learns to send the user to the setup wizard."""
    response = api_client.get("/athletes")
    assert response.status_code == 200
    assert response.json() == []


def test_listing_athletes_returns_the_one_that_exists(api_client):
    created = api_client.post("/athletes", json={"name": "Basil"}).json()
    listed = api_client.get("/athletes").json()
    assert [row["id"] for row in listed] == [created["id"]]
    assert listed[0]["name"] == "Basil"
    # Money-shaped values stay strings on the wire (ADR-0007 Negative 6).
    assert isinstance(listed[0]["plate_increment_kg"], str)


def test_the_first_listed_athlete_is_the_oldest(api_client, db_session):
    """`create_athlete` and the list agree on which athlete 'the' athlete is."""
    older = models.Athlete(name="Older")
    db_session.add(older)
    db_session.flush()
    newer = models.Athlete(name="Newer")
    newer.created_at = older.created_at + dt.timedelta(seconds=1)
    db_session.add(newer)
    db_session.flush()

    listed = api_client.get("/athletes").json()
    assert [row["name"] for row in listed] == ["Older", "Newer"]
    assert api_client.post("/athletes", json={"name": "Third"}).json()["id"] == listed[0]["id"]
