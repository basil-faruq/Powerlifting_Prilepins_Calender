"""The six things the agent can do, as plain typed functions.

Spec 13 §5A. Each one is a thin wrapper over a service that already exists --
none of them computes anything itself, because every number the athlete reads
has to come from the generator, a solver, the simulator or the database
(CLAUDE.md non-negotiable 1). What a tool adds is three things: the arguments
resolved from a sentence, the `@records` entry that puts the call in
`decision.tool_calls` and its own span, and an outcome shaped so the explainer
can narrate whichever branch happened.

They are plain functions *and* Pydantic AI tools. `agent.py` calls them
directly for `plan`, `reschedule` and `simulate` -- plain `match route.kind`,
because the LLM must never loop over a solver -- and registers the two
read-only ones on the answer agent. Both paths go through the same decorated
function, so a call looks identical in the trace whichever reached it.

**Nothing here commits.** Services flush; the route owns the transaction
(ADR-0007 rule 2), so these stay callable from a Temporal activity in spec 15.

**A tool that cannot do the thing returns, rather than raising.** A plan the
validator refused, a policy with no valid option, a request for a shaped block
we do not propose -- those are answers, and spec 13 §13 says the reply explains
the rule rather than the request becoming a 422. `NotFound` and
`LlmUnavailable` still propagate: an unknown athlete is a 404 and a dead model
is a 503.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlmodel import Session

from domains.strength import enums, missed_policy, models, notes, services, simulator
from domains.strength.agent.recorder import records
from domains.strength.agent.schemas import (
    BlockSnapshot,
    LogOutcome,
    NoteSearchOutcome,
    OneRmOut,
    PlanOutcome,
    RescheduleOutcome,
    SimulateOutcome,
)
from domains.strength.schemas import LoggedLift
from planner_core.errors import PlanInvalid
from planner_core.llm import Embedder

logger = logging.getLogger(__name__)

# How many upcoming sessions `get_block` reports. Spec 13 §5A says three.
NEXT_SESSIONS = 3


# --- Summaries for the trace ---------------------------------------------
#
# Each returns the handful of fields worth keeping in `tool_calls` and on the
# span. Deliberately small: dumping a whole outcome would put a 33-session plan
# payload into every trace, and spec 14 §10 rules out full rows anyway.


def _block_summary(out: BlockSnapshot) -> dict[str, Any]:
    return {
        "block_id": str(out.block_id) if out.block_id else None,
        "status": out.status,
        "week": out.week,
        "next_sessions": len(out.next_sessions),
        "one_rms": len(out.one_rms),
        "missed": len(out.missed_seqs),
        "has_synthetic_one_rms": out.has_synthetic_one_rms,
    }


def _notes_summary(out: NoteSearchOutcome) -> dict[str, Any]:
    return {
        "hits": len(out.hits),
        "nearest_distance": out.hits[0].distance if out.hits else None,
        "dates": [hit.noted_on.isoformat() for hit in out.hits],
    }


def _plan_summary(out: PlanOutcome) -> dict[str, Any]:
    return {
        "proposal_id": str(out.proposal_id) if out.proposal_id else None,
        "block_id": str(out.block_id) if out.block_id else None,
        "session_count": out.session_count,
        "preview_only": out.preview_only,
        "refused": bool(out.violations),
        "has_synthetic_one_rms": out.has_synthetic_one_rms,
    }


def _plan_validation(out: PlanOutcome) -> dict[str, Any] | None:
    if not out.violations and not out.warnings:
        return None
    return {
        "ok": not out.violations,
        "violation_rules": sorted({violation.rule for violation in out.violations}),
        "warning_count": len(out.warnings),
    }


def _reschedule_summary(out: RescheduleOutcome) -> dict[str, Any]:
    return {
        "tier": out.tier,
        # Every id, not just the first. `decision.proposal_id` holds one column
        # and an in-week move offers up to three, so this is where spec 15 finds
        # the rest (ADR-0015).
        "proposal_ids": [str(pid) for pid in out.proposal_ids],
        "num_options": len(out.proposal_ids),
        "failed": out.failure is not None,
        "reason": out.failure.reason if out.failure else None,
    }


def _reschedule_stats(out: RescheduleOutcome) -> dict[str, Any] | None:
    if out.stats is None:
        return None
    return {
        "status": out.stats.status,
        "objective": out.stats.objective,
        "wall_time_s": out.stats.wall_time_s,
        "num_booleans": out.stats.num_booleans,
    }


def _sim_summary(out: SimulateOutcome) -> dict[str, Any]:
    result = out.result
    return {
        "runs": result.stats.runs,
        "seed": result.stats.seed,
        "wall_time_s": result.stats.wall_time_s,
        "finish_p50": result.finish_date.p50.isoformat() if result.finish_date else None,
        "p_restart": result.p_restart,
        "has_synthetic_one_rms": out.has_synthetic_one_rms,
    }


def _log_summary(out: LogOutcome) -> dict[str, Any]:
    return {"log_id": str(out.log_id), "seq": out.seq, "lifts_logged": out.lifts_logged}


# --- Shared helpers ------------------------------------------------------


def _one_rms_of(session: Session, athlete_id: uuid.UUID) -> tuple[OneRmOut, ...]:
    """Current maxes, each carrying its own provenance.

    ADR-0004 rule 4: a number derived from a seeded max has to say so, and a
    chat reply is exactly where 125 kg would otherwise read as tested.
    """
    return tuple(
        OneRmOut(
            lift_code=lift,
            kg=row.kg,
            set_on=row.set_on,
            source=row.source,
            is_synthetic=row.is_synthetic,
        )
        for lift, row in sorted(services.current_one_rms(session, athlete_id).items())
    )


def _snapshot_is_synthetic(session: Session, state) -> bool:
    """Whether the *block's* planning maxes were development data.

    Matches `services.simulate_block`'s rule, deliberately: the question is
    what this block rests on, not what the athlete's newest row says today.
    """
    snapshot = state.one_rm_snapshot
    return any(
        row.is_synthetic and lift in snapshot and Decimal(row.kg) == snapshot[lift]
        for lift, row in services.current_one_rms(session, state.athlete_id).items()
    )


def _missed_seqs(state, today: dt.date) -> tuple[int, ...]:
    """Sessions that have gone by without being trained or written off.

    Derived rather than read from a status, because there is no status for it.
    ADR-0010: a session the athlete means to make up stays `planned` with a date
    in the past -- that is what lets the solver re-place it -- and `missed`
    means written off. `moved` is excluded: an approved reschedule already dealt
    with it. So "misses so far" is this derivation and nothing else, which spec
    13 §5A asks for without saying how.
    """
    return tuple(
        sorted(
            block_session.seq
            for block_session in state.plan
            if block_session.date < today and state.status_of[block_session.seq] == "planned"
        )
    )


def _resolve_seqs(
    state, missed_dates: Sequence[dt.date], unavailable_from: dt.date, window_end: dt.date
) -> tuple[int, ...]:
    """Which planned sessions the athlete is telling us they missed.

    `MissedEvent` takes seqs; a sentence gives dates. Sessions already `done`
    or `missed` are skipped rather than passed on -- `missed_policy` raises a
    `ValueError` for a seq that is already done, and "I couldn't train Monday"
    about a Monday that was logged is a mistake to absorb, not to crash on.

    With no dates given, the window `unavailable_from` to `window_end`, both
    inclusive, is used -- so "I can't train tomorrow" resolves without the
    athlete enumerating sessions. **Both ends matter**: the first version took
    everything from `unavailable_from` onward, which for a one-day absence
    selected the rest of the block.
    """
    wanted = set(missed_dates)
    return tuple(
        sorted(
            block_session.seq
            for block_session in state.plan
            if state.status_of[block_session.seq] == "planned"
            and (
                block_session.date in wanted
                if wanted
                else unavailable_from <= block_session.date <= window_end
            )
        )
    )


# --- 1. get_block --------------------------------------------------------


@records("get_block", summarise=_block_summary)
def get_block(session: Session, athlete: models.Athlete) -> BlockSnapshot:
    """The current block, the next three sessions, the 1RMs and the misses.

    Read-only, and the one tool the answer agent leans on. Returns an almost
    empty snapshot when nothing is running: an athlete asking "what should I
    do?" before planning anything is the first thing that will ever happen,
    and it is not an error.
    """
    today = services.today_for(athlete)
    one_rms = _one_rms_of(session, athlete.id)
    weekday_map = services.weekday_slots(session, athlete.id)

    block = services.running_block(session, athlete.id)
    if block is None:
        return BlockSnapshot(
            one_rms=one_rms,
            has_synthetic_one_rms=any(row.is_synthetic for row in one_rms),
            weekday_map=weekday_map,
        )

    state = services.load_block_state(session, block.id)
    template = services.get_template(session, state.template_id)
    upcoming = tuple(
        block_session
        for block_session in state.plan
        if block_session.date >= today and state.status_of[block_session.seq] == "planned"
    )[:NEXT_SESSIONS]
    # From the block's own Monday rather than from a session date, which a
    # reschedule may already have moved (`BlockState.start_date`'s own note).
    week = min(((today - state.start_date).days // 7) + 1, state.template_weeks)

    return BlockSnapshot(
        block_id=state.block_id,
        template_id=state.template_id,
        template_name=template.name,
        status=state.status,
        start_date=state.start_date,
        week=max(week, 1),
        template_weeks=state.template_weeks,
        next_sessions=upcoming,
        one_rms=one_rms,
        has_synthetic_one_rms=_snapshot_is_synthetic(session, state),
        missed_seqs=_missed_seqs(state, today),
        weekday_map=state.weekday_map,
    )


# --- 2. search_notes -----------------------------------------------------


@records("search_notes", summarise=_notes_summary)
def search_notes(
    session: Session,
    athlete: models.Athlete,
    query: str,
    embedder: Embedder,
    *,
    lift: str | None = None,
    since: dt.date | None = None,
    k: int = notes.DEFAULT_K,
) -> NoteSearchOutcome:
    """The athlete's own notes, closest in meaning first.

    `include_synthetic` is not exposed: ADR-0014 excludes seeded notes in both
    directions by default, and a chat reply quoting a demo note as though the
    athlete had written it is exactly the confusion the flag exists to prevent.

    A blank query or an out-of-range `k` raises `ValueError` from the service.
    The router never produces either, so reaching it is a bug and should
    surface as one rather than becoming a polite reply.
    """
    hits = notes.search_notes(
        session,
        athlete.id,
        query,
        embedder,
        lift=lift,
        since=since,
        k=k,
    )
    return NoteSearchOutcome(query=query, hits=tuple(hits))


# --- 3. plan_block -------------------------------------------------------


@records("plan_block", summarise=_plan_summary, validation=_plan_validation)
def plan_block(
    session: Session,
    athlete: models.Athlete,
    *,
    template_id: uuid.UUID,
    lifts: Sequence[str],
    start_date: dt.date,
    weeks: int | None = None,
    test_week: Mapping[str, int] | None = None,
) -> PlanOutcome:
    """A pending `new_block` proposal -- or a preview, or a refusal.

    `weeks` or `test_week` asks for a *shaped* block, and ADR-0011 leaves
    proposing one deliberately undone: the shaping solver is wired for preview
    only. So that request comes back `preview_only=True` with no proposal, and
    the explainer says what the shape would be and that there is nothing to
    approve. Silently planning the template-length block instead would be
    worse -- the athlete asked for nine weeks and would get eleven.

    A validator refusal is returned, not raised: spec 13 §13 wants the reply to
    explain the rule, and no proposal is written either way.
    """
    one_rms = _one_rms_of(session, athlete.id)
    synthetic = any(row.is_synthetic for row in one_rms)

    if weeks is not None or test_week is not None:
        try:
            draft, result, shaped = services.build_shaped_draft(
                session,
                athlete.id,
                template_id,
                list(lifts),
                start_date,
                weeks=weeks,
                test_week=test_week,
            )
        except PlanInvalid as exc:
            return PlanOutcome(
                violations=tuple(exc.result.violations),
                warnings=tuple(exc.result.warnings),
                preview_only=True,
                has_synthetic_one_rms=synthetic,
            )
        return PlanOutcome(
            session_count=len(draft.sessions),
            reasons=(
                f"a {shaped.weeks}-week shaped block, overreaching from week "
                f"{shaped.overreach_from_week}",
            ),
            warnings=tuple(result.warnings),
            preview_only=True,
            has_synthetic_one_rms=synthetic,
        )

    try:
        proposal = services.create_block_proposal(
            session, athlete.id, template_id, list(lifts), start_date
        )
    except PlanInvalid as exc:
        return PlanOutcome(
            violations=tuple(exc.result.violations),
            warnings=tuple(exc.result.warnings),
            has_synthetic_one_rms=synthetic,
        )

    payload = services.read_payload(proposal)
    return PlanOutcome(
        proposal_id=proposal.id,
        block_id=proposal.block_id,
        session_count=len(payload.plan),
        reasons=tuple(proposal.reasons),
        warnings=tuple(exc for exc in _violations(proposal.warnings)),
        has_synthetic_one_rms=synthetic,
    )


def _violations(raw: Sequence[Any]) -> list[Any]:
    """`proposal.warnings` is JSONB; the outcome is typed."""
    from planner_core.proposal import Violation

    return [Violation.model_validate(entry) for entry in raw]


# --- 4. reschedule -------------------------------------------------------


@records("reschedule", summarise=_reschedule_summary, stats=_reschedule_stats)
def reschedule(
    session: Session,
    athlete: models.Athlete,
    *,
    unavailable_from: dt.date,
    unavailable_until: dt.date | None = None,
    missed_dates: Sequence[dt.date] = (),
    injury: bool = False,
    source: enums.ProposalSource = "chat",
) -> RescheduleOutcome:
    """Pending proposals for an absence, ranked cheapest first.

    How many, and of what kind, depends on the tier (ADR-0010): `in_week_move`
    offers up to three, `week_shift` and `fold` one each, and `restart` one
    `new_block` against a replacement block the service has already persisted
    as a draft. Spec 13 §5A's "1-3 reschedule proposals" is wrong on both
    counts, which ADR-0015 records.

    `save_proposals` is what makes them approvable. The policy is pure, so what
    it returns are value objects; without the write the athlete would be shown
    options with nothing behind them.
    """
    today = services.today_for(athlete)
    block = services.running_block(session, athlete.id)
    if block is None:
        return RescheduleOutcome(
            tier="in_week_move",
            failure=missed_policy.PolicyFailed(
                reason="there is no running block to reschedule",
            ),
        )

    state = services.load_block_state(session, block.id)

    # `MissedEvent` is retrospective by construction: it models training that
    # did not happen, so it requires `today >= unavailable_from`. But spec 13
    # §12 requires "I can't make it tomorrow" to reschedule, which is an
    # absence that has not started yet.
    #
    # Resolved here rather than by widening `MissedEvent`, because that type's
    # retrospective framing is load-bearing -- `unavailable_days` closes an
    # open window at `today - 1`, which for a future window would be an empty
    # set, and ADR-0010's tier rules are written against it. Asking the policy
    # "as of the day the absence begins" gives the right answer with the
    # shipped semantics untouched: the solver then treats that day onward as
    # movable, which is exactly what was asked. ADR-0015 records it.
    if unavailable_until is None and unavailable_from >= today:
        # "I can't train tomorrow" -- or today -- is one day, not an open-ended
        # absence. This has to happen *before* the sessions are chosen: it used
        # to follow it, so a one-day absence selected every planned session from
        # that date to the end of the block, `classify` saw dozens of misses
        # instead of one, and the athlete was offered a restart for missing a
        # Wednesday (found by review).
        unavailable_until = unavailable_from

    # An open window that began in the past runs up to yesterday: `MissedEvent`
    # treats today as available, because the athlete is back.
    window_end = unavailable_until or (today - dt.timedelta(days=1))
    seqs = _resolve_seqs(state, missed_dates, unavailable_from, window_end)
    if not seqs:
        return RescheduleOutcome(
            tier="in_week_move",
            failure=missed_policy.PolicyFailed(
                reason="no planned session falls in that window",
            ),
            has_synthetic_one_rms=_snapshot_is_synthetic(session, state),
        )

    event = missed_policy.MissedEvent(
        block_id=state.block_id,
        missed_seqs=seqs,
        unavailable_from=unavailable_from,
        unavailable_until=unavailable_until,
        injury=injury,
        today=max(today, unavailable_from),
    )
    result = services.propose_for_missed(session, event, source=source)
    synthetic = _snapshot_is_synthetic(session, state)

    if not result.ok:
        return RescheduleOutcome(
            tier=result.tier,
            failure=result.failure,
            stats=result.stats or (result.failure.stats if result.failure else None),
            has_synthetic_one_rms=synthetic,
        )

    rows = services.save_proposals(session, result)
    return RescheduleOutcome(
        tier=result.tier,
        proposal_ids=tuple(row.id for row in rows),
        kinds=tuple(row.kind for row in rows),
        reasons=tuple(reason for row in rows for reason in row.reasons),
        warnings=tuple(_violations(rows[0].warnings)) if rows else (),
        costs=tuple(dict(row.cost) for row in rows),
        stats=result.stats,
        has_synthetic_one_rms=synthetic,
    )


# --- 5. simulate_block ---------------------------------------------------


@records("simulate_block", summarise=_sim_summary)
def simulate_block(
    session: Session,
    athlete: models.Athlete,
    *,
    scenario: Sequence[simulator.ForcedEvent] = (),
    runs: int = simulator.DEFAULT_SIM_CONFIG.runs,
    seed: int = 0,
) -> SimulateOutcome:
    """Percentiles and assumptions for the rest of the block. Writes nothing.

    Never a single predicted number (CLAUDE.md non-negotiable 8) -- the result
    carries p10/p50/p90 and its own assumptions list, and the explainer is
    instructed to quote ranges.

    `BlockNotRunning` and `PlanInvalid` (a scenario outside the block)
    propagate: both are already mapped, and both describe a request that
    cannot be answered rather than an answer of "no".
    """
    block = services.running_block(session, athlete.id)
    if block is None:
        raise PlanInvalid.of("block", "there is no running block to simulate")

    result, synthetic = services.simulate_block(
        session, block.id, scenario=scenario, runs=runs, seed=seed
    )
    return SimulateOutcome(block_id=block.id, result=result, has_synthetic_one_rms=synthetic)


# --- 6. log_session ------------------------------------------------------


@records("log_session", summarise=_log_summary)
def log_session(
    session: Session,
    athlete: models.Athlete,
    *,
    performed_on: dt.date,
    entries: Sequence[LoggedLift],
    note: str | None = None,
    embedder: Embedder | None = None,
    session_id: uuid.UUID | None = None,
) -> LogOutcome:
    """Record a session as performed, and index its note.

    Implemented and tested, but **no route kind dispatches to it** in v1. There
    are four kinds the router may choose and `log` is not one of them; adding
    it would mean widening `decision.route`'s CHECK constraint, which is a
    migration this pair of specs does not take (ADR-0015, D2). The safety
    route's "offer to log the note" stays an offer the UI acts on, and spec 15
    reaches this through its own activity.

    `session_id` defaults to the planned session on `performed_on`, so a caller
    with a date and no id -- which is all a sentence gives -- still works.
    """
    if session_id is None:
        session_id = _session_on(session, athlete.id, performed_on)

    log = services.log_session(
        session,
        session_id,
        performed_on,
        list(entries),
        note=note,
        embedder=embedder,
    )
    planned = services.get_session_row(session, session_id)
    return LogOutcome(
        log_id=log.id,
        session_id=session_id,
        seq=planned.seq,
        lifts_logged=len(entries),
    )


def _session_on(session: Session, athlete_id: uuid.UUID, performed_on: dt.date) -> uuid.UUID:
    block = services.running_block(session, athlete_id)
    if block is None:
        raise PlanInvalid.of("block", "there is no running block to log against")
    for row in services.sessions_of(session, block.id):
        if row.date == performed_on:
            return row.id
    raise PlanInvalid.of("session", f"no session is planned for {performed_on.isoformat()}")
