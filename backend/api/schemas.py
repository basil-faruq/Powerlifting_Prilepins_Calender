"""Request and response bodies. Pydantic models, never SQLModel tables.

Three reasons this file exists rather than returning the tables directly:

1. **A `Literal` on a `table=True` class validates nothing at runtime**
   (ADR-0002), so the tables cannot be the validation layer for input. These
   models are, and the database CHECKs are the backstop.
2. **A table exposes columns a client has no business setting.** The clearest
   case is `one_rep_max.is_synthetic`: it is unrepresentable in `OneRepMaxIn`,
   so no request can set it, which is what keeps ADR-0004's single-writer rule
   true at the HTTP boundary. The response models carry it on every row, so a
   number derived from development data can never be shown as real (rule 4).
3. **Spec 16 generates TypeScript from the OpenAPI schema**, so the shapes here
   are the frontend's types. `Decimal` serialises as a JSON string, which is
   deliberate: kilograms and exact intensities must not round-trip through a
   float.

The domain's own non-table models (`BlockDraft`, `ValidationResult`,
`SessionChange`, `LoggedLift`) are reused as-is rather than mirrored. They are
already Pydantic, already frozen, and a copy of them here would be one more
thing to keep in step.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from domains.strength import enums, models, shaping_solver, simulator
from domains.strength.agent import ChatTurn, Route
from domains.strength.calendar_solver import SolverStats
from domains.strength.schemas import (
    BlockDraft,
    LoggedLift,
    MeasuredOneRm,
    NoteHit,
    SessionChange,
)
from planner_core.proposal import ValidationResult, Violation
from planner_core.tracing import trace_url

# --- Athlete --------------------------------------------------------------


class AthleteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    plate_increment_kg: Decimal | None = Field(default=None, gt=0)
    rounding: enums.Rounding | None = None


class AthletePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plate_increment_kg: Decimal | None = Field(default=None, gt=0)
    rounding: enums.Rounding | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


class AthleteOut(BaseModel):
    id: uuid.UUID
    name: str
    plate_increment_kg: Decimal
    rounding: enums.Rounding
    timezone: str

    @classmethod
    def of(cls, athlete: models.Athlete) -> AthleteOut:
        return cls(
            id=athlete.id,
            name=athlete.name,
            plate_increment_kg=athlete.plate_increment_kg,
            rounding=athlete.rounding,
            timezone=athlete.timezone,
        )


# The request body for `PUT /athletes/{id}/one-rep-maxes` is the domain's own
# `MeasuredOneRm`, re-exported rather than mirrored. It already excludes
# `synthetic` and has no `is_synthetic` field, requires `set_on`, and is what
# `record_one_rep_maxes` takes -- so a copy here would be one more thing to keep
# in step for no gain.
OneRepMaxIn = MeasuredOneRm


class OneRepMaxOut(BaseModel):
    id: uuid.UUID
    lift_code: str
    kg: Decimal
    set_on: dt.date
    source: enums.OneRepMaxSource
    # Never omitted and never optional. The authoritative "is this real?"
    # (ADR-0004 rule 1); specs 17 and 19 raise a banner from it.
    is_synthetic: bool
    block_id: uuid.UUID | None

    @classmethod
    def of(cls, row: models.OneRepMax) -> OneRepMaxOut:
        return cls(
            id=row.id,
            lift_code=row.lift_code,
            kg=row.kg,
            set_on=row.set_on,
            source=row.source,
            is_synthetic=row.is_synthetic,
            block_id=row.block_id,
        )


class WeekdaySlotOut(BaseModel):
    slot: int
    weekday: int
    locked_from: dt.date | None

    @classmethod
    def of(cls, row: models.WeekdayMap) -> WeekdaySlotOut:
        return cls(slot=row.slot, weekday=row.weekday, locked_from=row.locked_from)


class AthleteDetail(AthleteOut):
    """`GET /athletes/{id}`: the athlete, their current maxes and their days."""

    one_rep_maxes: list[OneRepMaxOut]
    weekday_map: list[WeekdaySlotOut]
    # True while any current max is development data. A flag at the top level
    # so the UI need not scan the rows to know whether to warn.
    has_synthetic_one_rms: bool


class WeekdayMapIn(BaseModel):
    """`{slots: {1: 0, 2: 2, 3: 4}}`, 0 = Monday.

    `template_id` and `lifts` are optional because spec 06 signs the body with
    `slots` alone, yet `check_weekday_map` needs to know which slots are
    actually needed -- a deadlift-only block needs Day 2 and nothing else. Left
    out, the check runs against the newest template and every lift it
    programmes, which is the strictest reading and the right default for a
    setup wizard (spec 17).
    """

    model_config = ConfigDict(extra="forbid")

    slots: dict[int, int]
    template_id: uuid.UUID | None = None
    lifts: list[enums.LiftCode] | None = None


class WeekdayMapOut(BaseModel):
    slots: list[WeekdaySlotOut]
    # Empty when the map is coherent. Populated on a dry run, and a populated
    # list on a real PUT is a 422 instead of this body.
    problems: list[str] = []


# --- Templates ------------------------------------------------------------


class TemplateEntryOut(BaseModel):
    lift_code: str
    variation: str | None
    week: int
    slot: int
    sets: int
    reps: int
    # The authored pair, unreduced, so the athlete is shown the `70/120` they
    # wrote rather than a decimal that has lost it (ADR-0003 rule 5).
    pct_num: Decimal
    pct_den: Decimal
    is_test: bool

    @classmethod
    def of(cls, row: models.TemplateEntry) -> TemplateEntryOut:
        return cls(
            lift_code=row.lift_code,
            variation=row.variation,
            week=row.week,
            slot=row.slot,
            sets=row.sets,
            reps=row.reps,
            pct_num=row.pct_num,
            pct_den=row.pct_den,
            is_test=row.is_test,
        )


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    weeks: int
    slots: int
    overreach_from_week: int
    source_file: str | None
    entries: list[TemplateEntryOut]


class TemplateImportOut(BaseModel):
    template_id: uuid.UUID
    entries: int


# --- Blocks ---------------------------------------------------------------


class BlockCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    athlete_id: uuid.UUID
    template_id: uuid.UUID
    lifts: list[enums.LiftCode] = Field(min_length=1)
    start_date: dt.date


class BlockPreviewOut(BaseModel):
    """The plan and the verdict, written nowhere.

    Carries `has_synthetic_one_rms` for the same reason `AthleteDetail` does:
    every weight below was computed from the snapshot, so if the snapshot is
    development data the whole preview is (ADR-0004 rule 4).
    """

    draft: BlockDraft
    validation: ValidationResult
    has_synthetic_one_rms: bool


class ShapeCreate(BlockCreate):
    """A block the template does not cover: a different length, a moved test.

    `weeks` outside 6-14 is a 422 from the field bound alone, which is what
    spec 09 §13 asks for. Left out, the block takes the template's own length
    and the solver reproduces it.
    """

    model_config = ConfigDict(extra="forbid")

    weeks: int | None = Field(
        default=None, ge=shaping_solver.MIN_WEEKS, le=shaping_solver.MAX_WEEKS
    )
    # {lift: week}. Moving a test moves the flag and extends that lift's ramp
    # to reach it.
    test_week: dict[enums.LiftCode, int] | None = None


class ShapedPreviewOut(BlockPreviewOut):
    """A shaped preview: the plan, the verdict, and what the solver did.

    The solver fields are not decoration -- spec 09 §14 wants them for spec
    14's trace, and `cost` is how a reader sees that a default shaping
    deviated from the template by nothing at all.
    """

    weeks: int
    test_week: dict[str, int]
    overreach_from_week: int
    cost: shaping_solver.ShapingCost
    stats: SolverStats


class SimulateCreate(BaseModel):
    """A what-if over the rest of a running block (spec 10).

    `runs` above 5000 is a 422 from the field bound alone, which is what spec 10
    §13 asks for. The seed defaults to 0 rather than to the clock: the same
    request is the same answer, and the seed used is echoed in `stats` either
    way.
    """

    model_config = ConfigDict(extra="forbid")

    scenario: list[simulator.ForcedEvent] = Field(default_factory=list, max_length=20)
    runs: int = Field(default=simulator.DEFAULT_SIM_CONFIG.runs, ge=1, le=5000)
    # Non-negative: numpy refuses a negative seed. Below 2**53 so a JavaScript
    # client can send back exactly the number it was shown in `stats.seed`.
    seed: int = Field(default=0, ge=0, le=2**53)


class SimulationOut(simulator.SimulationResult):
    """The simulator's ranges, plus where the maxes under them came from.

    `has_synthetic_one_rms` is the same flag every plan-derived response
    carries (ADR-0007 rule 5): a projection of a development max is a
    projection of nothing real, and must say so.
    """

    block_id: uuid.UUID
    has_synthetic_one_rms: bool


class BlockOut(BaseModel):
    id: uuid.UUID
    athlete_id: uuid.UUID
    template_id: uuid.UUID
    number: int
    start_date: dt.date
    status: enums.BlockStatus
    lifts: list[str]
    # Read back as exact decimals, though JSONB holds them as strings.
    one_rm_snapshot: dict[str, Decimal]
    workflow_id: str | None

    @classmethod
    def of(cls, block: models.Block) -> BlockOut:
        return cls(
            id=block.id,
            athlete_id=block.athlete_id,
            template_id=block.template_id,
            number=block.number,
            start_date=block.start_date,
            status=block.status,
            lifts=list(block.lifts),
            one_rm_snapshot={lift: Decimal(str(kg)) for lift, kg in block.one_rm_snapshot.items()},
            workflow_id=block.workflow_id,
        )


class UnavailableIn(BaseModel):
    """ "I cannot train between these dates."

    `from` rather than `from_date` on the wire: it reads as the athlete would
    say it, and `from` is a Python keyword only on this side of the alias.
    An open `until` means "still away", which the policy closes at today.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_date: dt.date = Field(alias="from")
    until: dt.date | None = None
    # Always a restart, however short (ADR-0010), and the proposal carries
    # the line about seeing a physio.
    injury: bool = False


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ResumeIn(BaseModel):
    """Back to training. Whatever was due in between is treated as an absence."""

    model_config = ConfigDict(extra="forbid")

    on_date: dt.date | None = None


