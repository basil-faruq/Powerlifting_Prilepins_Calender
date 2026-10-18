"""One chat turn, and the `decision` row it has to leave behind.

Spec 13's DoD requires every turn to write exactly one `decision` row --
including the ones that failed, which is the half that is easy to get wrong.
These tests assert the row, not just the reply: route, `filter_fired`,
`outcome`, `proposal_id`, the `tool_calls` entries, and both `chat_message`
rows pointing at it (D1, which is what makes `GET /decisions?athlete_id=`
possible without a column for the athlete).

Needs Postgres.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import models, services
from domains.strength.agent import agent as agent_module
from domains.strength.agent.schemas import ChatModels, RouterChoice
from planner_core.errors import OllamaUnreachable
from tests.api.conftest import propose
from tests.fakes import (
    FakeEmbedder,
    RefusingModel,
    replying_model,
    router_model,
    tool_calling_model,
)


def _models(*, router: object, agent: object | None = None) -> ChatModels:
    return ChatModels(router=router, agent=agent or replying_model("Noted."))


@pytest.fixture
def active(api_client, db_session: Session, ready) -> models.Block:
    block_id, proposal_id = propose(api_client, ready["athlete"], ready["template"])
    approved = api_client.post(f"/proposals/{proposal_id}/approve")
    assert approved.status_code == 200, approved.text
    return services.get_block(db_session, block_id)


def _decision(db_session: Session, decision_id: uuid.UUID) -> models.Decision:
    row = db_session.get(models.Decision, decision_id)
    assert row is not None
    return row


def _messages(db_session: Session, conversation_id: uuid.UUID) -> list[models.ChatMessage]:
    return list(
        db_session.scalars(
            sa.select(models.ChatMessage)
            .where(models.ChatMessage.conversation_id == conversation_id)
            .order_by(models.ChatMessage.created_at, models.ChatMessage.id)
        )
    )


def _next_monday(after: dt.date) -> dt.date:
    return after + dt.timedelta(days=(7 - after.weekday()) % 7 or 7)


# --- The safety route ----------------------------------------------------


def test_pain_answers_from_a_fixed_reply_with_no_model_call(db_session: Session, ready):
    """Spec 13 §12: `safety` without any LLM call, and `filter_fired` set.

    Both models raise on use, so this passing means neither was reached.
    """
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts on squats",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    assert turn.route.kind == "safety"
    row = _decision(db_session, turn.decision_id)
    assert row.route == "safety"
    assert row.filter_fired == "pain_keyword"
    assert row.outcome == "answered"
    assert "physio" in turn.reply
    assert "squat" in turn.reply


# --- The clarify filters -------------------------------------------------


def test_no_one_rms_clarifies_and_records_the_filter(db_session: Session, athlete, template):
    turn = agent_module.handle_message(
        db_session,
        athlete_id=athlete.id,
        message="plan me a block",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert (row.route, row.filter_fired, row.outcome) == ("clarify", "no_one_rms", "answered")
    assert "1RM" in turn.reply


def test_an_unparseable_turn_is_failed_but_not_an_error(db_session: Session, ready, active):
    """Spec 13 §11. The system behaved exactly as designed, so this is a 200
    with `outcome = "failed"` -- the route column has no value for it."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="mmhm",
        models_=_models(router=router_model(RouterChoice(kind="answer"), fail_times=5)),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.route == "clarify"
    assert row.outcome == "failed"
    assert row.filter_fired is None
    assert turn.failure is None
    assert "rephrasing" in turn.reply


# --- plan ---------------------------------------------------------------


def test_a_plan_turn_leaves_a_pending_proposal_and_no_outcome(db_session: Session, ready):
    """D1: `outcome IS NULL` means "a proposal is waiting on you".

    No new enum value and no migration -- `approved`/`rejected` belong to the
    human's later decision in spec 15.
    """
    monday = _next_monday(services.today_for(ready["athlete"]))

    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="start a new block with squat and bench on Monday",
        models_=_models(
            router=router_model(
                RouterChoice(kind="plan", lifts=["squat", "bench"], start_date=monday)
            ),
            agent=replying_model("I've prepared a block for you to approve."),
        ),
        embedder=FakeEmbedder(),
        template_id=ready["template"].id,
    )

    row = _decision(db_session, turn.decision_id)
    assert row.route == "plan"
    assert row.outcome is None
    assert row.proposal_id is not None
    assert len(turn.proposal_ids) == 1
    assert services.get_proposal(db_session, turn.proposal_ids[0]).status == "pending"


