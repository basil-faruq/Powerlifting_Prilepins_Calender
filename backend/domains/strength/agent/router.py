"""Two layers: deterministic filters, then an LLM that picks among what is left.

Spec 13 §5B, and CLAUDE.md non-negotiable 3. Layer 1 is Python and runs first,
always. Only if nothing fires does layer 2 happen, and by then the dangerous
answers are already unreachable -- the model's output type cannot express
`safety`, so a sufficiently confused generation cannot route a pain report to
the planner.

The three filters, in order:

1. **pain** -- `notes.is_pain_text(message)`. Spec 13 §5B says "matches the
   `pain_flags` keyword list", but `pain_flags` takes a `Session` and queries
   the database: it screens stored *notes*, not a sentence. The free-text
   decider is `is_pain_text`, which is the right one anyway because ADR-0014
   made it negation-aware -- "no pain today, felt great" must not refuse to
   plan. ADR-0015 records the correction.
2. **no 1RMs** -- every weight is a percentage of one, so there is nothing to
   compute from.
3. **no weekday map**, when the message is about planning. The planning test
   is a keyword set, not a model call: asking the LLM whether to ask the LLM
   defeats a filter whose purpose is to run first.

Layer 2 is handed a table of the next fourteen dates and picks one. It does not
do arithmetic -- the same rule as CLAUDE.md non-negotiable 1, one layer
earlier. A date it returns that is not resolvable becomes a `clarify`.
"""

from __future__ import annotations

import datetime as dt
import logging

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlmodel import Session

from domains.strength import enums, models, notes, services
from domains.strength.agent import prompts
from domains.strength.agent.recorder import records
from domains.strength.agent.schemas import Route, RouterChoice
from planner_core.llm import structured, unavailable_from

logger = logging.getLogger(__name__)


PAIN_FILTER = "pain_keyword"
NO_ONE_RMS_FILTER = "no_one_rms"
NO_WEEKDAY_MAP_FILTER = "no_weekday_map"

# Spec 13 §11: two unusable structured outputs and we stop asking. Not a
# starting value -- a third attempt on a model that has misunderstood the
# schema twice is a worse use of the athlete's time than a rephrase.
MAX_ATTEMPTS = 2