class WorkflowSessionOut(BaseModel):
    id: uuid.UUID
    seq: int
    week: int
    slot: int
    date: dt.date
    status: enums.PlannedSessionStatus


class BlockStateOut(BaseModel):
    """Where the block's workflow has got to.

    `state = "none"` is a block whose workflow was never started -- a draft
    awaiting its first approval -- and it is a 200, not a 404. That is a real
    and expected condition, and making the UI branch on an error to render
    "not started yet" would be a poor trade.

    `error` is set when an activity failed after its retries. The workflow
    holds its state rather than dying, so this is a thing a human can read
    and then fix (spec 15 §13).
    """

    state: str
    block_id: uuid.UUID
    next_session: WorkflowSessionOut | None = None
    pending_proposal_ids: list[uuid.UUID] = Field(default_factory=list)
    tier: enums.MissedTier | None = None
    error: str | None = None

    @classmethod
    def none(cls, block_id: uuid.UUID) -> BlockStateOut:
        return cls(state="none", block_id=block_id)

    @classmethod
    def of(cls, view) -> BlockStateOut:
        return cls(
            state=view.state,
            block_id=view.block_id,
            next_session=(
                WorkflowSessionOut(
                    id=view.next_session.id,
                    seq=view.next_session.seq,
                    week=view.next_session.week,
                    slot=view.next_session.slot,
                    date=view.next_session.date,
                    status=view.next_session.status,
                )
                if view.next_session is not None
                else None
            ),
            pending_proposal_ids=list(view.pending_proposal_ids),
            tier=view.tier,
            error=view.error,
        )


