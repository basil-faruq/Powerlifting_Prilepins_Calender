"""The domain-agnostic proposal types, and the boundary that keeps them agnostic.

`planner_core.proposal` is what every later spec carries a change in -- the
solver's output, the agent's tool result, the workflow's signal, the UI's
approval card. The strength domain plugs into it; it never reaches back.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pydantic
import pytest

import planner_core
from domains.strength import enums
from domains.strength.schemas import SessionChange, StrengthPayload
from domains.strength.validator import StrengthValidator
from planner_core.proposal import (
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalStatus,
    ValidationResult,
    Validator,
    Violation,
)

# --- ValidationResult -----------------------------------------------------


def test_a_result_with_no_violations_is_ok():
    warning = Violation(rule="volume_trend", message="squat W1 to W2: rising")

    result = ValidationResult.of((), (warning,))

    assert result.ok is True
    assert result.warnings == (warning,)


def test_a_result_with_a_violation_is_not_ok():
    violation = Violation(rule="pct_cap", message="squat W11 S1: too heavy", refs=("seq:31",))

    assert ValidationResult.of((violation,)).ok is False


@pytest.mark.parametrize("ok", [True, False])
def test_ok_must_agree_with_the_violations(ok):
    violations = () if not ok else (Violation(rule="pct_cap", message="x"),)

    with pytest.raises(pydantic.ValidationError, match="ok"):
        ValidationResult(ok=ok, violations=violations)


def test_a_violation_has_no_refs_by_default_and_is_frozen():
    violation = Violation(rule="order", message="x")

    assert violation.refs == ()
    with pytest.raises(pydantic.ValidationError):
        violation.rule = "other"


# --- Proposal -------------------------------------------------------------


def test_a_strength_proposal_round_trips_through_json_exactly(golden_draft):
    """`proposal.payload` is JSONB, so the payload must survive JSON unchanged --
    Decimals as strings, never floats."""
    payload = StrengthPayload(
        changes=(
            SessionChange(
                seq=4,
                week=2,
                slot=1,
                action="move",
                from_date=golden_draft.sessions[3].date,
                to_date=golden_draft.sessions[3].date,
            ),
        ),
        plan=golden_draft.sessions,
        new_one_rms={"squat": Decimal("147.5")},
    )
    proposal = Proposal[StrengthPayload](
        block_id=uuid.uuid4(),
        kind="reschedule",
        payload=payload,
        cost={"days_moved": 1.0},
        reasons=("missed Monday",),
        source="chat",
    )

    restored = Proposal[StrengthPayload].model_validate_json(proposal.model_dump_json())

    assert restored == proposal
    assert restored.status == "pending"
    first = restored.payload.plan[0].lifts[0]
    assert isinstance(first.weight_kg, Decimal)
    assert first.pct_num == golden_draft.sessions[0].lifts[0].pct_num
    assert restored.payload.new_one_rms == {"squat": Decimal("147.5")}


def test_a_proposal_rejects_an_unknown_kind(golden_draft):
    with pytest.raises(pydantic.ValidationError):
        Proposal[StrengthPayload](
            block_id=uuid.uuid4(),
            kind="teleport",
            payload=StrengthPayload(plan=golden_draft.sessions),
            source="chat",
        )


# --- SessionChange --------------------------------------------------------


def test_a_create_needs_a_target_date():
    with pytest.raises(pydantic.ValidationError, match="to_date"):
        SessionChange(seq=1, week=1, slot=1, action="create")


def test_a_move_needs_both_dates():
    with pytest.raises(pydantic.ValidationError, match="from_date"):
        SessionChange(seq=1, week=1, slot=1, action="move", to_date=dt.date(2026, 3, 2))


def test_a_skip_needs_no_dates():
    assert SessionChange(seq=1, week=1, slot=1, action="skip").to_date is None


# --- One vocabulary, two places -------------------------------------------


def test_the_proposal_vocabularies_have_one_source_of_truth():
    """`models.Proposal`'s CHECK constraints derive from `enums`, which now
    re-exports these from `planner_core` rather than repeating them."""
    assert enums.ProposalKind is ProposalKind
    assert enums.ProposalStatus is ProposalStatus
    assert enums.ProposalSource is ProposalSource
    assert get_args(ProposalKind) == enums.PROPOSAL_KINDS


def test_the_strength_validator_satisfies_the_validator_protocol():
    assert isinstance(StrengthValidator(), Validator)


# --- The boundary (CLAUDE.md layout rule; spec 05 DoD) --------------------


def test_planner_core_imports_nothing_from_domains():
    package = Path(planner_core.__file__).resolve().parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name == "domains" or name.startswith("domains.")
            ]

    assert offenders == []