# How far ahead a date the model returns may land without being stated outright.
# A year is generous enough for "next spring" and tight enough that a
# hallucinated 2031 is caught.
MAX_DATE_HORIZON_DAYS = 365

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class RouterContext(BaseModel):
    """What the prompt needs, resolved from the database before the model runs.

    Assembled once per turn and thrown away. `today` comes from
    `services.today_for(athlete)` -- the athlete's own timezone, never
    `date.today()` (ADR-0007).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    today: dt.date
    # slot -> weekday name, for the prompt to print.
    weekdays: dict[int, str]
    block_week: int | None = None
    block_weeks: int | None = None
    lifts: tuple[str, ...] = ()
    has_one_rms: bool = False
    has_block: bool = False


def build_context(session: Session, athlete: models.Athlete) -> RouterContext:
    """Everything layer 2's prompt needs, read once."""
    today = services.today_for(athlete)
    slots = services.weekday_slots(session, athlete.id)
    block = services.running_block(session, athlete.id)

    week: int | None = None
    weeks: int | None = None
    lifts: tuple[str, ...] = ()
    if block is not None:
        # `running_block` returns only `active`/`paused`, so the state loads --
        # a `draft` would raise, its schedule still being in a proposal.
        state = services.load_block_state(session, block.id)
        weeks = state.template_weeks
        lifts = tuple(state.lifts)
        # Counted from the block's own Monday, not from a session date -- a
        # reschedule may already have moved those.
        elapsed = ((today - state.start_date).days // 7) + 1
        week = min(max(elapsed, 1), weeks)

    return RouterContext(
        today=today,
        weekdays={slot: WEEKDAY_NAMES[weekday] for slot, weekday in slots.items()},
        block_week=week,
        block_weeks=weeks,
        lifts=lifts,
        has_one_rms=bool(services.current_one_rms(session, athlete.id)),
        has_block=block is not None,
    )


# --- Layer 1 -------------------------------------------------------------


def filter_route(session: Session, athlete: models.Athlete, message: str) -> Route | None:
    """The deterministic filters. `None` means layer 2 may run.

    No model is constructed and no network touched, which is what makes spec
    13 §12's "without any LLM call" a property a test can assert by handing
    this a model that raises on use.
    """
    if notes.is_pain_text(message):
        # Which lift, if the sentence named one. `detect_lifts` unions the
        # mentioned lifts; with no session context there is nothing else to
        # union, so a bare "it hurts" names none -- and the reply says so
        # generally rather than guessing.
        mentioned = tuple(notes.detect_lifts(message))
        return Route(kind="safety", filter_fired=PAIN_FILTER, lifts=mentioned)

    if not services.current_one_rms(session, athlete.id):
        return Route(
            kind="clarify",
            filter_fired=NO_ONE_RMS_FILTER,
            clarify_reason="no_one_rms",
        )

    if not services.weekday_slots(session, athlete.id) and prompts.looks_like_planning(message):
        return Route(
            kind="clarify",
            filter_fired=NO_WEEKDAY_MAP_FILTER,
            clarify_reason="no_weekday_map",
        )

    return None


# --- Layer 2 -------------------------------------------------------------


def select_route(
    model: Model,
    message: str,
    *,
    ctx: RouterContext,
    history: list[models.ChatMessage] | None = None,
) -> RouterChoice | None:
    """Ask the model for a route. `None` after `MAX_ATTEMPTS` unusable answers.

    **One call, in the provider's native structured-output mode.** This was
    briefly two (a kind, then that kind's parameters), on the theory that a
    16-field schema was too wide for a 14b model. That theory was wrong. The
    schema scored 7/30 as a tool call and the narrower ones scored no better;
    measured side by side, the *mode* was the difference -- the same wide
    schema scored 4/4 at about 5 s a call in native mode, against 78 s as a
    tool. `llm.structured` picks the mode, and the two-stage code was removed
    rather than kept as an optimisation nobody needs (ADR-0015).

    The message arrives as quoted data, not as part of the instructions: it is
    the athlete's own words, and a sentence asking the agent to ignore its
    instructions is one somebody could write (CLAUDE.md).
    """
    agent: Agent[None, RouterChoice] = Agent(
        model,
        output_type=structured(RouterChoice),
        instructions=prompts.router_instructions(
            today=ctx.today,
            weekdays=ctx.weekdays,
            block_week=ctx.block_week,
            block_weeks=ctx.block_weeks,
            lifts=ctx.lifts,
        ),
        # Pydantic AI's own retry, which hands the model its validation error.
        # One, so with the outer loop the model sees the schema at most
        # `MAX_ATTEMPTS` times.
        retries=1,
    )

    prompt = _with_history(message, history or [])
    for attempt in range(MAX_ATTEMPTS):
        try:
            return agent.run_sync(prompt).output
        except Exception as exc:
            # A dead model is a 503, not a bad sentence. `unavailable_from`
            # recognises Pydantic AI's own `ModelAPIError`/`ModelHTTPError`,
            # which is what a real outage raises -- the first version only
            # recognised `LlmUnavailable` itself, so Ollama being down was
            # retried and then reported as "I couldn't understand that".
            unavailable = unavailable_from(exc)
            if unavailable is not None:
                raise unavailable from exc
            logger.warning(
                "router: unusable structured output on attempt %d of %d",
                attempt + 1,
                MAX_ATTEMPTS,
                exc_info=True,
            )
    return None


def _with_history(message: str, history: list[models.ChatMessage]) -> str:
    """The last turns, then the new message. Both quoted as data.

    Spec 13 §10: the last ten messages, no summarisation in v1.
    """
    if not history:
        return prompts.quoted("message", message)

    transcript = "\n".join(f"{row.role}: {row.content}" for row in history)
    return (
        f"{prompts.quoted('conversation_so_far', transcript)}\n\n"
        f"{prompts.quoted('message', message)}"
    )


# --- Validating what the model said --------------------------------------


def _resolve_lifts(choice: RouterChoice) -> tuple[tuple[str, ...], str | None]:
    """Lift names against the register. Unknown names are a question, not a guess.

    Returns the resolved codes and, if one could not be resolved, the reason to
    clarify with. Matching is case-insensitive and tolerates the plural the
    athlete will actually type ("squats").
    """
    resolved: list[str] = []
    for name in choice.lifts:
        code = name.strip().lower().rstrip("s")
        if code == "bencpres":  # "bench press" -> "bench pres" -> stripped
            code = "bench"
        if code in enums.LIFT_CODES:
            resolved.append(code)
            continue
        # One more try for multi-word names the model may echo back.
        match = next((c for c in enums.LIFT_CODES if c in name.strip().lower()), None)
        if match is None:
            return (), "unknown_lift"
        resolved.append(match)
    # De-duplicated, in training order, so a tool never sees "bench, squat".
    return tuple(code for code in enums.LIFT_CODES if code in set(resolved)), None


def _date_is_plausible(value: dt.date, today: dt.date) -> bool:
    """Whether a date the model returned can be trusted.

    The prompt supplies fourteen days and says to copy one. Beyond that window
    a date is either stated outright in the message -- fine -- or computed,
    which the model is not allowed to do. A year's horizon is the compromise:
    it accepts a block starting next quarter and rejects a hallucinated 2031.
    """
    return (
        today - dt.timedelta(days=MAX_DATE_HORIZON_DAYS)
        <= value
        <= today + dt.timedelta(days=MAX_DATE_HORIZON_DAYS)
    )


def _check(choice: RouterChoice, ctx: RouterContext) -> str | None:
    """What is wrong with this choice, if anything. `None` means usable."""
    for value in (
        choice.start_date,
        choice.unavailable_from,
        choice.unavailable_until,
        choice.scenario_start_date,
        *choice.missed_dates,
    ):
        if value is not None and not _date_is_plausible(value, ctx.today):
            return "date_out_of_range"

    if (
        choice.unavailable_until is not None
        and choice.unavailable_from is not None
        and choice.unavailable_until < choice.unavailable_from
    ):
        return "window_reversed"

    if choice.kind == "reschedule" and choice.unavailable_from is None:
        return "no_window"

    # `reschedule` and `simulate` both need a block to act on.
    if choice.kind in ("reschedule", "simulate") and not ctx.has_block:
        return "no_block"

    if choice.kind == "plan" and choice.start_date is None:
        return "no_start_date"

    return None


# --- Both layers together ------------------------------------------------


def _route_summary(route: Route) -> dict[str, object]:
    """What spec 21's drawer reads as "the LLM route with its parameters".

    `decision` has no column for the extracted parameters, and D1 takes no
    migration, so this entry -- always `tool_calls[0]` -- is where they live.
    """
    summary: dict[str, object] = {
        "kind": route.kind,
        "filter_fired": route.filter_fired,
        "unparseable": route.unparseable,
        "clarify_reason": route.clarify_reason,
        "lifts": list(route.lifts),
    }
    if route.choice is not None:
        summary["choice"] = route.choice.model_dump(mode="json", exclude_defaults=True)
    return summary


@records("router", summarise=_route_summary)
def route(
    session: Session,
    athlete: models.Athlete,
    message: str,
    *,
    model: Model,
    history: list[models.ChatMessage] | None = None,
) -> Route:
    """Layer 1, then layer 2 only if nothing fired.

    Decorated, so the route and its extracted parameters land in
    `decision.tool_calls` as the first entry and in a `tool.router` span,
    without `handle_message` having to assemble either by hand.
    """
    filtered = filter_route(session, athlete, message)
    if filtered is not None:
        return filtered

    ctx = build_context(session, athlete)
    choice = select_route(model, message, ctx=ctx, history=history)
    if choice is None:
        return Route(kind="clarify", unparseable=True, clarify_reason="unparseable")

    lifts, lift_problem = _resolve_lifts(choice)
    if lift_problem is not None:
        return Route(kind="clarify", clarify_reason=lift_problem, choice=choice)

    problem = _check(choice, ctx)
    if problem is not None:
        return Route(kind="clarify", clarify_reason=problem, choice=choice)

    # A plan that names no lifts is a question, not a licence to plan all five:
    # the prompt tells the model an unset field becomes a question, and silently
    # defaulting here would contradict it (found by review).
    if choice.kind == "plan" and not lifts:
        return Route(kind="clarify", clarify_reason="no_lifts", choice=choice)

    return Route(kind=choice.kind, choice=choice, lifts=lifts)
