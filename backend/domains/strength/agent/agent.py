"""One chat turn: route, dispatch in plain Python, explain, record.

Spec 13 §5C. `match route.kind` and nothing cleverer -- the LLM picks a route
and Python calls the solver, so the model never loops over one. That is the
difference between an agent that can be audited and one that can be surprising.

**The write path.** Exactly one `decision` row per turn, including every
failure path (spec 13 DoD). The row is created after routing and before
dispatch, because `decision.route` is NOT NULL with a CHECK constraint and
there is no "unknown" value to insert first -- D1 takes no migration. Both
`chat_message` rows carry `decision_id`, which is how `GET /decisions` finds
the athlete and the input message without a column for either.

**Nothing here commits** (ADR-0007 rule 2). The route owns the transaction, so
spec 15 can call this from an activity that owns its own.

**The 503 has to leave its row behind.** `LlmUnavailable` is caught, written as
`outcome = "failed"`, and returned on `ChatTurn.failure` rather than raised --
because a service may not commit, and an exception thrown from here would roll
the row back on its way to the handler. The route commits, then raises. See
`handle_message`.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import sqlalchemy as sa
from pydantic_ai import Agent, ModelRetry, UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from sqlmodel import Session

from domains.strength import enums, models, services
from domains.strength.agent import explainer, prompts, recorder, router, tools
from domains.strength.agent.schemas import (
    HISTORY_LIMIT,
    BlockSnapshot,
    ChatModels,
    PlanOutcome,
    Route,
    SimulateOutcome,
    to_forced_events,
)
from planner_core.errors import LlmUnavailable, NotFound, PlanInvalid
from planner_core.llm import Embedder, unavailable_from
from planner_core.tracing import traced

logger = logging.getLogger(__name__)

# What a chat turn's `run_id` is. One run per *conversation*, not per turn: for
# spec 15 the natural value is the Temporal workflow run id -- one run, one
# execution -- and a conversation is the chat analogue. Per-turn would make an
# indexed column a strictly worse duplicate of the primary key, and "every
# decision in this run" would stop being a useful question (ADR-0015).
RUN_PREFIX = "chat"


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """Everything the route needs to answer, and the row ids it wrote.

    A dataclass rather than a `BaseModel` because `failure` holds an exception
    instance, which Pydantic has no business validating.
    """

    reply: str
    route: Route
    decision_id: uuid.UUID
    conversation_id: uuid.UUID
    proposal_ids: tuple[uuid.UUID, ...] = ()
    # The whole outcome, not just the result: the response needs the block id
    # too, and a simulate turn has no proposal to read it from.
    simulation: SimulateOutcome | None = None
    has_synthetic_one_rms: bool = False
    trace_id: str | None = None
    # Deferred rather than raised, so the route can commit the failed decision
    # row before the 503 goes out.
    failure: LlmUnavailable | None = None


@dataclass
class _Dispatch:
    """What a route produced, before it becomes a row and a reply."""

    reply: str
    proposal_ids: tuple[uuid.UUID, ...] = ()
    simulation: SimulateOutcome | None = None
    has_synthetic_one_rms: bool = False
    # None means "a proposal is waiting on you" (D1: `outcome IS NULL`).
    outcome: enums.DecisionOutcome | None = "answered"
    extra: dict[str, object] = field(default_factory=dict)


# --- Reading the conversation -------------------------------------------


# Within one turn the two messages tie on `created_at`, because
# `TimestampMixin`'s server default is `now()` and Postgres' `now()` is the
# *transaction* start time -- constant for both inserts. So the role breaks the
# tie: a turn is one question and one reply, and the question came first.
#
# Without this the reply can sort before the question, which inverts the
# transcript the router is shown and the history the UI renders. `id` is no use
# as a tie-break: it is a random UUID, not an insertion order. Ordering across
# turns is still by `created_at`, since each turn is its own transaction.
_ROLE_ORDER = sa.case((models.ChatMessage.role == "user", 0), else_=1)


def _history(session: Session, conversation_id: uuid.UUID) -> list[models.ChatMessage]:
    """The last `HISTORY_LIMIT` messages, oldest first (spec 13 §10).

    Served by `ix_chat_message_conversation_id_created_at`, which spec 02 added
    for exactly this read. No summarisation in v1: older turns are dropped.
    """
    rows = list(
        session.scalars(
            sa.select(models.ChatMessage)
            .where(models.ChatMessage.conversation_id == conversation_id)
            .order_by(models.ChatMessage.created_at.desc(), _ROLE_ORDER.desc())
            .limit(HISTORY_LIMIT)
        )
    )
    return list(reversed(rows))


def _check_conversation(
    session: Session, conversation_id: uuid.UUID, athlete_id: uuid.UUID
) -> None:
    """Refuse a conversation that belongs to someone else.

    `chat_message.conversation_id` is not a foreign key and there is no
    conversation table (spec 02 §N), so any UUID is a syntactically valid
    conversation. Without this check, passing a guessed id would append to --
    and read back -- another athlete's conversation. Spec 13 does not mention
    it; single-athlete today is a decision, not a reason to leave the hole.
    """
    other = session.scalar(
        sa.select(models.ChatMessage.athlete_id)
        .where(models.ChatMessage.conversation_id == conversation_id)
        .limit(1)
    )
    if other is not None and other != athlete_id:
        raise NotFound("conversation", conversation_id)


def _resolve_template(
    session: Session, athlete_id: uuid.UUID, template_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Which template to plan against. `None` means ask.

    Spec 13 never says where this comes from, and no message supplies it. The
    order is: what the caller passed; the template the athlete's most recent
    block used; the only one in the database. Picking the newest template
    silently would plan against something nobody asked for.
    """
    if template_id is not None:
        return template_id

    recent = session.scalar(
        sa.select(models.Block.template_id)
        .where(models.Block.athlete_id == athlete_id)
        .order_by(models.Block.created_at.desc())
        .limit(1)
    )
    if recent is not None:
        return recent

    templates = list(session.scalars(sa.select(models.BlockTemplate.id).limit(2)))
    return templates[0] if len(templates) == 1 else None