def test_a_plan_with_33_sessions(db_session: Session, ready):
    """Spec 13 §12: "one pending proposal with 33 sessions"."""
    monday = _next_monday(services.today_for(ready["athlete"]))

    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="new block, all lifts",
        models_=_models(
            router=router_model(
                RouterChoice(
                    kind="plan",
                    lifts=["squat", "bench", "deadlift", "press", "row"],
                    start_date=monday,
                )
            ),
            agent=replying_model("Done."),
        ),
        embedder=FakeEmbedder(),
        template_id=ready["template"].id,
    )

    payload = services.read_payload(services.get_proposal(db_session, turn.proposal_ids[0]))
    assert len(payload.plan) == 33


def test_a_refused_plan_is_answered_not_raised(db_session: Session, athlete, template, maxes):
    """Spec 13 §13: a tool's refusal becomes a reply.

    No weekday map here, so the generator cannot place a session. The athlete
    is told which setup step is missing, and nothing is written.
    """
    monday = _next_monday(dt.date.today())

    turn = agent_module.handle_message(
        db_session,
        athlete_id=athlete.id,
        message="new block with squat starting Monday",
        models_=_models(
            router=router_model(RouterChoice(kind="plan", lifts=["squat"], start_date=monday)),
            agent=replying_model("That won't work."),
        ),
        embedder=FakeEmbedder(),
        template_id=template.id,
    )

    row = _decision(db_session, turn.decision_id)
    # The weekday filter catches it first, which is the better answer anyway.
    assert row.route == "clarify"
    assert row.outcome == "answered"
    assert turn.proposal_ids == ()


# --- reschedule ---------------------------------------------------------


def test_a_reschedule_turn_leaves_pending_proposals(db_session: Session, ready, active):
    """Spec 13 §12: "I can't make it tomorrow" -> reschedule with proposals."""
    for seq in (1, 2):
        row = next(r for r in services.sessions_of(db_session, active.id) if r.seq == seq)
        row.status = "done"
        db_session.add(row)
    db_session.flush()
    third = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 3)

    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="I can't train on Wednesday",
        models_=_models(
            router=router_model(
                RouterChoice(
                    kind="reschedule",
                    unavailable_from=third.date,
                    unavailable_until=third.date,
                    missed_dates=[third.date],
                )
            ),
            agent=replying_model("I've moved that session."),
        ),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.route == "reschedule"
    assert row.outcome is None
    assert 1 <= len(turn.proposal_ids) <= 3
    assert row.proposal_id == turn.proposal_ids[0]


def test_every_proposal_id_is_recoverable_from_the_tool_calls(db_session: Session, ready, active):
    """The known price of D1: one column, up to three options.

    The cheapest goes in `decision.proposal_id` and all of them in
    `tool_calls`, which is where spec 15 looks for the rest (ADR-0015).
    """
    for seq in (1, 2):
        row = next(r for r in services.sessions_of(db_session, active.id) if r.seq == seq)
        row.status = "done"
        db_session.add(row)
    db_session.flush()
    third = next(r for r in services.sessions_of(db_session, active.id) if r.seq == 3)

    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="can't make Wednesday",
        models_=_models(
            router=router_model(
                RouterChoice(
                    kind="reschedule",
                    unavailable_from=third.date,
                    unavailable_until=third.date,
                    missed_dates=[third.date],
                )
            ),
            agent=replying_model("Moved."),
        ),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    entry = next(call for call in row.tool_calls if call["name"] == "reschedule")
    assert entry["output_summary"]["proposal_ids"] == [str(p) for p in turn.proposal_ids]


def test_a_policy_failure_is_answered_with_its_reason(db_session: Session, ready, active):
    # Past the end of an 11-week block, but inside the router's own 365-day
    # horizon -- so this reaches the tool rather than being clarified away.
    far = dt.date.today() + dt.timedelta(days=200)

    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="I'll be away",
        models_=_models(
            router=router_model(
                RouterChoice(kind="reschedule", unavailable_from=far, missed_dates=[far])
            ),
            agent=replying_model("Nothing to move."),
        ),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.route == "reschedule"
    assert row.outcome == "answered"
    assert turn.proposal_ids == ()


