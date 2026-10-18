"""Carrying a planner error across the Temporal activity boundary, and back.

An activity runs in another process. When `approve_proposal` refuses a stale
proposal there, the exception has to survive serialisation and arrive at the
route as the *same* refusal, or `api/main.py`'s handlers -- the single place
that knows an error is also a status code (ADR-0007 rule 2) -- would turn an
honest 409 into a 500.

So this module is one translation with two halves, kept together because they
are only correct in pairs:

* `to_application_error` wraps a `PlannerError` on the way out of an activity.
* `reraise_domain` unwraps it on the way in to a route.

Everything it translates is a **deterministic refusal**: the proposal was
already decided, the plan is invalid, the block moved on. Retrying those is
pure waste and a retried `AlreadyDecided` would mask whatever really happened,
so each one is marked `non_retryable` and the retry policy names them too --
belt and braces, because the two mechanisms fail differently.

`PlanInvalid` is the reason this is a module and not two lines: it carries a
whole `ValidationResult`, and the violations *are* the 422 body. They travel in
the failure's `details` and are rebuilt on the far side, so a shape-preview
computed in an activity answers byte-identically to one computed in-process.

This lives in `planner_core` because both halves have callers that must not
import each other: the activities are strength-domain code, the client is API
code, and the engine is what they already share.
"""

from __future__ import annotations

from typing import Any, NoReturn

from temporalio.exceptions import ApplicationError, FailureError

from planner_core.errors import (
    AlreadyDecided,
    BlockNotRunning,
    NotFound,
    PlanInvalid,
    PlannerError,
    ProposalStale,
)
from planner_core.proposal import ValidationResult

# Every error here is a refusal the activity will give again, identically, on
# every attempt. The names are what `RetryPolicy.non_retryable_error_types`
# matches on, so they are the class names as `ApplicationError.type` reports
# them -- keep the two in step.
#
# `WeekdayLocked` is deliberately absent: it lives in the strength domain
# (`services.py`), not in the engine, so it is registered by the activity layer
# through `register_refusal` rather than named here.
_REFUSALS: dict[str, type[PlannerError]] = {
    cls.__name__: cls
    for cls in (NotFound, PlanInvalid, ProposalStale, BlockNotRunning, AlreadyDecided)
}


def register_refusal(exc_type: type[Exception]) -> None:
    """Add a domain error to the set carried across the boundary verbatim.

    `planner_core` must not import from `domains` (CLAUDE.md), so a
    domain-specific refusal registers itself instead of being listed above.
    """
    _REFUSALS[exc_type.__name__] = exc_type  # type: ignore[assignment]


def non_retryable_types() -> list[str]:
    """The names for `RetryPolicy(non_retryable_error_types=...)`."""
    return sorted(_REFUSALS)


def is_refusal(exc: BaseException) -> bool:
    return type(exc).__name__ in _REFUSALS


def to_application_error(exc: Exception) -> ApplicationError:
    """Wrap a domain refusal so it survives the trip out of an activity.

    The `type` is the class name, which is both what the retry policy matches
    and what `reraise_domain` rebuilds from. `details` carries the few fields
    each error was constructed with -- not the formatted message, which is
    derived from them anyway.
    """
    return ApplicationError(
        str(exc),
        _detail(exc),
        type=type(exc).__name__,
        non_retryable=is_refusal(exc),
    )


def _detail(exc: Exception) -> dict[str, Any]:
    """The constructor arguments, as JSON the data converter can carry."""
    if isinstance(exc, PlanInvalid):
        return {"result": exc.result.model_dump(mode="json")}
    if isinstance(exc, NotFound):
        return {"what": exc.what, "key": str(exc.key)}
    if isinstance(exc, ProposalStale):
        return {"reason": exc.reason}
    if isinstance(exc, BlockNotRunning):
        return {"block_id": str(exc.block_id), "status": exc.status}
    if isinstance(exc, AlreadyDecided):
        return {"status": exc.status}
    # A registered domain refusal with no rebuild rule, or a bug. The message
    # still crosses; only the structure is lost.
    return {"message": str(exc)}


def reraise_domain(exc: BaseException) -> NoReturn:
    """Re-raise a workflow failure as the domain error it started as.

    Temporal nests the cause -- a failed update arrives wrapped in one or two
    outer failures -- so this walks down to the first `ApplicationError` it
    recognises. Anything unrecognised is re-raised untouched rather than
    flattened into a generic error: a real bug in an activity should look like
    one, not like a refusal.
    """
    found = _find_application_error(exc)
    if found is None:
        raise exc
    rebuilt = _rebuild(found)
    if rebuilt is None:
        raise exc
    raise rebuilt from exc


def _find_application_error(exc: BaseException) -> ApplicationError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApplicationError) and current.type in _REFUSALS:
            return current
        if isinstance(current, FailureError) and current.cause is not None:
            current = current.cause
            continue
        current = current.__cause__
    return None


def _rebuild(failure: ApplicationError) -> PlannerError | None:
    """Reconstruct the original error from `type` and `details`."""
    cls = _REFUSALS.get(failure.type or "")
    if cls is None:
        return None
    detail = failure.details[0] if failure.details else {}
    if not isinstance(detail, dict):
        return None
    try:
        if cls is PlanInvalid:
            return PlanInvalid(ValidationResult.model_validate(detail["result"]))
        if cls is NotFound:
            return NotFound(detail["what"], detail["key"])
        if cls is ProposalStale:
            return ProposalStale(detail["reason"])
        if cls is BlockNotRunning:
            return BlockNotRunning(detail["block_id"], detail["status"])
        if cls is AlreadyDecided:
            return AlreadyDecided(detail["status"])
    except (KeyError, TypeError, ValueError):
        return None
    # A refusal registered by a domain. It is rebuilt only if its constructor
    # takes the message, which is the convention for domain-level refusals.
    try:
        return cls(detail.get("message", failure.message))  # type: ignore[call-arg]
    except TypeError:
        return None
