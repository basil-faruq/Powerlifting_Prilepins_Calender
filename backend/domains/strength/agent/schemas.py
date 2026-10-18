"""The shapes the agent passes around: the route, the tool outcomes, the trace.

Spec 13. A leaf module -- it imports from the domain and from `planner_core`,
and nothing in `agent/` imports it back except to use these types. `Route` is
read by `router.py`, `agent.py`, `api/schemas.py` and `scripts/eval_router.py`,
so it cannot live in any of them without making one import another for a type
alone.

Two deliberate departures from spec 13 §5B, both recorded in ADR-0015.

**`RouterChoice` is what the model may return; `Route` is what gets stored.**
The spec describes one `Route` with four kinds, but the persisted column allows
six -- `safety` and `clarify` are the deterministic filters' outcomes
(CLAUDE.md non-negotiable 3). Splitting them means the LLM's output type cannot
express "this is a safety matter": only Python can decide that, which is the
whole point of running the filters first.

**`RouterChoice` is flat.** The spec has `scenario: list[ForcedEvent]`, but
`ForcedEvent` requires *exactly one* of `start_week` and `start_date` -- a
cross-field invariant, which is the least reliable thing to ask a 14b model
for, and every violation costs a retry round trip. Four scalar fields and a
Python-side `to_forced_events` that returns `None` when they do not form a
valid event is strictly better: the failure becomes a `clarify` instead of a
retry. One event per turn covers every example in spec 13 §12.

Tool outcomes carry **ids, not rows**. That keeps them dumpable to JSON for the
explainer and for `decision.tool_calls`, and it satisfies spec 14 §10's "never
full DB rows in span attributes". `agent.py` reloads the rows once, for the
HTTP response.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models import Model

from domains.strength import enums, missed_policy, simulator
from domains.strength.calendar_solver import SolverStats
from domains.strength.schemas import DraftSession, NoteHit
from planner_core.proposal import Violation

# Spec 13 §10. Older messages are dropped; there is no summarisation in v1.
HISTORY_LIMIT = 10


@dataclass(frozen=True, slots=True)
class ChatModels:
    """The two roles one turn needs, resolved once per request.

    A frozen dataclass rather than a `BaseModel` so nothing tries to validate
    a `Model` into it. It lives here rather than in `api/deps.py` because
    `agent.py` needs the type and must not import from `api` -- the dependency
    that provides it is the thin half, so the type goes in the leaf.
    """

    router: Model
    agent: Model


# --- The route ------------------------------------------------------------


class RouterChoice(BaseModel):
    """What layer 2's LLM is allowed to return.

    `extra="forbid"` rather than ignore: a model that invents a field has
    misunderstood the schema, and a retry with the error is more likely to
    produce something usable than silently dropping it.

    Every field is optional except `kind`, because the router's job is to pick
    a route and extract whatever parameters the sentence happened to contain.
    A missing parameter is a `clarify`, decided in Python, not a model error.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer", "plan", "reschedule", "simulate"]

    # Bounded so a runaway generation cannot become a 500-lift plan request.
    # Five lifts exist; eight leaves room for a duplicate or a near-miss name
    # that Python will then reject by name.
    lifts: list[str] = Field(default_factory=list, max_length=8)

    start_date: dt.date | None = None

    # `shaping_solver` accepts 6..14; the wider bound here lets a request for
    # 20 weeks reach Python and be refused with a sentence rather than a
    # schema error the athlete never sees.
    weeks: int | None = Field(default=None, ge=3, le=20)

    # A test week is one lift in one week. Two scalars rather than a mapping:
    # a dict with a lift-code key is another structured-output trap, and one
    # pair is all a sentence ever says.
    test_week_lift: str | None = None
    test_week_number: int | None = Field(default=None, ge=1)

    missed_dates: list[dt.date] = Field(default_factory=list, max_length=30)
    unavailable_from: dt.date | None = None
    unavailable_until: dt.date | None = None
    injury: bool = False

    # One forced event, flattened. See the module docstring.
    scenario_kind: Literal["illness", "travel"] | None = None
    scenario_start_week: int | None = Field(default=None, ge=1)
    scenario_start_date: dt.date | None = None
    scenario_days: int | None = Field(default=None, ge=1, le=90)

    # What the athlete actually asked, for the `answer` route to work from.
    question: str | None = None