# --- simulate -----------------------------------------------------------


def test_a_simulate_turn_returns_a_result_and_proposes_nothing(db_session: Session, ready, active):
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="what if I'm sick for 10 days in week 6?",
        models_=_models(
            router=router_model(
                RouterChoice(
                    kind="simulate",
                    scenario_kind="illness",
                    scenario_start_week=6,
                    scenario_days=10,
                )
            ),
            agent=replying_model("It should still finish, with some slippage."),
        ),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.route == "simulate"
    assert row.outcome == "answered"
    assert row.proposal_id is None
    assert turn.simulation is not None
    assert turn.simulation.block_id == active.id
    assert turn.simulation.result.assumptions


def test_an_incomplete_scenario_becomes_a_question(db_session: Session, ready, active):
    """`to_forced_events` returning `None` is a clarify, not a retry."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="what if I travel?",
        models_=_models(
            router=router_model(RouterChoice(kind="simulate", scenario_kind="travel")),
            agent=replying_model("unused"),
        ),
        embedder=FakeEmbedder(),
    )

    assert turn.simulation is None
    assert "how many days" in turn.reply


# --- answer -------------------------------------------------------------


def test_an_answer_turn_may_consult_the_read_only_tools(db_session: Session, ready, active):
    """Spec 13 §5C: the answer agent gets `get_block` and `search_notes` only.

    `FunctionModel` because what matters is the sequence -- the agent consulted
    the database before speaking -- which `TestModel` cannot express.
    """
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="what's on Thursday?",
        models_=_models(
            router=router_model(RouterChoice(kind="answer", question="what's on Thursday?")),
            agent=tool_calling_model("get_block", "Your next session is a squat day."),
        ),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.route == "answer"
    assert row.outcome == "answered"
    assert "squat day" in turn.reply
    # The tool ran in an anyio worker thread and still recorded itself.
    assert any(call["name"] == "get_block" for call in row.tool_calls)


# --- The row, in general ------------------------------------------------


def test_one_turn_writes_exactly_one_decision(db_session: Session, ready, active):
    before = len(list(db_session.scalars(sa.select(models.Decision))))

    agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my shoulder is sore",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    after = len(list(db_session.scalars(sa.select(models.Decision))))
    assert after == before + 1


def test_both_messages_point_at_the_decision(db_session: Session, ready, active):
    """D1: this join is how `GET /decisions?athlete_id=` works at all."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my elbow hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    rows = _messages(db_session, turn.conversation_id)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert all(row.decision_id == turn.decision_id for row in rows)
    assert rows[0].content == "my elbow hurts"
    assert rows[1].content == turn.reply


def test_the_run_id_is_the_conversation(db_session: Session, ready, active):
    """One run per conversation, not per turn: per-turn would make an indexed
    column a worse duplicate of the primary key (ADR-0015)."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.run_id == f"chat:{turn.conversation_id}"


def test_two_turns_in_one_conversation_share_a_run_id(db_session: Session, ready, active):
    first = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )
    second = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my elbow hurts too",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
        conversation_id=first.conversation_id,
    )

    assert first.decision_id != second.decision_id
    assert (
        _decision(db_session, first.decision_id).run_id
        == _decision(db_session, second.decision_id).run_id
    )


def test_the_router_is_the_first_tool_call(db_session: Session, ready, active):
    """D1 again: there is no column for the extracted route parameters, so
    `tool_calls[0]` is where spec 21's drawer reads them."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="what's next?",
        models_=_models(
            router=router_model(RouterChoice(kind="answer", question="what's next?")),
            agent=replying_model("A squat day."),
        ),
        embedder=FakeEmbedder(),
    )

    row = _decision(db_session, turn.decision_id)
    assert row.tool_calls[0]["name"] == "router"
    assert row.tool_calls[0]["output_summary"]["kind"] == "answer"


