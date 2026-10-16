"""`POST /blocks/{id}/simulate`: the database half of spec 10.

`test_simulator.py` proves the simulation itself in memory. What is left is what
only exists once rows do: reading a running block back, the status gate, the
request bounds, and the one flag every plan-derived response must carry.

The route writes nothing, so the strongest assertion here is the last one: the
block, its sessions and the athlete's maxes are identical afterwards.

These need Postgres and skip cleanly without it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import models, services
from tests.api.conftest import ALL_LIFTS, API_MAXES, TESTED_ON, propose

# Fewer than the default so the suite stays quick. The statistics are tested
# in `test_simulator.py`; this file tests the wiring.
RUNS = 40


@pytest.fixture
def active(api_client, db_session: Session, ready) -> uuid.UUID:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    return block_id


def simulate(api_client, block_id, **body):
    return api_client.post(f"/blocks/{block_id}/simulate", json={"runs": RUNS, **body})


def test_a_running_block_returns_ranges_and_assumptions(api_client, active):
    response = simulate(api_client, active, seed=3)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["block_id"] == str(active)
    assert body["finish_date"]["p10"] <= body["finish_date"]["p50"] <= body["finish_date"]["p90"]
    assert set(body["per_lift"]) == set(ALL_LIFTS)
    assert body["assumptions"]
    assert body["stats"]["runs"] == RUNS
    assert body["stats"]["seed"] == 3


def test_the_same_request_is_the_same_answer(api_client, active):
    first = simulate(api_client, active, seed=9).json()
    second = simulate(api_client, active, seed=9).json()

    first["stats"].pop("wall_time_s")
    second["stats"].pop("wall_time_s")
    assert first == second


def test_a_paused_block_can_be_simulated(api_client, db_session: Session, active):
    """ADR-0010: paused counts as running."""
    block = services.get_block(db_session, active)
    block.status = "paused"
    db_session.add(block)
    db_session.flush()

    assert simulate(api_client, active).status_code == 200


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_a_finished_block_is_a_409(api_client, db_session: Session, active, status):
    block = services.get_block(db_session, active)
    block.status = status
    db_session.add(block)
    db_session.flush()

    response = simulate(api_client, active)
    assert response.status_code == 409
    assert status in response.json()["detail"]


def test_a_draft_block_is_a_409_not_a_422_about_missing_sessions(api_client, ready):
    """A proposed block has no sessions yet. 'Not running' is the cause."""
    block_id, _ = propose(api_client, ready["athlete"], ready["template"])

    response = simulate(api_client, block_id)
    assert response.status_code == 409
    assert "draft" in response.json()["detail"]


def test_an_unknown_block_is_a_404(api_client):
    assert simulate(api_client, uuid.uuid4()).status_code == 404


def test_more_than_five_thousand_runs_is_a_422(api_client, active):
    assert simulate(api_client, active, runs=5001).status_code == 422
    assert simulate(api_client, active, runs=0).status_code == 422


def test_a_forced_event_past_the_end_of_the_block_is_a_422(api_client, active):
    response = simulate(
        api_client,
        active,
        scenario=[{"kind": "illness", "start_week": 12, "days": 3}],
    )
    assert response.status_code == 422
    assert "past the block" in response.json()["detail"]


def test_a_forced_event_with_two_starts_is_a_422(api_client, active):
    response = simulate(
        api_client,
        active,
        scenario=[{"kind": "illness", "start_week": 2, "start_date": "2030-01-01", "days": 3}],
    )
    assert response.status_code == 422


def test_a_forced_illness_is_reflected_in_the_assumptions(api_client, active):
    response = simulate(
        api_client, active, scenario=[{"kind": "illness", "start_week": 6, "days": 10}]
    )

    assert response.status_code == 200, response.text
    assert any("Forced in every run: illness" in a for a in response.json()["assumptions"])


def test_real_maxes_do_not_raise_the_synthetic_flag(api_client, active):
    assert simulate(api_client, active).json()["has_synthetic_one_rms"] is False


def test_a_projection_built_on_a_synthetic_max_says_so(
    api_client, db_session: Session, ready, active
):
    """ADR-0004 rule 4 / ADR-0007 rule 5. The flag follows the block's own
    snapshot: a synthetic row carrying the max the block was planned from."""
    db_session.add(
        models.OneRepMax(
            athlete_id=ready["athlete"].id,
            lift_code="squat",
            kg=API_MAXES["squat"],
            set_on=TESTED_ON + dt.timedelta(days=1),
            source="synthetic",
            is_synthetic=True,
        )
    )
    db_session.flush()

    assert simulate(api_client, active).json()["has_synthetic_one_rms"] is True


def test_a_dev_seed_run_after_the_block_was_planned_does_not_taint_it(
    api_client, db_session: Session, ready, active
):
    """The block rests on real maxes. Seeding different synthetic ones afterwards
    makes them the athlete's newest rows, but not what this projection is of."""
    for lift in ALL_LIFTS:
        db_session.add(
            models.OneRepMax(
                athlete_id=ready["athlete"].id,
                lift_code=lift,
                kg=Decimal("42"),
                set_on=TESTED_ON + dt.timedelta(days=1),
                source="synthetic",
                is_synthetic=True,
            )
        )
    db_session.flush()

    assert simulate(api_client, active).json()["has_synthetic_one_rms"] is False


def test_a_negative_seed_is_a_422(api_client, active):
    assert simulate(api_client, active, seed=-1).status_code == 422


def test_simulating_writes_nothing(api_client, db_session: Session, ready, active):
    """No proposal, no approval, no new max: the route answers a question."""

    def snapshot():
        db_session.expire_all()
        return (
            [(r.seq, r.date, r.status) for r in services.sessions_of(db_session, active)],
            sorted(
                (row.lift_code, row.kg, row.set_on)
                for row in db_session.scalars(
                    sa.select(models.OneRepMax).where(
                        models.OneRepMax.athlete_id == ready["athlete"].id
                    )
                )
            ),
            db_session.scalar(sa.select(sa.func.count()).select_from(models.Proposal)),
            services.get_block(db_session, active).status,
        )

    before = snapshot()
    assert (
        simulate(
            api_client, active, scenario=[{"kind": "illness", "start_week": 3, "days": 8}]
        ).status_code
        == 200
    )
    assert snapshot() == before