class Route(BaseModel):
    """The decision after both layers, and what `decision.route` is written from.

    `kind` is the full six-value column vocabulary, not the LLM's four.
    `filter_fired` is non-null exactly when a deterministic filter decided,
    which is what makes "no LLM was called" checkable after the fact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: enums.DecisionRoute
    filter_fired: str | None = None
    # None for `safety` and `clarify`: no model was asked, or its answer was
    # unusable.
    choice: RouterChoice | None = None
    clarify_reason: str | None = None
    # Lift codes resolved against the register, so a tool never sees a name
    # the domain does not know.
    lifts: tuple[str, ...] = ()
    # Set when the model's structured output was unusable twice. Drives
    # `outcome = "failed"` while the route itself stays `clarify` -- there is no
    # "unknown" value in the column and D1 forbids a migration to add one.
    unparseable: bool = False


def to_forced_events(choice: RouterChoice) -> tuple[simulator.ForcedEvent, ...] | None:
    """Build the simulator's scenario from the router's flat fields.

    `None` means the fields do not form a valid event -- no kind, no duration,
    neither start or both -- and the caller turns that into a `clarify` rather
    than a retry. An empty tuple is not the same thing: it is a `simulate` with
    no disruption, which is a legitimate "how is this block going to go?".
    """
    fields = (
        choice.scenario_kind,
        choice.scenario_days,
        choice.scenario_start_week,
        choice.scenario_start_date,
    )
    if all(field is None for field in fields):
        return ()

    if choice.scenario_kind is None or choice.scenario_days is None:
        return None
    # `ForcedEvent` requires exactly one start. Checked here so the failure is
    # a sentence to the athlete, not a `ValidationError` from a model call.
    if (choice.scenario_start_week is None) == (choice.scenario_start_date is None):
        return None

    try:
        event = simulator.ForcedEvent(
            kind=choice.scenario_kind,
            start_week=choice.scenario_start_week,
            start_date=choice.scenario_start_date,
            days=choice.scenario_days,
        )
    except ValueError:
        return None
    return (event,)


# --- The trace entry ------------------------------------------------------


class ToolCall(BaseModel):
    """One entry in `decision.tool_calls`, as spec 13 §5A specifies it.

    Also what spec 21's decision drawer renders, and -- for the first entry,
    which is always the router -- where spec 21 reads the extracted route
    parameters from, since no column holds them (ADR-0015, D1).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = Field(ge=0)
    solver_stats: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    # Set when the tool raised. The entry is still recorded: a turn that failed
    # halfway is exactly the one worth being able to read afterwards.
    error: str | None = None


# --- Tool outcomes --------------------------------------------------------


class OneRmOut(BaseModel):
    """One current max, with its provenance.

    `is_synthetic` is not optional here. ADR-0004 rule 4 requires any number
    derived from a seeded max to say so, and this is where a chat reply would
    otherwise quote 125 kg as though the athlete had tested it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: str
    kg: Decimal
    set_on: dt.date
    source: str
    is_synthetic: bool


class BlockSnapshot(BaseModel):
    """What `get_block` returns: enough to answer a question without a solver.

    Every field is nullable because "no block yet" is a normal state, not an
    error -- an athlete asking "what should I do?" before planning anything is
    the first thing that will ever happen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    template_name: str | None = None
    status: str | None = None
    start_date: dt.date | None = None
    # The template week the athlete is in now, 1-based.
    week: int | None = None
    template_weeks: int | None = None
    next_sessions: tuple[DraftSession, ...] = ()
    one_rms: tuple[OneRmOut, ...] = ()
    has_synthetic_one_rms: bool = False
    # Sessions whose date has passed, that are neither done nor written off,
    # and that no approved reschedule has moved. See `tools.get_block` for why
    # that -- and not the `missed` status -- is the count (ADR-0010).
    missed_seqs: tuple[int, ...] = ()
    weekday_map: dict[int, int] = Field(default_factory=dict)


class PlanOutcome(BaseModel):
    """What `plan_block` did: a pending proposal, a preview, or a refusal.

    Three outcomes in one shape because the explainer has to narrate whichever
    happened, and a tool that raised for two of them would make the common
    case the only one with a reply.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: uuid.UUID | None = None
    block_id: uuid.UUID | None = None
    session_count: int = 0
    reasons: tuple[str, ...] = ()
    warnings: tuple[Violation, ...] = ()
    # Non-empty when the validator refused. No proposal was written.
    violations: tuple[Violation, ...] = ()
    # True when `weeks` or `test_week` was asked for. ADR-0011 leaves proposing
    # a *shaped* block deliberately undone, so this is a preview and the
    # athlete has nothing to approve.
    preview_only: bool = False
    has_synthetic_one_rms: bool = False


class RescheduleOutcome(BaseModel):
    """What the missed-session policy decided, and the rows it left behind.

    `proposal_ids` is 0..3: only the `in_week_move` tier offers alternatives;
    `week_shift` and `fold` give one, and `restart` gives one `new_block`
    against a block that is already persisted as a draft (ADR-0010). Spec 13
    §5A's flat "1-3 reschedule proposals" is wrong on both counts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: missed_policy.Tier
    proposal_ids: tuple[uuid.UUID, ...] = ()
    kinds: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    warnings: tuple[Violation, ...] = ()
    costs: tuple[dict[str, float], ...] = ()
    failure: missed_policy.PolicyFailed | None = None
    stats: SolverStats | None = None
    has_synthetic_one_rms: bool = False


class SimulateOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: uuid.UUID
    result: simulator.SimulationResult
    has_synthetic_one_rms: bool


class LogOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    log_id: uuid.UUID
    session_id: uuid.UUID
    seq: int
    lifts_logged: int


class NoteSearchOutcome(BaseModel):
    """`search_notes`' hits, wrapped so the summary has somewhere to live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    hits: tuple[NoteHit, ...] = ()
