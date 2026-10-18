"""Errors the proposal lifecycle raises, and the HTTP codes they mean.

These live in `planner_core` rather than in the strength domain because they
describe the *engine's* workflow -- solver proposes, validator rejects, human
approves (CLAUDE.md non-negotiable #2) -- and spec 23's jobshop domain reaches
the same four dead ends. `PlanInvalid` carries a `ValidationResult`, which is
already a `planner_core` type, so nothing here imports from `domains`.

Only the error-to-status mapping lives in `api/main.py`; the services raise
these and never touch `HTTPException`, so `approve_proposal` stays callable from
a Temporal activity in spec 15, where there is no request to fail.

`LlmUnavailable` and its two subclasses join them from spec 11. They are not
part of the proposal lifecycle, but they are engine-level for the same reason:
`planner_core/llm.py` is the only module allowed to import a provider SDK
(CLAUDE.md), so the errors it raises cannot live in a domain either.
"""

from __future__ import annotations

from planner_core.proposal import ValidationResult, Violation


class PlannerError(Exception):
    """Base for every error the planner raises deliberately.

    A bare `except PlannerError` in a route would hide the distinctions below,
    so this exists to be caught in tests and in spec 13's agent tools, not in
    the API.
    """


class NotFound(PlannerError):
    """No such row. → 404.

    Carries what was looked for rather than a formatted sentence, so the route
    can phrase it and a tool can read it back.
    """

    def __init__(self, what: str, key: object) -> None:
        self.what = what
        self.key = key
        super().__init__(f"no {what} with id {key}")


class PlanInvalid(PlannerError):
    """The validator rejected the plan. → 422, body lists the violations.

    Raised with the whole `ValidationResult` rather than a message, because the
    violations *are* the response body (spec 06 §13) and the warnings travel
    with them -- a plan can be invalid and still carry advice worth showing.
    """

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__("; ".join(v.message for v in result.violations))

    @classmethod
    def of(cls, rule: str, message: str, refs: tuple[str, ...] = ()) -> PlanInvalid:
        """One violation, for the deterministic failures that are not the validator's.

        `generate_block` raises a plain `ValueError` for a missing 1RM or a
        non-Monday start (ADR-0005). Those reach the client through the same
        shape as a validator verdict, so a caller has one error body to parse
        rather than two.
        """
        return cls(
            ValidationResult.of(violations=(Violation(rule=rule, message=message, refs=refs),))
        )


class ProposalStale(PlannerError):
    """The block moved on since the proposal was computed. → 409.

    Distinct from `PlanInvalid`: the plan is not wrong, it is answering a
    question about a state the database has left. Re-propose rather than fix.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BlockNotRunning(PlannerError):
    """The block is not one that is under way. → 409.

    Raised by anything that asks "what happens next?" of a block with no next:
    a `draft` has no sessions, a `done` or `abandoned` block has nothing left to
    do. `active` and `paused` both count as running (ADR-0010), because a paused
    block is the one training resumes into.

    Carries the status rather than a sentence so a client can tell "not yet
    approved" from "already over" without parsing prose.
    """

    def __init__(self, block_id: object, status: str) -> None:
        self.block_id = block_id
        self.status = status
        super().__init__(f"block {block_id} is {status}, not running")


class AlreadyDecided(PlannerError):
    """This proposal was approved, rejected or superseded already. → 409.

    This is what makes a second approve on the same proposal a 409 rather than
    a silent re-run (spec 06 §12), which matters because approval is the only
    write path and re-running it would duplicate sessions.
    """

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"proposal is already {status}")


class TemporalUnavailable(PlannerError):
    """The workflow service could not be reached. → 503.

    The sibling of `LlmUnavailable` below, and 503 for the same reason: the
    request was well formed and the plan may be perfect, the machine that owns
    the block's state is simply not there. The fix is to start something, not
    to change the request, so the message names what.

    Spec 15 routes approval through the workflow, which makes Temporal a hard
    dependency of approving, rejecting or editing a proposal. It is pointedly
    *not* one of logging a session: that route commits the athlete's record
    first and the signal afterwards, because a training log must never be lost
    to infrastructure (ADR-0014, and `services.log_session`'s own docstring).
    """

    def __init__(self, address: str, detail: str = "") -> None:
        self.address = address
        self.detail = detail
        message = (
            f"The workflow service is not reachable at {address}. "
            "Start it with 'make infra', and the worker with 'make worker'."
        )
        if detail:
            message = (
                f"The workflow service at {address} did not serve the request ({detail}). "
                "If it is not running, start it with 'make infra' and 'make worker'."
            )
        super().__init__(message)


class LlmUnavailable(PlannerError):
    """The local model service could not serve a request. → 503.

    Separate from everything above because it says nothing about the *plan*: the
    request was well formed and the plan may be perfect, the machine that embeds
    or routes it is simply not there. 503 rather than 500 for the same reason --
    the fix is to start Ollama, not to change the request.

    Spec 12 catches this base class when indexing a note, so that a training log
    is never lost to a model being down: the log is saved and the note is left
    for `reindex`. Both subclasses are caught by that one `except`, which is why
    they share a base at all.
    """


class OllamaUnreachable(LlmUnavailable):
    """Nothing answered at Ollama's configured address.

    Carries the URL it tried, because the usual cause is that Ollama is running
    but not where settings say -- it lives natively on the host, never in
    Compose (CLAUDE.md), so a containerised caller needs a different host than a
    local one and the address is the first thing to check.

    `detail` carries the last failure when there was one. It matters because a
    persistent 500 reaches here too, and "Ollama is loading the model" or "out
    of memory" is not fixed by starting a service that is already running --
    without the detail the message would send the reader somewhere useless.
    """

    def __init__(self, base_url: str, detail: str = "") -> None:
        self.base_url = base_url
        self.detail = detail
        message = f"Ollama not reachable at {base_url}. Start it with 'ollama serve'."
        if detail:
            message = f"Ollama at {base_url} did not serve the request ({detail}). "
            message += "If it is not running, start it with 'ollama serve'."
        super().__init__(message)


class ModelNotPulled(LlmUnavailable):
    """Ollama is running, but the model it was asked for is not downloaded.

    The message names the exact `ollama pull` command rather than describing the
    problem, because that command *is* the fix and retyping a model name is how
    a typo survives. `detail` keeps whatever Ollama itself said, so a 404 that
    turns out to be a wrong endpoint rather than a missing model is still
    diagnosable from the error alone.
    """

    def __init__(self, model: str, detail: str = "") -> None:
        self.model = model
        self.detail = detail
        message = f"Model {model} not found. Run: ollama pull {model}"
        super().__init__(f"{message} ({detail})" if detail else message)