# --- Dispatch ------------------------------------------------------------


def _safety_reply(route: Route) -> _Dispatch:
    """The fixed reply. No model, by design.

    A reply about pain is the last place to let a model improvise, and it has
    to say the same thing every time so the athlete learns what it means.
    """
    if route.lifts:
        named = " and ".join(route.lifts)
        return _Dispatch(reply=f"{prompts.SAFETY_REPLY} (About your {named}.)")
    return _Dispatch(reply=prompts.SAFETY_REPLY)


CLARIFY_REPLIES = {
    "no_one_rms": prompts.CLARIFY_ONE_RMS,
    "no_weekday_map": prompts.CLARIFY_WEEKDAYS,
    "no_template": prompts.CLARIFY_TEMPLATE,
    "unparseable": prompts.CLARIFY_UNPARSEABLE,
    "unknown_lift": prompts.CLARIFY_UNKNOWN_LIFT,
    "date_out_of_range": prompts.CLARIFY_DATE_OUT_OF_RANGE,
    "no_block": prompts.CLARIFY_NO_BLOCK,
    "bad_scenario": prompts.CLARIFY_SCENARIO,
    "no_lifts": prompts.CLARIFY_NO_LIFTS,
    "no_start_date": prompts.CLARIFY_NO_START_DATE,
    "no_window": prompts.CLARIFY_NO_WINDOW,
    "window_reversed": prompts.CLARIFY_WINDOW_REVERSED,
}


def _clarify_reply(route: Route) -> _Dispatch:
    reply = CLARIFY_REPLIES.get(route.clarify_reason or "", prompts.CLARIFY_UNPARSEABLE)
    # `unparseable` is the model having failed twice, which spec 13 §11 calls a
    # failed turn -- and the route column has no value for it, so the outcome
    # is what distinguishes it from an ordinary question (ADR-0015).
    outcome: enums.DecisionOutcome = "failed" if route.unparseable else "answered"
    return _Dispatch(reply=reply, outcome=outcome)


# The answer agent may make this many model requests in a turn. A tool call and
# its follow-up is two; the cap exists so a model that loops on a tool cannot
# make fifty database calls inside one request.
ANSWER_REQUEST_LIMIT = 6


def _answer_fallback(snapshot: BlockSnapshot) -> str:
    """What to say when the model's own answer could not be trusted.

    Built from the snapshot alone, so every number in it is one the database
    produced. Plain on purpose: the athlete asked a question and the model's
    reply was thrown away, so the useful thing left is the facts.
    """
    if snapshot.block_id is None:
        return prompts.ANSWER_NO_BLOCK
    parts = [
        f"You're in week {snapshot.week} of {snapshot.template_weeks} of {snapshot.template_name}."
    ]
    if snapshot.next_sessions:
        dates = ", ".join(session.date.isoformat() for session in snapshot.next_sessions)
        parts.append(f"Your next sessions are on {dates}.")
    if snapshot.missed_seqs:
        parts.append(f"{len(snapshot.missed_seqs)} past session(s) are still unlogged.")
    if snapshot.has_synthetic_one_rms:
        parts.append("Your maxes are development placeholders, not tested numbers.")
    return " ".join(parts)


