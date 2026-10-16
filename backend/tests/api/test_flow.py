"""Spec 06 §12 end to end: maxes -> days -> block -> approve -> log.

This is the spec's Definition of Done in one test, and the claim it makes is
the one that matters after week 1: the app is usable with no AI at all.

The template comes from the committed golden fixture rather than the gitignored
workbook, so this runs on a fresh clone -- the same reasoning ADR-0005
modification 15 applied to the generator suite. `test_templates.py` covers the
upload itself, and skips when the workbook is absent.

Spec §12 words the last step as "logging squat W11 Day 1 at 151 kg x 1 -> new
squat 1RM 151 kg". Those numbers come from the spreadsheet's example maxes
(151 derives from 140), which CLAUDE.md and ADR-0004 bar from tests. The step
is asserted on this suite's own maxes instead, with the prescribed weight read
back from the API rather than written down -- stronger anyway, since a test
that hard-codes the number it expects cannot notice the generator changing it.
"""

from __future__ import annotations

from decimal import Decimal

from tests.api.conftest import ALL_LIFTS, API_MAXES, MON_WED_FRI, START, TESTED_ON


def test_the_whole_week_one_journey(api_client, athlete, template):
    """From an athlete with nothing to a logged test single and a new 1RM."""
    maxes = [
        {
            "lift_code": lift,
            "kg": str(kg),
            "set_on": TESTED_ON.isoformat(),
            "source": "tested",
        }
        for lift, kg in sorted(API_MAXES.items())
    ]
    response = api_client.put(f"/athletes/{athlete.id}/one-rep-maxes", json=maxes)
    assert response.status_code == 200, response.text
    assert all(not row["is_synthetic"] for row in response.json())

    response = api_client.put(
        f"/athletes/{athlete.id}/weekday-map",
        json={"slots": {str(slot): weekday for slot, weekday in MON_WED_FRI.items()}},
    )
    assert response.status_code == 200, response.text
    assert [row["weekday"] for row in response.json()["slots"]] == [0, 2, 4]
    assert all(row["locked_from"] is None for row in response.json()["slots"])

    body = {
        "athlete_id": str(athlete.id),
        "template_id": str(template.id),
        "lifts": ALL_LIFTS,
        "start_date": START.isoformat(),
    }
    response = api_client.post("/blocks/preview", json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert len(preview["draft"]["sessions"]) == 33
    assert preview["validation"]["ok"] is True
    # Real maxes in, so nothing downstream is development data.
    assert preview["has_synthetic_one_rms"] is False

    response = api_client.post("/blocks", json=body)
    assert response.status_code == 201, response.text
    block_id = response.json()["block_id"]
    proposal_id = response.json()["proposal"]["id"]
    assert response.json()["proposal"]["status"] == "pending"

    # The approval gate: a pending proposal has written no schedule at all.
    assert api_client.get(f"/blocks/{block_id}/sessions").json() == []
    assert api_client.get(f"/blocks/{block_id}").json()["status"] == "draft"

    response = api_client.post(f"/proposals/{proposal_id}/approve")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"

    sessions = api_client.get(f"/blocks/{block_id}/sessions").json()
    assert len(sessions) == 33
    assert api_client.get(f"/blocks/{block_id}").json()["status"] == "active"
    assert api_client.get(f"/athletes/{athlete.id}/blocks/current").json()["id"] == block_id

    # Approved, so the days this block uses are pinned from its start.
    detail = api_client.get(f"/athletes/{athlete.id}").json()
    assert {row["locked_from"] for row in detail["weekday_map"]} == {START.isoformat()}

    # Second approve on the same proposal -> 409 (spec §12).
    assert api_client.post(f"/proposals/{proposal_id}/approve").status_code == 409

    # The squat's test single, found by `is_test` rather than by week number.
    test_session, prescribed = _squat_test(sessions)
    assert (test_session["week"], test_session["slot"]) == (11, 1)

    # A kilo over the prescription: "completed at the prescribed weight" means
    # at or above, and the new max is what they actually lifted (ADR-0005
    # modification 5).
    lifted = prescribed + Decimal("1")
    response = api_client.post(
        f"/sessions/{test_session['id']}/log",
        json={
            "performed_on": test_session["date"],
            "entries": [
                {
                    "lift_code": "squat",
                    "sets_done": 1,
                    "reps_done": 1,
                    "weight_kg": str(lifted),
                }
            ],
            "note": "felt strong",
        },
    )
    assert response.status_code == 201, response.text
    assert api_client.get(f"/sessions/{test_session['id']}").json()["status"] == "done"

    detail = api_client.get(f"/athletes/{athlete.id}").json()
    current = {row["lift_code"]: row for row in detail["one_rep_maxes"]}
    assert Decimal(current["squat"]["kg"]) == lifted
    assert current["squat"]["source"] == "tested"
    assert current["squat"]["is_synthetic"] is False
    assert detail["has_synthetic_one_rms"] is False
    # A squat test says nothing about the other lifts.
    assert Decimal(current["bench"]["kg"]) == API_MAXES["bench"]

    history = api_client.get(
        f"/athletes/{athlete.id}/one-rep-maxes/history", params={"lift": "squat"}
    ).json()
    assert [Decimal(row["kg"]) for row in history] == [lifted, API_MAXES["squat"]]
    # The new max is attributed to the block it was tested in.
    assert history[0]["block_id"] == block_id


def _squat_test(sessions: list[dict]) -> tuple[dict, Decimal]:
    for session in sessions:
        for lift in session["lifts"]:
            if lift["lift_code"] == "squat" and lift["is_test"]:
                return session, Decimal(lift["weight_kg"])
    raise AssertionError("the authored block has a squat test single")
