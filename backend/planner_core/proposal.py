"""How a change travels from a solver to a human decision, in any domain.

Four shapes and one protocol, and nothing in them knows what a barbell is:

* `Violation` — one rule, broken or warned about, in words a person can read.
* `ValidationResult` — violations reject, warnings inform. `ok` is derived.
* `Proposal` — a candidate change plus its cost, its reasons and its warnings,
  waiting for someone to approve it. Generic in its payload, so the strength
  domain's `StrengthPayload` and spec 23's job-shop payload both fit.
* `Validator` — the protocol a domain's validator satisfies.

This module is the reason `planner_core` can be called an engine rather than a
folder: the solver (spec 07), the agent's tools (spec 13), the Temporal workflow
(spec 15) and the approval UI (spec 19) all speak in these types, and none of
them imports a domain to do it. `planner_core` must not import from `domains`
(CLAUDE.md's layout rule); a test walks this package's AST and asserts it.

Everything here is frozen, and every collection is a tuple — a validation
result that can be edited after the fact is not evidence of anything. Spec 05
§5A says `list`; this is the same one-word deviation, for the same reason, that
ADR-0003 modification 13 made for `ParsedTemplate.entries`.
"""

from __future__ import annotations

import uuid
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The vocabulary of a proposal. These live here rather than in a domain because
# `Proposal` is typed by them and `planner_core` cannot import `domains`;
# `domains.strength.enums` re-exports them, so `models.Proposal`'s CHECK
# constraints and these literals cannot drift apart.
ProposalKind = Literal["new_block", "reschedule", "weekday_change"]
ProposalStatus = Literal["pending", "approved", "rejected", "superseded"]
ProposalSource = Literal["chat", "workflow"]


class Violation(BaseModel):
    """One broken rule, or one thing worth saying about an otherwise fine plan.

    Whether this rejects a plan or merely annotates it is decided by which list
    of a `ValidationResult` it lands in, not by anything here. The same rule id
    is therefore never both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A stable identifier, not prose: the UI groups on it and spec 21's trace
    # view records it. Snake case, matching the rule tables in spec 05 §5C.
    rule: str = Field(min_length=1)
    # For a person. Spec 05 §11: it names the lift, week and slot.
    message: str = Field(min_length=1)
    # What the violation is about, as `"seq:12"`-style identifiers, so the
    # calendar can highlight exactly the sessions involved.
    refs: tuple[str, ...] = ()


class ValidationResult(BaseModel):
    """The verdict on a plan: what rejects it, and what merely deserves saying."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    violations: tuple[Violation, ...] = ()
    warnings: tuple[Violation, ...] = ()

    @model_validator(mode="after")
    def _ok_follows_the_violations(self) -> ValidationResult:
        """`ok` is not an independent opinion.

        A result carrying violations while claiming `ok` would let a plan past
        the one gate that exists (CLAUDE.md non-negotiable #2), so the
        combination cannot be constructed. Prefer `ValidationResult.of`, which
        derives `ok` instead of asserting it.
        """
        if self.ok == bool(self.violations):
            raise ValueError(
                f"ok={self.ok} contradicts {len(self.violations)} violation(s): "
                "ok is true exactly when there are none"
            )
        return self

    @classmethod
    def of(
        cls,
        violations: tuple[Violation, ...] = (),
        warnings: tuple[Violation, ...] = (),
    ) -> ValidationResult:
        """The normal constructor: violations and warnings in, `ok` derived."""
        return cls(ok=not violations, violations=tuple(violations), warnings=tuple(warnings))


class Proposal[PayloadT](BaseModel):
    """A candidate change, costed and explained, waiting for a human.

    Nothing in this system writes without one (CLAUDE.md non-negotiable #2).
    `warnings` is copied out of the `ValidationResult` so the approval card can
    show what the athlete is accepting; violations never reach a proposal,
    because a plan carrying one is not offered at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    block_id: uuid.UUID
    kind: ProposalKind
    payload: PayloadT
    # Solver-reported, domain-defined: `{"days_moved": 2.0}` here, a makespan
    # delta in spec 23. `float` is deliberate and is the one place it is
    # allowed — a cost is for ranking and display, never for arithmetic that
    # reaches a weight (CLAUDE.md non-negotiable #4).
    cost: dict[str, float] = Field(default_factory=dict)
    # Why this option, in the solver's own words. The LLM explainer (spec 13)
    # may rephrase these but may not add a number that is not already here.
    reasons: tuple[str, ...] = ()
    warnings: tuple[Violation, ...] = ()
    source: ProposalSource
    status: ProposalStatus = "pending"


@runtime_checkable
class Validator(Protocol):
    """What a domain must provide to have its plans checked.

    Structural, so a domain's validator satisfies it without importing it --
    the same approach `generator.TemplateRow` takes. Spec 23's job-shop adapter
    plugs in here with no change to `planner_core`.
    """

    def validate(self, plan: object, context: object) -> ValidationResult: ...