def _answer(
    session: Session,
    athlete: models.Athlete,
    route: Route,
    *,
    model: Model,
    embedder: Embedder,
    ctx: router.RouterContext,
) -> _Dispatch:
    """An agent with only the two read-only tools (spec 13 §5C).

    The closures bind `session` and `athlete`, which the model must not see.
    They carry no recording or tracing logic of their own: `tools.get_block`
    is already decorated, so a call looks identical in the trace whether the
    model or `match route.kind` reached it.

    Four things here came from review, each a real hole:

    - **The reply is number-checked.** This route's only guard was a sentence
      in the prompt asking for no invented numbers -- the "prompt asking
      nicely" CLAUDE.md non-negotiable 1 rejects. It now goes through the same
      `verify` as every other reply, against what the tools actually returned.
    - **Tools run `sequential`.** Pydantic AI runs one response's tool calls
      concurrently, each sync tool in its own thread; two of them on one
      SQLAlchemy `Session` is "concurrent operations are not permitted".
    - **Arguments the model chose are validated.** A blank query raised
      `ValueError`, which is a 500 and loses the turn. `ModelRetry` hands the
      model the error instead.
    - **Notes are quoted data inside the tool result.** `DATA_RULE` covers a
      block marked as data; a note arriving as bare JSON was covered by nothing.
    """
    answer_agent: Agent[None, str] = Agent(
        model,
        instructions=prompts.answer_instructions(today=ctx.today, weekdays=ctx.weekdays),
    )
    snapshots: list[BlockSnapshot] = []
    # Everything a tool returned, as JSON: what the reply is allowed to quote.
    returned: list[dict[str, object]] = []

    @answer_agent.tool_plain(sequential=True)
    def get_block() -> BlockSnapshot:
        """The current block, the next three sessions, and the current 1RMs."""
        out = tools.get_block(session, athlete)
        snapshots.append(out)
        returned.append(out.model_dump(mode="json"))
        return out

    @answer_agent.tool_plain(sequential=True)
    def search_notes(
        query: str, lift: str | None = None, since: dt.date | None = None
    ) -> list[dict[str, object]]:
        """Search the athlete's own training notes by meaning.

        Args:
            query: What to look for, in the athlete's own words.
            lift: Restrict to one lift code, if the question names one.
            since: Only notes from this date onward.
        """
        if not query.strip():
            raise ModelRetry("query must not be empty; say what to look for")
        if lift is not None and lift not in enums.LIFT_CODES:
            raise ModelRetry(f"lift must be one of {', '.join(enums.LIFT_CODES)}")

        out = tools.search_notes(session, athlete, query, embedder, lift=lift, since=since)
        returned.append(out.model_dump(mode="json"))
        return [
            {
                "noted_on": hit.noted_on.isoformat(),
                "lifts": list(hit.lift_codes),
                "text": prompts.quoted("note", hit.text),
            }
            for hit in out.hits
        ]

    question = (route.choice.question if route.choice else None) or ""
    try:
        reply = answer_agent.run_sync(
            prompts.quoted("message", question),
            usage_limits=UsageLimits(request_limit=ANSWER_REQUEST_LIMIT),
        ).output
    except (UnexpectedModelBehavior, UsageLimitExceeded):
        # The model misbehaved rather than being down (a dead model is
        # translated and raised in `_dispatch`). The facts are still ours to
        # give, so the turn is answered rather than lost.
        logger.warning("answer agent misbehaved; using the snapshot", exc_info=True)
        reply = ""

    # A model that never called `get_block` still needs one for the fallback and
    # for the synthetic flag. Reused if it did, so the trace has one entry.
    snapshot = snapshots[-1] if snapshots else tools.get_block(session, athlete)
    if not snapshots:
        returned.append(snapshot.model_dump(mode="json"))

    verdict = explainer.verify(reply, returned, fallback=_answer_fallback(snapshot))
    return _Dispatch(
        reply=verdict.text,
        # Either the block's own snapshot or any current max: the model can quote
        # a development max from `one_rms` even when the running block does not
        # rest on one, and the flag must not say otherwise.
        has_synthetic_one_rms=snapshot.has_synthetic_one_rms
        or any(row.is_synthetic for row in snapshot.one_rms),
    )


