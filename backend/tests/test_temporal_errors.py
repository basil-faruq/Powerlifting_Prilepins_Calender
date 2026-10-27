"""A refusal must mean the same thing after a trip through an activity.

The point of these tests is one sentence: `api/main.py` is the only place that
turns an error into a status code (ADR-0007 rule 2), and it only sees the
exception it is handed. If an activity's `ProposalStale` arrives at the route
as a generic failure, an honest 409 becomes a 500 and the client is told the
server is broken when the real answer is "re-propose".

So each test below takes a real error, sends it the way Temporal would, and
asserts what comes out the other end is indistinguishable from what went in.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.exceptions import ApplicationError

from planner_core import temporal_errors
from planner_core.errors import (
    AlreadyDecided,
    BlockNotRunning,
    NotFound,
    PlanInvalid,
    PlannerError,
    ProposalStale,
    TemporalUnavailable,
)
from planner_core.proposal import ValidationResult, Violation


def _round_trip(exc: Exception) -> BaseException:
    """Out of an activity and back into a route, as Temporal would carry it."""
    with pytest.raises(PlannerError) as caught:
        temporal_errors.reraise_domain(temporal_errors.to_application_error(exc))
    return caught.value


# --- The five engine refusals --------------------------------------------


def test_a_stale_proposal_is_still_stale_on_the_other_side() -> None:
    back = _round_trip(ProposalStale("block already has sessions"))

    assert isinstance(back, ProposalStale)
    assert back.reason == "block already has sessions"


def test_an_already_decided_proposal_keeps_the_status_that_decided_it() -> None:
    back = _round_trip(AlreadyDecided("approved"))

    assert isinstance(back, AlreadyDecided)
    assert back.status == "approved"
    # The 409 body says *which* decision was already taken, so this matters.
    assert str(back) == "proposal is already approved"


def test_not_found_keeps_what_was_looked_for() -> None:
    key = uuid.uuid4()
    back = _round_trip(NotFound("proposal", key))

    assert isinstance(back, NotFound)
    assert back.what == "proposal"
    assert str(back.key) == str(key)


def test_a_block_that_is_not_running_keeps_its_status() -> None:
    block_id = uuid.uuid4()
    back = _round_trip(BlockNotRunning(block_id, "draft"))

    assert isinstance(back, BlockNotRunning)
    assert back.status == "draft"
    assert str(block_id) in str(back)


def test_the_violations_survive_because_they_are_the_response_body() -> None:
    """`PlanInvalid` is why this module exists rather than two lines of code.

    A shape-preview computed in an activity has to answer with the same 422 as
    one computed in-process, and the violations *are* that body (spec 06 §13).
    """
    result = ValidationResult.of(
        violations=(
            Violation(rule="past_date", message="session 4 is dated before today", refs=("4",)),
            Violation(rule="rest_gap", message="squat twice inside the rest gap", refs=("4", "5")),
        ),
        warnings=(Violation(rule="prilepin_total", message="36 reps at 56%"),),
    )

    back = _round_trip(PlanInvalid(result))

    assert isinstance(back, PlanInvalid)
    assert back.result == result
    assert [v.rule for v in back.result.violations] == ["past_date", "rest_gap"]
    # A plan can be invalid and still carry advice worth showing.
    assert [w.rule for w in back.result.warnings] == ["prilepin_total"]


# --- Retry classification -------------------------------------------------


def test_a_refusal_is_marked_never_to_be_retried() -> None:
    """Retrying a deterministic refusal is waste, and it hides the cause.

    The second attempt at an approve that already succeeded raises
    `AlreadyDecided`, which would then be reported as the failure instead of
    whatever really went wrong.
    """
    wrapped = temporal_errors.to_application_error(AlreadyDecided("approved"))

    assert wrapped.non_retryable is True
    assert wrapped.type == "AlreadyDecided"


def test_every_refusal_is_named_for_the_retry_policy_too() -> None:
    """Belt and braces: the flag and the policy list fail differently."""
    names = temporal_errors.non_retryable_types()

    assert set(names) >= {
        "AlreadyDecided",
        "BlockNotRunning",
        "NotFound",
        "PlanInvalid",
        "ProposalStale",
    }


def test_a_domain_refusal_can_register_itself() -> None:
    """`planner_core` must not import `domains`, so the domain comes to it.

    `WeekdayLocked` lives in `services.py` because a weekday map is a
    strength-domain idea, and the engine cannot name it.
    """

    class WeekdayLocked(PlannerError):
        pass

    temporal_errors.register_refusal(WeekdayLocked)
    try:
        assert "WeekdayLocked" in temporal_errors.non_retryable_types()
        assert temporal_errors.to_application_error(WeekdayLocked("locked")).non_retryable
    finally:
        temporal_errors._REFUSALS.pop("WeekdayLocked", None)


# --- What must NOT be translated -----------------------------------------


def test_a_real_bug_is_left_alone_rather_than_dressed_as_a_refusal() -> None:
    """A `KeyError` in an activity should look like a `KeyError`.

    Flattening everything into a planner error would turn a crash into a
    plausible-looking 409 and lose the traceback that explains it.
    """
    boom = ApplicationError("something broke", type="KeyError")

    with pytest.raises(ApplicationError):
        temporal_errors.reraise_domain(boom)


def test_an_unexpected_exception_passes_straight_through() -> None:
    with pytest.raises(ZeroDivisionError):
        temporal_errors.reraise_domain(ZeroDivisionError("nope"))


def test_a_refusal_nested_under_a_wrapper_is_still_found() -> None:
    """Temporal wraps a failed update, so the cause is not the top exception."""
    inner = temporal_errors.to_application_error(ProposalStale("moved on"))
    outer = ApplicationError("activity task failed", type="ActivityFailure")
    outer.__cause__ = inner

    with pytest.raises(ProposalStale) as caught:
        temporal_errors.reraise_domain(outer)
    assert caught.value.reason == "moved on"


# --- What the client turns an outage into --------------------------------


def test_an_update_that_nothing_applies_is_an_outage_not_a_bug() -> None:
    """No worker on the queue must read as 503, not 500.

    `WorkflowUpdateRPCTimeoutOrCancelledError` is raised when the server accepts
    an update but nothing ever applies it, which is exactly "no worker running".
    It is NOT a `TimeoutError`, so it used to fall through to the bug branch:
    approving a proposal with the worker stopped answered `500 Internal Server
    Error`, and the UI told the athlete nothing it could act on. Found by
    stopping the worker during spec 19's live run.
    """
    from temporalio.client import WorkflowUpdateRPCTimeoutOrCancelledError

    from api import temporal_client

    with pytest.raises(TemporalUnavailable) as caught:
        temporal_client.translate(WorkflowUpdateRPCTimeoutOrCancelledError())

    # The message has to name the fix, because the athlete is the one who runs it.
    assert "make infra" in str(caught.value)


def test_a_bug_inside_the_client_still_looks_like_a_bug() -> None:
    from api import temporal_client

    with pytest.raises(ZeroDivisionError):
        temporal_client.translate(ZeroDivisionError("nope"))