class PlannedLiftOut(BaseModel):
    lift_code: str
    variation: str | None
    sets: int
    reps: int
    pct_num: Decimal
    pct_den: Decimal
    weight_kg: Decimal
    rel_intensity: Decimal
    is_test: bool

    @classmethod
    def of(cls, row: models.PlannedLift) -> PlannedLiftOut:
        return cls(
            lift_code=row.lift_code,
            variation=row.variation,
            sets=row.sets,
            reps=row.reps,
            pct_num=row.pct_num,
            pct_den=row.pct_den,
            weight_kg=row.weight_kg,
            rel_intensity=row.rel_intensity,
            is_test=row.is_test,
        )


class SessionOut(BaseModel):
    id: uuid.UUID
    block_id: uuid.UUID
    week: int
    slot: int
    seq: int
    date: dt.date
    # What the block was first planned with. Unchanged by a reschedule, so a
    # moved session still shows where it came from.
    original_date: dt.date
    status: enums.PlannedSessionStatus
    lifts: list[PlannedLiftOut]


class SessionLogIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    performed_on: dt.date
    entries: list[LoggedLift] = Field(min_length=1)
    note: str | None = None


class SessionLogOut(BaseModel):
    id: uuid.UUID
    planned_session_id: uuid.UUID
    performed_on: dt.date
    entries: list[LoggedLift]
    note: str | None

    @classmethod
    def of(cls, log: models.SessionLog) -> SessionLogOut:
        return cls(
            id=log.id,
            planned_session_id=log.planned_session_id,
            performed_on=log.performed_on,
            entries=[LoggedLift.model_validate(entry) for entry in log.entries],
            note=log.note,
        )