def _plan(
    session: Session,
    athlete: models.Athlete,
    route: Route,
    *,
    model: Model,
    template_id: uuid.UUID | None,
) -> _Dispatch:
    if template_id is None:
        return _Dispatch(reply=prompts.CLARIFY_TEMPLATE)

    choice = route.choice
    assert choice is not None and choice.start_date is not None  # `_check` guaranteed both
    test_week = (
        {choice.test_week_lift: choice.test_week_number}
        if choice.test_week_lift and choice.test_week_number
        else None
    )

    out = tools.plan_block(
        session,
        athlete,
        template_id=template_id,
        lifts=route.lifts,  # the router refuses a plan that names none
        start_date=choice.start_date,
        weeks=choice.weeks,
        test_week=test_week,
    )
    return _explain_plan(model, out)


def _explain_plan(model: Model, out: PlanOutcome) -> _Dispatch:
    payload = out.model_dump(mode="json")
    subject = "plan_invalid" if out.violations else "proposal"
    explanation = explainer.explain(
        model, subject, payload, fallback=explainer.template(subject, payload)
    )
    return _Dispatch(
        reply=explanation.text,
        proposal_ids=(out.proposal_id,) if out.proposal_id else (),
        has_synthetic_one_rms=out.has_synthetic_one_rms,
        # A pending proposal is the athlete's to decide, so the decision has no
        # outcome yet. A preview or a refusal is answered and done (D1).
        outcome=None if out.proposal_id else "answered",
    )


def _reschedule(
    session: Session, athlete: models.Athlete, route: Route, *, model: Model
) -> _Dispatch:
    choice = route.choice
    assert choice is not None and choice.unavailable_from is not None

    out = tools.reschedule(
        session,
        athlete,
        unavailable_from=choice.unavailable_from,
        unavailable_until=choice.unavailable_until,
        missed_dates=choice.missed_dates,
        injury=choice.injury,
    )
    payload = out.model_dump(mode="json")
    subject = "policy_failure" if out.failure is not None else "proposal"
    explanation = explainer.explain(
        model, subject, payload, fallback=explainer.template(subject, payload)
    )
    return _Dispatch(
        reply=explanation.text,
        proposal_ids=out.proposal_ids,
        has_synthetic_one_rms=out.has_synthetic_one_rms,
        outcome=None if out.proposal_ids else "answered",
        extra={"tier": out.tier},
    )


def _simulate(
    session: Session, athlete: models.Athlete, route: Route, *, model: Model
) -> _Dispatch:
    choice = route.choice
    assert choice is not None

    scenario = to_forced_events(choice)
    if scenario is None:
        return _Dispatch(reply=prompts.CLARIFY_SCENARIO)

    out = tools.simulate_block(session, athlete, scenario=scenario)
    return _explain_simulation(model, out)


def _explain_simulation(model: Model, out: SimulateOutcome) -> _Dispatch:
    payload = out.model_dump(mode="json")
    explanation = explainer.explain(
        model, "simulation", payload, fallback=explainer.template("simulation", payload)
    )
    return _Dispatch(
        reply=explanation.text,
        simulation=out,
        has_synthetic_one_rms=out.has_synthetic_one_rms,
    )