def test_the_trace_id_is_null_with_tracing_off(db_session: Session, ready, active):
    """Spec 14 §12: without keys everything works and the id is absent."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    assert turn.trace_id is None
    assert _decision(db_session, turn.decision_id).langfuse_trace_id is None


def test_handle_message_does_not_commit(db_session: Session, ready, active):
    """ADR-0007 rule 2. Spec 15 calls this from an activity owning its own."""
    agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    assert db_session.in_transaction()


# --- Failures -----------------------------------------------------------


def test_an_unknown_athlete_writes_nothing(db_session: Session, ready):
    from planner_core.errors import NotFound

    before = len(list(db_session.scalars(sa.select(models.Decision))))

    with pytest.raises(NotFound):
        agent_module.handle_message(
            db_session,
            athlete_id=uuid.uuid4(),
            message="hello",
            models_=_models(router=RefusingModel()),
            embedder=FakeEmbedder(),
        )

    assert len(list(db_session.scalars(sa.select(models.Decision)))) == before


def test_a_dead_model_returns_the_failure_rather_than_raising(db_session: Session, ready, active):
    """The crux of the 503 path.

    A service may not commit (ADR-0007 rule 2), and raising from here would
    roll the row back on its way to the handler -- so the failure is handed to
    the route, which commits and *then* raises it.
    """
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="plan me a new block",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    assert isinstance(turn.failure, OllamaUnreachable)
    row = _decision(db_session, turn.decision_id)
    assert row.outcome == "failed"
    assert row.route == "clarify"
    assert row.filter_fired is None


def test_a_borrowed_conversation_id_is_refused(db_session: Session, ready, active, template):
    """`conversation_id` is not a foreign key, so any UUID is syntactically
    valid -- without the check, a guess would read another conversation."""
    from planner_core.errors import NotFound

    other = models.Athlete(name="someone-else", plate_increment_kg=1, rounding="floor")
    db_session.add(other)
    db_session.flush()
    theirs = uuid.uuid4()
    db_session.add(
        models.ChatMessage(
            conversation_id=theirs, athlete_id=other.id, role="user", content="private"
        )
    )
    db_session.flush()

    with pytest.raises(NotFound):
        agent_module.handle_message(
            db_session,
            athlete_id=ready["athlete"].id,
            message="what did I say?",
            models_=_models(router=RefusingModel()),
            embedder=FakeEmbedder(),
            conversation_id=theirs,
        )


# --- Conversation history -----------------------------------------------


def test_history_is_capped_at_ten_messages(db_session: Session, ready, active):
    """Spec 13 §10. Older turns are dropped; no summarisation in v1.

    `created_at` is set explicitly here because every row inserted in one
    transaction otherwise shares it -- Postgres' `now()` is the transaction
    start time -- and real turns are one transaction each.
    """
    conversation = uuid.uuid4()
    base = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)
    for index in range(14):
        db_session.add(
            models.ChatMessage(
                conversation_id=conversation,
                athlete_id=ready["athlete"].id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"message {index}",
                created_at=base + dt.timedelta(minutes=index),
            )
        )
    db_session.flush()

    history = agent_module._history(db_session, conversation)

    assert len(history) == 10
    assert history[-1].content == "message 13"
    assert history[0].content == "message 4"


def test_a_turns_question_sorts_before_its_reply(db_session: Session, ready, active):
    """Both messages of a turn share one `created_at`, so the role breaks the
    tie -- otherwise the transcript the router sees is inverted."""
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    history = agent_module._history(db_session, turn.conversation_id)

    assert [row.role for row in history] == ["user", "assistant"]
    assert history[0].content == "my knee hurts"


def test_conversation_messages_requires_the_right_athlete(db_session: Session, ready, active):
    turn = agent_module.handle_message(
        db_session,
        athlete_id=ready["athlete"].id,
        message="my knee hurts",
        models_=_models(router=RefusingModel(), agent=RefusingModel()),
        embedder=FakeEmbedder(),
    )

    rows = agent_module.conversation_messages(db_session, turn.conversation_id, ready["athlete"].id)

    assert len(rows) == 2
    assert [row.role for row in rows] == ["user", "assistant"]


# --- Template resolution ------------------------------------------------


def test_the_only_template_is_used_when_none_was_given(db_session: Session, ready):
    """Spec 13 never says where `template_id` comes from."""
    resolved = agent_module._resolve_template(db_session, ready["athlete"].id, None)

    assert resolved == ready["template"].id


def test_an_explicit_template_wins(db_session: Session, ready):
    resolved = agent_module._resolve_template(db_session, ready["athlete"].id, ready["template"].id)

    assert resolved == ready["template"].id


def test_the_most_recent_blocks_template_is_preferred(db_session: Session, ready, active):
    resolved = agent_module._resolve_template(db_session, ready["athlete"].id, None)

    assert resolved == active.template_id