class SessionDetail(SessionOut):
    log: SessionLogOut | None


# --- Proposals ------------------------------------------------------------


class ProposalOut(BaseModel):
    id: uuid.UUID
    block_id: uuid.UUID
    kind: enums.ProposalKind
    status: enums.ProposalStatus
    # The payload stays a plain object: its shape depends on `kind`
    # (`StrengthPayload`, or `WeekdayChangePayload` with its `slots`), and a
    # union here would generate a frontend type that has to be narrowed before
    # it can be read.
    payload: dict
    cost: dict
    # Why the solver proposed this.
    reasons: list[str]
    warnings: list[Violation]
    source: enums.ProposalSource
    decided_at: dt.datetime | None
    # Why the human decided as they did. Null unless a reject gave a reason.
    decision_reason: str | None

    @classmethod
    def of(cls, proposal: models.Proposal) -> ProposalOut:
        return cls(
            id=proposal.id,
            block_id=proposal.block_id,
            kind=proposal.kind,
            status=proposal.status,
            payload=proposal.payload,
            cost=proposal.cost,
            reasons=[str(reason) for reason in proposal.reasons],
            warnings=[Violation.model_validate(w) for w in proposal.warnings],
            source=proposal.source,
            decided_at=proposal.decided_at,
            decision_reason=proposal.decision_reason,
        )


class BlockCreated(BaseModel):
    block_id: uuid.UUID
    proposal: ProposalOut


class ProposalReject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class ProposalEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[SessionChange] = Field(min_length=1)


# --- Notes ----------------------------------------------------------------


