"""Fixtures for the route tests: an athlete, real maxes, a template with entries.

Two deliberate choices here.

**The maxes are this file's own numbers** (`API_MAXES`), not the spreadsheet's
examples and not `seed_dev_one_rms`'s defaults. A reader seeing 141 kg in a
test can therefore tell at a glance which block it came from, which is the
point of the CLAUDE.md guardrail. They are written as `source = "tested"` with
`is_synthetic` false, because that is what a real athlete's input looks like
and these tests are about the real path.

**The template entries come from the committed golden fixture**, through
`tests.golden`, so the API tests exercise the authored 11x3 block -- 33
sessions, four test singles, and a plan the validator accepts (CLAUDE.md
non-negotiable #5) -- without needing the gitignored workbook.

**The workflow runs in-process** (`_in_process_workflows`). Since spec 15 an
approval is a message to a Temporal execution, and requiring a running
server for every route test would be a poor trade -- slow, and failing for
reasons that have nothing to do with the route. So the client functions are
replaced by ones that call the very same activities directly, against this
test's session. The real durable path has its own suite in
`test_block_workflow_run.py`, under a time-skipping server.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import uuid
from decimal import Decimal

import pytest
from sqlmodel import Session

from api import temporal_client
from domains.strength import models
from domains.strength.workflows import contracts
from domains.strength.workflows.activities import StrengthActivities
from planner_core import temporal_errors
from tests import golden

# This suite's own maxes. Distinct from the spreadsheet's examples
# (160/140/105/70/105) and from the dev seed's (150/130/100/60/90), so a weight
# in a failure message identifies where it came from.
API_MAXES: dict[str, Decimal] = {
    "squat": Decimal("125"),
    "bench": Decimal("95"),
    "deadlift": Decimal("145"),
    "press": Decimal("55"),
    "row": Decimal("85"),
}

MON_WED_FRI = golden.MON_WED_FRI
ALL_LIFTS = sorted(API_MAXES)


def next_monday(after: dt.date | None = None) -> dt.date:
    """The first Monday strictly after `after` (default: today).

    `golden.START` is a fixed 2026-01-05, which these tests deliberately do not
    reuse: a block entirely in the past satisfies the four calendar rules
    vacuously -- they all judge from `ctx.today` onward (ADR-0006) -- so it
    would prove the wiring works without ever running a rule. A block starting
    next week is both realistic and the only way a route test exercises
    `off_weekday` and `rest_gap` at all.
    """
    today = after or dt.date.today()
    return today + dt.timedelta(days=7 - today.weekday())


START = next_monday()

# A month ago: a 1RM cannot be measured in the future, and `record_one_rep_maxes`
# refuses one that claims to have been.
TESTED_ON = dt.date.today() - dt.timedelta(days=30)


# --- The workflow, without Temporal ---------------------------------------


class _InProcessWorkflows:
    """What `api.temporal_client` does, minus the durable execution.

    Every call lands on the real `StrengthActivities`, so the route tests
    still exercise `approve_proposal`, the staleness check and the error
    translation -- the things a route test is actually about. What they do
    not exercise is timers, signals and `continue_as_new`, which is what
    `test_block_workflow_run.py` is for.

    The alternative was a Temporal server per route test. It would have made
    the suite minutes longer and would have failed for reasons that had
    nothing to do with the routes.
    """

    def __init__(self, session: Session) -> None:
        self._activities = StrengthActivities(lambda: _no_commit(session))
        self.started: dict[str, uuid.UUID] = {}
        self.signals: list[tuple[str, str, tuple]] = []

    async def start_block_workflow(self, block_id, *, first_proposal_id=None) -> str:
        workflow_id = contracts.workflow_id_for(block_id)
        self.started[workflow_id] = block_id
        self._activities.bind_workflow(
            contracts.BindRequest(block_id=block_id, workflow_id=workflow_id)
        )
        if first_proposal_id is not None:
            self._activities.commit_proposal(_decide(first_proposal_id))
        return workflow_id

    async def update(self, workflow_id: str, name: str, *args, result_type=None):
        if name == contracts.APPROVE:
            return self._activities.commit_proposal(_decide(args[0]))
        if name == contracts.REJECT:
            reason = args[1] if len(args) > 1 else None
            return self._activities.reject_proposal(_decide(args[0], reason=reason))
        if name == contracts.EDIT:
            return self._activities.apply_edit(
                contracts.EditRequest(proposal_id=args[0], as_of=_now(), changes=tuple(args[1]))
            )
        raise AssertionError(f"no in-process stand-in for update {name!r}")

    async def signal(self, workflow_id: str, name: str, *args) -> None:
        self.signals.append((workflow_id, name, args))

    async def query(self, workflow_id: str, name: str, *, result_type=None):
        block_id = self.started.get(workflow_id)
        if block_id is None:
            raise AssertionError(f"queried a workflow that was never started: {workflow_id}")
        return contracts.WorkflowStateView(state="ACTIVE", block_id=block_id)

    async def is_running(self, workflow_id: str) -> bool:
        return workflow_id in self.started

    async def ping(self) -> bool:
        return True


def _decide(proposal_id: uuid.UUID, reason: str | None = None) -> contracts.DecideRequest:
    return contracts.DecideRequest(proposal_id=proposal_id, as_of=_now(), reason=reason)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@contextlib.contextmanager
def _no_commit(session: Session):
    """The activity's unit of work, sharing the test's own session.

    It flushes instead of committing: the route that called it owns the
    transaction here, which is exactly the arrangement spec 15 replaced in
    production and the one the route tests still want.
    """
    session.flush()
    yield session
    session.flush()


@pytest.fixture(autouse=True)
def _in_process_workflows(monkeypatch, db_session: Session) -> _InProcessWorkflows:
    """Autouse, because a route that reaches a real Temporal in a unit test
    is a hang waiting to happen -- and because every approval route goes
    through the workflow now, not just the ones that look like it."""
    fake = _InProcessWorkflows(db_session)
    for name in ("start_block_workflow", "update", "signal", "query", "is_running", "ping"):
        monkeypatch.setattr(temporal_client, name, _translating(getattr(fake, name)))
    monkeypatch.setattr(temporal_client, "get_client", _refuse_to_connect)
    return fake


def _translating(fn):
    """Unwrap an activity's refusal, exactly as the real client does.

    An activity turns `PlanInvalid` into an `ApplicationError` so it can
    cross a process boundary, and `temporal_client` turns it back so
    `api/main.py` still answers 422. Without this the stand-in would be
    kinder than production and every refusal would surface as a 500 -- which
    is precisely the class of bug these route tests exist to catch.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised as itself
            temporal_errors.reraise_domain(exc)

    return wrapper


