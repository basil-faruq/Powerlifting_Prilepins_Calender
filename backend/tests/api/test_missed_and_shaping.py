"""The database half of specs 08 and 09.

`test_missed_policy.py` and `test_shaping_solver.py` prove the pure logic.
What is left is everything those two cannot reach, because it only exists
once rows do:

* reading a running block back out as a `BlockState`, and the lift-ordering
  trap that reading does
* the two write-path branches a fold and a restart need, both inside
  `approve_proposal` so the single-write-path rule holds
* `POST /blocks/shape-preview`

These need Postgres and skip cleanly without it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlmodel import Session

from domains.strength import missed_policy, models, services
from tests.api.conftest import ALL_LIFTS, START, block_body, propose

CFG = missed_policy.MissedPolicyConfig(solver_time_limit_s=2.0, solver_num_workers=1)


@pytest.fixture
def active(api_client, db_session: Session, ready) -> tuple[uuid.UUID, models.Block]:
    """A block that has been approved, so its sessions exist and it is running."""
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    block = services.get_block(db_session, block_id)
    db_session.refresh(block)
    return block_id, block


def mark(db_session: Session, block_id: uuid.UUID, seq: int, status: str) -> None:
    row = next(r for r in services.sessions_of(db_session, block_id) if r.seq == seq)
    row.status = status
    db_session.add(row)
    db_session.flush()


# --- 1. Reading a running block back -----------------------------------------


def test_a_block_round_trips_through_the_database_unchanged(
    db_session: Session, api_client, ready, active
):
    """`draft_session_of` is `persist_planned_sessions`'s declared inverse,
    and this is the test that holds the two together."""
    block_id, block = active
    state = services.load_block_state(db_session, block_id)

    draft, _ = services.build_block_draft(
        db_session,
        block.athlete_id,
        block.template_id,
        ALL_LIFTS,
        START,
    )
    assert state.plan == draft.sessions


def test_reading_a_block_keeps_its_lifts_in_training_order(
    db_session: Session, api_client, ready, active
):
    """The trap `draft_session_of` exists to avoid.

    `generate_block` orders a session's lifts by `enums.LIFT_CODES` -- squat
    before bench, the order they are trained in -- while `lifts_of` reads
    them back alphabetically. Without the re-sort, every sentence the policy
    writes about day 1 would call it "bench/squat".
    """
    block_id, _ = active
    state = services.load_block_state(db_session, block_id)
    first = state.by_seq[1]
    assert [lift.lift_code for lift in first.lifts] == ["squat", "bench"]


def test_a_draft_block_has_no_state_to_load(db_session: Session, api_client, ready):
    """Its schedule is still in a pending proposal, so there is nothing to
    reschedule around."""
    from planner_core.errors import PlanInvalid

    block_id, _ = propose(api_client, ready["athlete"], ready["template"])
    with pytest.raises(PlanInvalid):
        services.load_block_state(db_session, block_id)


def test_the_state_reports_done_sessions_as_history(db_session: Session, api_client, ready, active):
    block_id, _ = active
    mark(db_session, block_id, 1, "done")
    mark(db_session, block_id, 2, "missed")
    state = services.load_block_state(db_session, block_id)
    assert state.history_seqs == frozenset({1, 2})


# --- 2. The policy end to end ------------------------------------------------


def test_proposing_for_a_missed_session_returns_a_validated_reschedule(
    db_session: Session, api_client, ready, active
):
    block_id, _ = active
    for seq in (1, 2):
        mark(db_session, block_id, seq, "done")

    missed = next(r for r in services.sessions_of(db_session, block_id) if r.seq == 3)
    event = missed_policy.MissedEvent(
        block_id=block_id,
        missed_seqs=(3,),
        unavailable_from=missed.date,
        unavailable_until=missed.date,
        today=missed.date + dt.timedelta(days=1),
    )
    result = services.propose_for_missed(db_session, event, cfg=CFG)
    assert result.ok, result.failure
    assert result.tier in ("in_week_move", "week_shift")


def test_a_session_moved_by_an_earlier_approval_does_not_block_the_next_one(
    db_session: Session, api_client, ready, active
):
    """The gap ADR-0009 carried forward.

    A session parked off its home weekday by an approved reschedule is not
    mentioned by the *next* proposal, so without the `moved` waiver the
    weekday rule would fail it and drop every option. That would make the
    second missed session of a block unplannable, every time.
    """
    block_id, _ = active
    rows = services.sessions_of(db_session, block_id)
    for seq in (1, 2):
        mark(db_session, block_id, seq, "done")
    # Session 3 was approved onto a Saturday by an earlier reschedule.
    parked = next(r for r in rows if r.seq == 3)
    parked.date = parked.date + dt.timedelta(days=1)
    parked.status = "moved"
    db_session.add(parked)
    db_session.flush()

    missed = next(r for r in services.sessions_of(db_session, block_id) if r.seq == 4)
    event = missed_policy.MissedEvent(
        block_id=block_id,
        missed_seqs=(4,),
        unavailable_from=missed.date,
        unavailable_until=missed.date,
        today=missed.date + dt.timedelta(days=1),
    )
    result = services.propose_for_missed(db_session, event, cfg=CFG)
    assert result.ok, result.failure


# --- 3. The fold write path --------------------------------------------------


def test_approving_a_fold_moves_the_lifts_and_writes_off_the_session(
    db_session: Session, api_client, ready, active
):
    """The one change that rewrites what a session *contains*.

    `_apply_plan` otherwise only touches dates and statuses, so this is the
    branch that had to be added -- inside `approve_proposal`, which keeps
    ADR-0007 rule 1 intact.
    """
    block_id, _ = active
    for seq in (1, 2):
        mark(db_session, block_id, seq, "done")
    # Day 1 of week 2 was missed; day 2 of week 2 was trained anyway.
    mark(db_session, block_id, 5, "done")

    missed = next(r for r in services.sessions_of(db_session, block_id) if r.seq == 4)
    event = missed_policy.MissedEvent(
        block_id=block_id,
        missed_seqs=(4,),
        unavailable_from=missed.date,
        unavailable_until=missed.date,
        today=missed.date + dt.timedelta(days=3),
    )
    result = services.propose_for_missed(db_session, event, cfg=CFG)
    assert result.tier == "fold", result.failure
    assert result.ok, result.failure

    policy_proposal = result.proposals[0]
    change = policy_proposal.payload.changes[0]
    target_seq = change.into_seq

    before = len(services.lifts_of(db_session, _row(db_session, block_id, target_seq).id))
    folded_count = len(services.lifts_of(db_session, _row(db_session, block_id, 4).id))

    stored = models.Proposal(
        block_id=block_id,
        kind="reschedule",
        status="pending",
        payload=policy_proposal.payload.model_dump(mode="json"),
        reasons=list(policy_proposal.reasons),
        warnings=[],
        source="chat",
    )
    db_session.add(stored)
    db_session.flush()

    services.approve_proposal(db_session, stored.id)
    db_session.flush()

    assert _row(db_session, block_id, 4).status == "skipped"
    after = len(services.lifts_of(db_session, _row(db_session, block_id, target_seq).id))
    assert after == before + folded_count


def _row(db_session: Session, block_id: uuid.UUID, seq: int) -> models.PlannedSession:
    return next(r for r in services.sessions_of(db_session, block_id) if r.seq == seq)


# --- 4. The restart write path -----------------------------------------------


def test_approving_a_restart_abandons_the_block_it_replaces(
    db_session: Session, api_client, ready, active
):
    """Otherwise the athlete ends up with two running blocks and
    `running_block` has to guess between them."""
    block_id, block = active
    missed = _row(db_session, block_id, 1)
    event = missed_policy.MissedEvent(
        block_id=block_id,
        missed_seqs=(1,),
        unavailable_from=missed.date,
        today=missed.date + dt.timedelta(days=1),
        injury=True,
    )
    result = services.propose_for_missed(db_session, event, cfg=CFG)
    assert result.tier == "restart"
    assert result.ok, result.failure

    policy_proposal = result.proposals[0]
    assert policy_proposal.kind == "new_block"
    assert policy_proposal.payload.supersedes_block_id == block_id

    stored = models.Proposal(
        block_id=policy_proposal.block_id,
        kind="new_block",
        status="pending",
        payload=policy_proposal.payload.model_dump(mode="json"),
        reasons=list(policy_proposal.reasons),
        warnings=[],
        source="chat",
    )
    db_session.add(stored)
    db_session.flush()
    services.approve_proposal(db_session, stored.id)
    db_session.flush()

    db_session.refresh(block)
    assert block.status == "abandoned"
    assert services.get_block(db_session, policy_proposal.block_id).status == "active"


def test_a_restart_writes_no_one_rep_max_row(db_session: Session, api_client, ready, active):
    """CLAUDE.md #9 and ADR-0005 rule 6: the reduction is an assumption about
    one block, not a measurement of the athlete. It lives in the new block's
    snapshot and nowhere else."""
    block_id, block = active
    before = {
        (row.lift_code, row.kg)
        for row in services.current_one_rms(db_session, block.athlete_id).values()
    }

    missed = _row(db_session, block_id, 1)
    event = missed_policy.MissedEvent(
        block_id=block_id,
        missed_seqs=(1,),
        unavailable_from=missed.date,
        today=missed.date + dt.timedelta(days=1),
        injury=True,
    )
    result = services.propose_for_missed(db_session, event, cfg=CFG)
    assert result.ok, result.failure

    after = {
        (row.lift_code, row.kg)
        for row in services.current_one_rms(db_session, block.athlete_id).values()
    }
    assert after == before

    replacement = services.get_block(db_session, result.proposals[0].block_id)
    for lift, kg in replacement.one_rm_snapshot.items():
        assert Decimal(kg) < Decimal(block.one_rm_snapshot[lift])


# --- 5. POST /blocks/shape-preview -------------------------------------------


def test_shape_preview_with_no_options_reproduces_the_ordinary_preview(api_client, ready):
    """The claim the whole module rests on: shaping with default settings is
    the template, so the two routes agree weight for weight."""
    body = block_body(ready["athlete"], ready["template"])
    plain = api_client.post("/blocks/preview", json=body)
    shaped = api_client.post("/blocks/shape-preview", json=body)
    assert plain.status_code == 200, plain.text
    assert shaped.status_code == 200, shaped.text
    assert shaped.json()["draft"] == plain.json()["draft"]


def test_shape_preview_reports_what_the_solver_did(api_client, ready):
    body = block_body(ready["athlete"], ready["template"])
    response = api_client.post("/blocks/shape-preview", json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["weeks"] == 11
    assert out["overreach_from_week"] == 7
    assert out["test_week"] == {"squat": 11, "bench": 10, "press": 9, "deadlift": 11}
    # Default shaping deviates from the template by nothing at all.
    assert out["cost"]["pct_dev"] == 0
    assert out["cost"]["vol_dev"] == 0
    assert out["cost"]["rep_dev"] == 0
    assert out["stats"]["status"] == "OPTIMAL"


def test_shape_preview_can_shorten_a_block(api_client, ready):
    body = block_body(ready["athlete"], ready["template"], weeks=8)
    response = api_client.post("/blocks/shape-preview", json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["weeks"] == 8
    assert out["overreach_from_week"] == 6
    assert {s["week"] for s in out["draft"]["sessions"]} == set(range(1, 9))
    assert out["validation"]["ok"] is True


def test_shape_preview_can_move_a_test_week(api_client, ready):
    body = block_body(ready["athlete"], ready["template"], test_week={"press": 11})
    response = api_client.post("/blocks/shape-preview", json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["test_week"]["press"] == 11
    tests = [
        (session["week"], lift["lift_code"])
        for session in out["draft"]["sessions"]
        for lift in session["lifts"]
        if lift["is_test"]
    ]
    assert (11, "press") in tests
    assert (9, "press") not in tests


@pytest.mark.parametrize("weeks", [5, 15])
def test_shape_preview_refuses_a_week_count_outside_the_range(api_client, ready, weeks):
    """Spec 09 §13. The field bound alone is enough, so the solver is never
    asked an impossible question."""
    body = block_body(ready["athlete"], ready["template"], weeks=weeks)
    response = api_client.post("/blocks/shape-preview", json=body)
    assert response.status_code == 422, response.text