class NoteHitOut(BaseModel):
    """One retrieved note.

    `is_synthetic` is on the wire deliberately. ADR-0004 makes the flag the
    authoritative "is this real?" and requires the UI to surface it while any
    row has it set -- a seeded demo note must never read as something the
    athlete wrote. Spec 12 §3's field list omits it; ADR-0007 rule 5 governs.

    `distance` is cosine distance, 0 identical and 1 unrelated, exactly as
    pgvector returned it. Nothing rescales it into a "relevance" score: the
    explainer may only repeat numbers it was given.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    noted_on: dt.date
    lift_codes: list[str] = []
    distance: float
    is_synthetic: bool = False

    @classmethod
    def of(cls, hit: NoteHit) -> NoteHitOut:
        return cls(
            text=hit.text,
            noted_on=hit.noted_on,
            lift_codes=list(hit.lift_codes),
            distance=hit.distance,
            is_synthetic=hit.is_synthetic,
        )


class ReindexOut(BaseModel):
    """How many chunks exist after a re-embed."""

    model_config = ConfigDict(extra="forbid")

    reindexed: int


# --- Chat (spec 13) -------------------------------------------------------

# Stripped before the length check, so a message of spaces is as empty as "" and
# both come back as FastAPI's own 422 -- the same trap `routes/notes.py`
# documents, and it must be used as `Annotated[ChatText, ...]`.
ChatText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class ChatCreate(BaseModel):
    """One message from the athlete."""

    model_config = ConfigDict(extra="forbid")

    athlete_id: uuid.UUID
    # Absent starts a new conversation. A conversation that belongs to another
    # athlete is a 404: `chat_message.conversation_id` is not a foreign key, so
    # any UUID is syntactically valid and the ownership check is load-bearing.
    conversation_id: uuid.UUID | None = None
    message: ChatText
    # Which template to plan against, when the athlete has more than one and
    # the message does not say. Resolved by the agent when absent.
    template_id: uuid.UUID | None = None


class ChatResponse(BaseModel):
    """What one turn produced: a reply, the route, and anything to approve."""

    model_config = ConfigDict(extra="forbid")

    reply: str
    route: Route
    # Pending, for the approval cards. The agent cannot approve (spec 13 §11).
    proposals: list[ProposalOut] = []
    simulation: SimulationOut | None = None
    decision_id: uuid.UUID
    conversation_id: uuid.UUID
    # ADR-0007 rule 5 / ADR-0004 rule 4: any response showing a number derived
    # from a seeded max says so. Spec 13 §3 omits it, which ADR-0015 records.
    has_synthetic_one_rms: bool = False
    # Null when tracing is off, which is the normal local case (spec 14 §12).
    trace_url: str | None = None

    @classmethod
    def of(cls, turn: ChatTurn, proposals: Sequence[models.Proposal]) -> ChatResponse:
        simulation = None
        if turn.simulation is not None:
            simulation = SimulationOut(
                **turn.simulation.result.model_dump(),
                block_id=turn.simulation.block_id,
                has_synthetic_one_rms=turn.simulation.has_synthetic_one_rms,
            )
        return cls(
            reply=turn.reply,
            route=turn.route,
            proposals=[ProposalOut.of(proposal) for proposal in proposals],
            simulation=simulation,
            decision_id=turn.decision_id,
            conversation_id=turn.conversation_id,
            has_synthetic_one_rms=turn.has_synthetic_one_rms,
            trace_url=trace_url(turn.trace_id),
        )


class ChatMessageOut(BaseModel):
    """One stored message, for the conversation history."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    role: enums.ChatRole
    content: str
    created_at: dt.datetime
    decision_id: uuid.UUID | None

    @classmethod
    def of(cls, row: models.ChatMessage) -> ChatMessageOut:
        return cls(
            id=row.id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
            decision_id=row.decision_id,
        )


# --- Decisions (spec 14) --------------------------------------------------


class DecisionOut(BaseModel):
    """One decision, with its trace link and the turn that caused it.

    `athlete_id` and `message` are joined from the `chat_message` row rather
    than stored on `decision`: neither has a column, and this pair of specs
    takes no migration (D1, ADR-0015). Both are null for a workflow decision,
    which has no chat message -- reach those with `source=workflow`.

    `outcome` null means pending: a proposal is waiting on the athlete. That is
    what spec 21 renders as `pending`, with no new enum value.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: str
    source: enums.ProposalSource
    route: enums.DecisionRoute
    filter_fired: str | None
    outcome: enums.DecisionOutcome | None
    proposal_id: uuid.UUID | None
    created_at: dt.datetime
    athlete_id: uuid.UUID | None = None
    message: str | None = None
    tool_calls: list[dict] = []
    trace_url: str | None = None

    @classmethod
    def of(
        cls,
        row: models.Decision,
        *,
        athlete_id: uuid.UUID | None = None,
        message: str | None = None,
    ) -> DecisionOut:
        return cls(
            id=row.id,
            run_id=row.run_id,
            source=row.source,
            route=row.route,
            filter_fired=row.filter_fired,
            outcome=row.outcome,
            proposal_id=row.proposal_id,
            created_at=row.created_at,
            athlete_id=athlete_id,
            message=message,
            tool_calls=[dict(call) for call in row.tool_calls],
            trace_url=trace_url(row.langfuse_trace_id),
        )


# --- Errors ---------------------------------------------------------------


class ProblemOut(BaseModel):
    """The body of a 4xx. One shape for every refusal this API makes.

    `detail` so a generic client finds the message where it expects it;
    `violations` and `problems` so a specific one can show them against the
    sessions they name (spec 06 §13).
    """

    detail: str
    violations: list[Violation] = []
    warnings: list[Violation] = []
    problems: list[str] = []