async def _refuse_to_connect():
    raise AssertionError(
        "a route test tried to reach a real Temporal; add a stand-in to _InProcessWorkflows instead"
    )


@pytest.fixture
def athlete(db_session: Session) -> models.Athlete:
    """An athlete with 1 kg plates, rounding down -- the author's own settings."""
    row = golden.athlete(increment="1.0", rounding="floor")
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def template(db_session: Session, golden_entries) -> models.BlockTemplate:
    """The authored template and all 77 of its entries, as database rows."""
    row = golden.template()
    db_session.add(row)
    db_session.flush()
    for entry in golden_entries:
        db_session.add(
            models.TemplateEntry(
                template_id=row.id,
                lift_code=entry.lift_code,
                variation=entry.variation,
                week=entry.week,
                slot=entry.slot,
                sets=entry.sets,
                reps=entry.reps,
                pct_num=entry.pct_num,
                pct_den=entry.pct_den,
                is_test=entry.is_test,
            )
        )
    db_session.flush()
    return row


@pytest.fixture
def maxes(db_session: Session, athlete: models.Athlete) -> dict[str, Decimal]:
    """Real, athlete-reported maxes. Nothing synthetic in this suite."""
    for lift_code, kg in API_MAXES.items():
        db_session.add(
            models.OneRepMax(
                athlete_id=athlete.id,
                lift_code=lift_code,
                kg=kg,
                set_on=TESTED_ON,
                source="tested",
            )
        )
    db_session.flush()
    return dict(API_MAXES)


@pytest.fixture
def weekday_map(db_session: Session, athlete: models.Athlete) -> dict[int, int]:
    for slot, weekday in MON_WED_FRI.items():
        db_session.add(models.WeekdayMap(athlete_id=athlete.id, slot=slot, weekday=weekday))
    db_session.flush()
    return dict(MON_WED_FRI)


@pytest.fixture
def ready(athlete, template, maxes, weekday_map) -> dict[str, object]:
    """Everything a block needs: an athlete, maxes, days and a template."""
    return {"athlete": athlete, "template": template, "maxes": maxes}


def block_body(athlete: models.Athlete, template: models.BlockTemplate, **overrides) -> dict:
    body = {
        "athlete_id": str(athlete.id),
        "template_id": str(template.id),
        "lifts": ALL_LIFTS,
        "start_date": START.isoformat(),
    }
    body.update(overrides)
    return body


def propose(api_client, athlete, template, **overrides) -> tuple[uuid.UUID, uuid.UUID]:
    """`POST /blocks`. Returns `(block_id, proposal_id)`."""
    response = api_client.post("/blocks", json=block_body(athlete, template, **overrides))
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["block_id"]), uuid.UUID(body["proposal"]["id"])


@pytest.fixture
def chat_client(api_client, request):
    """`api_client` plus an injected pair of models (spec 13).

    Layered on top rather than folded into `api_client`, so every existing
    route suite keeps the client it already had. The models come from the test
    through `request.param`, or default to a pair that refuses on use -- which
    is what makes a filter test's "no LLM was called" assertion mean something
    at the route level too.
    """
    from api.deps import get_chat_models
    from api.main import app
    from domains.strength.agent.schemas import ChatModels
    from tests.fakes import RefusingModel

    models_ = getattr(request, "param", None) or ChatModels(
        router=RefusingModel(), agent=RefusingModel()
    )
    app.dependency_overrides[get_chat_models] = lambda: models_
    try:
        yield api_client
    finally:
        app.dependency_overrides.pop(get_chat_models, None)