def _dispatch(
    session: Session,
    athlete: models.Athlete,
    route: Route,
    *,
    models_: ChatModels,
    embedder: Embedder,
    template_id: uuid.UUID | None,
) -> _Dispatch:
    """Plain Python. The LLM never loops over the solver (spec 13 §5C).

    A `PlanInvalid` out of a tool becomes a reply rather than a 422: spec 13
    §13 wants the athlete told which rule was broken, and nothing was written.
    """
    agent_model = models_.agent
    try:
        match route.kind:
            case "safety":
                return _safety_reply(route)
            case "clarify":
                return _clarify_reply(route)
            case "answer":
                ctx = router.build_context(session, athlete)
                return _answer(
                    session,
                    athlete,
                    route,
                    model=agent_model,
                    embedder=embedder,
                    ctx=ctx,
                )
            case "plan":
                return _plan(session, athlete, route, model=agent_model, template_id=template_id)
            case "reschedule":
                return _reschedule(session, athlete, route, model=agent_model)
            case "simulate":
                return _simulate(session, athlete, route, model=agent_model)
    except PlanInvalid as exc:
        payload = {
            "violations": [v.model_dump(mode="json") for v in exc.result.violations],
            "warnings": [w.model_dump(mode="json") for w in exc.result.warnings],
        }
        return _Dispatch(reply=explainer.template("plan_invalid", payload))
    except Exception as exc:
        # A dead model raises Pydantic AI's own `ModelAPIError` from inside
        # the answer agent, and `handle_message` only knows `LlmUnavailable`.
        # Without this a real outage on the `answer` route was a bare 500 that
        # rolled back the user's message and left no decision row at all.
        unavailable = unavailable_from(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise

    raise AssertionError(f"unrouted kind {route.kind!r}")  # pragma: no cover


# --- One turn ------------------------------------------------------------


def handle_message(
    session: Session,
    *,
    athlete_id: uuid.UUID,
    message: str,
    models_: ChatModels,
    embedder: Embedder,
    conversation_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
    source: enums.ProposalSource = "chat",
) -> ChatTurn:
    """Route, dispatch, explain, and write exactly one `decision` row.

    Flushes and never commits (ADR-0007 rule 2).

    `LlmUnavailable` comes back on `ChatTurn.failure` instead of being raised.
    The route then commits -- making the `outcome = "failed"` row durable --
    and re-raises it for `api/main.py` to turn into a 503. Raising from here
    would roll that row back on the way out, and spec 13's DoD requires every
    turn to leave one.
    """
    athlete = services.get_athlete(session, athlete_id)

    if conversation_id is None:
        conversation_id = uuid.uuid4()
    else:
        _check_conversation(session, conversation_id, athlete_id)

    with traced(
        "chat_turn",
        athlete_id=athlete_id,
        conversation_id=conversation_id,
        message_length=len(message),
    ) as span:
        trace_id = span.trace_id

        with recorder.recording() as rec:
            history = _history(session, conversation_id)

            user_row = models.ChatMessage(
                conversation_id=conversation_id,
                athlete_id=athlete_id,
                role="user",
                content=message,
            )
            session.add(user_row)
            session.flush()

            failure: LlmUnavailable | None = None
            try:
                route = router.route(
                    session, athlete, message, model=models_.router, history=history
                )
            except LlmUnavailable as exc:
                # We never reached a route. `clarify` is the value that means
                # "we could not act", and `outcome` is what tells it apart from
                # a question we chose to ask (ADR-0015).
                route = Route(kind="clarify", clarify_reason="llm_unavailable")
                failure = exc

            decision = models.Decision(
                run_id=f"{RUN_PREFIX}:{conversation_id}",
                source=source,
                route=route.kind,
                filter_fired=route.filter_fired,
                tool_calls=[],
                langfuse_trace_id=trace_id,
            )
            session.add(decision)
            session.flush()
            user_row.decision_id = decision.id

            if failure is None:
                try:
                    result = _dispatch(
                        session,
                        athlete,
                        route,
                        models_=models_,
                        embedder=embedder,
                        template_id=_resolve_template(session, athlete_id, template_id),
                    )
                except LlmUnavailable as exc:
                    result = _Dispatch(
                        reply=prompts.LLM_UNAVAILABLE_REPLY,
                        outcome="failed",
                    )
                    failure = exc
            else:
                result = _Dispatch(
                    reply=prompts.LLM_UNAVAILABLE_REPLY,
                    outcome="failed",
                )

            assistant_row = models.ChatMessage(
                conversation_id=conversation_id,
                athlete_id=athlete_id,
                role="assistant",
                content=result.reply,
                decision_id=decision.id,
            )
            session.add(assistant_row)

            decision.tool_calls = rec.dump()
            # One column, and an in-week move offers up to three options. The
            # cheapest is stored -- the policy ranked them -- and every id is in
            # `tool_calls`, which is where spec 15 looks for the rest (ADR-0015).
            decision.proposal_id = result.proposal_ids[0] if result.proposal_ids else None
            decision.outcome = result.outcome
            session.add(decision)
            session.flush()

        span.set_many(route=route.kind, outcome=result.outcome or "pending")

    return ChatTurn(
        reply=result.reply,
        route=route,
        decision_id=decision.id,
        conversation_id=conversation_id,
        proposal_ids=result.proposal_ids,
        simulation=result.simulation,
        has_synthetic_one_rms=result.has_synthetic_one_rms,
        trace_id=trace_id,
        failure=failure,
    )


def conversation_messages(
    session: Session,
    conversation_id: uuid.UUID,
    athlete_id: uuid.UUID,
    *,
    limit: int = 50,
) -> Sequence[models.ChatMessage]:
    """One conversation's messages, oldest first.

    `athlete_id` is required, not optional: see `_check_conversation`.
    """
    _check_conversation(session, conversation_id, athlete_id)
    return list(
        session.scalars(
            sa.select(models.ChatMessage)
            .where(
                models.ChatMessage.conversation_id == conversation_id,
                models.ChatMessage.athlete_id == athlete_id,
            )
            .order_by(models.ChatMessage.created_at, _ROLE_ORDER)
            .limit(limit)
        )
    )
