"""Persisting a policy result, and the solver stats that travel with it.

Spec 08 produced `PolicyResult.proposals` and never wrote them anywhere, which
nothing noticed because nothing outside its own tests called
`propose_for_missed`. Spec 13's `reschedule` tool is the first caller that
needs rows: `approve_proposal` takes a `proposal_id` and looks one up, so until
these are written a reschedule offers the athlete options with nothing to
approve.

Needs Postgres, and skips cleanly without it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlmodel import Session

from domains.strength import missed_policy, models, services
from tests.api.conftest import propose

CFG = missed_policy.MissedPolicyConfig(solver_time_limit_s=2.0, solver_num_workers=1)


@pytest.fixture
def active(api_client, db_session: Session, ready) -> uuid.UUID:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    return block_id


def _mark(db_session: Session, block_id: uuid.UUID, seq: int, status: str) -> None:
    row = next(r for r in services.sessions_of(db_session, block_id) if r.seq == seq)
    row.status = status
    db_session.add(row)
    db_session.flush()


@pytest.fixture
def missed_result(db_session: Session, active) -> tuple[uuid.UUID, missed_policy.PolicyResult]:
    """One in-week miss, which is the tier that offers more than one option."""
    for seq in (1, 2):
        _mark(db_session, active, seq, "done")

    missed = next(r for r in services.sessions_of(db_session, active) if r.seq == 3)
    event = missed_policy.MissedEvent(
        block_id=active,
        missed_seqs=(3,),
        unavailable_from=missed.date,
        unavailable_until=missed.date,
        today=missed.date + dt.timedelta(days=1),
    )
    result = services.propose_for_missed(db_session, event, cfg=CFG)
    assert result.ok, result.failure
    return active, result


def test_saving_a_result_writes_one_row_per_option(db_session: Session, missed_result):
    _block_id, result = missed_result

    rows = services.save_proposals(db_session, result)

    assert len(rows) == len(result.proposals)
    assert all(isinstance(row, models.Proposal) for row in rows)


def test_a_saved_row_keeps_the_value_objects_own_id(db_session: Session, missed_result):
    """The id the agent records in `tool_calls` has to be the id approval takes.

    If the row got a fresh id, the agent would hand the athlete an id that
    `approve_proposal` cannot find -- and the failure would look like a stale
    proposal rather than a mismatch.
    """
    _block_id, result = missed_result

    rows = services.save_proposals(db_session, result)

    assert [row.id for row in rows] == [proposal.id for proposal in result.proposals]


def test_a_saved_row_is_findable_and_approvable(db_session: Session, missed_result):
    _block_id, result = missed_result

    (first, *_rest) = services.save_proposals(db_session, result)
    found = services.get_proposal(db_session, first.id)

    assert found.status == "pending"
    assert found.kind == "reschedule"
    # The whole point: this is the call that had nothing to look up before.
    services.approve_proposal(db_session, found.id)
    db_session.refresh(found)
    assert found.status == "approved"


def test_the_payload_round_trips(db_session: Session, missed_result):
    _block_id, result = missed_result

    (first, *_rest) = services.save_proposals(db_session, result)
    payload = services.read_payload(first)

    assert payload.plan == result.proposals[0].payload.plan
    assert payload.changes == result.proposals[0].payload.changes


def test_reasons_warnings_and_cost_survive(db_session: Session, missed_result):
    """Spec 21's trace reads `reasons`, and the explainer's fallback is built
    from them, so losing them in the write would be invisible until then."""
    _block_id, result = missed_result
    source = result.proposals[0]

    (first, *_rest) = services.save_proposals(db_session, result)

    assert first.reasons == list(source.reasons)
    assert len(first.warnings) == len(source.warnings)
    assert first.cost == dict(source.cost)
    assert first.source == "chat"


def test_the_rows_keep_the_policys_ranking(db_session: Session, missed_result):
    """Cheapest first, which is what makes "the first one" a defensible default
    for `decision.proposal_id` when three options share one column."""
    _block_id, result = missed_result
    if len(result.proposals) < 2:
        pytest.skip("this miss had only one option")

    rows = services.save_proposals(db_session, result)

    assert [row.cost.get("total") for row in rows] == [
        proposal.cost.get("total") for proposal in result.proposals
    ]
    totals = [row.cost["total"] for row in rows if "total" in row.cost]
    assert totals == sorted(totals)


def test_saving_a_failure_writes_nothing(db_session: Session, active):
    """A `PolicyFailed` is an answer, not an error -- and not a row."""
    failed = missed_policy.PolicyResult(
        tier="fold",
        failure=missed_policy.PolicyFailed(reason="nothing to fold into"),
    )

    assert services.save_proposals(db_session, failed) == []


def test_save_proposals_does_not_commit(db_session: Session, missed_result):
    """ADR-0007 rule 2: services flush, the route commits.

    Spec 15 calls these from a Temporal activity that owns its own
    transaction, so a commit here would take that decision away from it.
    """
    _block_id, result = missed_result

    services.save_proposals(db_session, result)

    assert db_session.in_transaction()


# --- The solver stats the span needs -------------------------------------


def test_an_in_week_result_carries_the_solvers_stats(db_session: Session, missed_result):
    """Spec 14's `solver.calendar` span is emitted by `propose_for_missed`,
    which is the nearest impure caller -- `calendar_solver.solve` is reached
    only from pure `missed_policy`. Without this field the span would have
    nothing to report on the success path (ADR-0016)."""
    _block_id, result = missed_result

    if result.tier != "in_week_move":
        pytest.skip("stats only exist where CP-SAT ran")
    assert result.stats is not None
    assert result.stats.status in ("OPTIMAL", "FEASIBLE")
    assert result.stats.wall_time_s >= 0


def test_the_fast_path_reports_no_solver_stats(db_session: Session, active):
    """Honest rather than convenient: the greedy path ran no solve.

    Reporting zeros would put a fabricated `wall_time_s` in a trace, which is
    the same mistake as letting the LLM invent a number.
    """
    for seq in (1, 2):
        _mark(db_session, active, seq, "done")
    missed = next(r for r in services.sessions_of(db_session, active) if r.seq == 3)
    event = missed_policy.MissedEvent(
        block_id=active,
        missed_seqs=(3,),
        unavailable_from=missed.date,
        unavailable_until=missed.date,
        today=missed.date + dt.timedelta(days=1),
    )

    result = services.propose_for_missed(db_session, event, cfg=CFG, fast=True)

    assert result.ok, result.failure
    assert result.stats is None
