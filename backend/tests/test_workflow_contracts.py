"""`contracts.py` must stay importable inside Temporal's workflow sandbox.

A workflow replays its own history to recover, so every line of it has to
produce the same result the second time (CLAUDE.md non-negotiable #6). Temporal
enforces that with a sandbox that reloads the workflow's modules per run and
forbids the obviously non-deterministic ones.

`contracts.py` is what `block_workflow.py` imports instead of `activities.py`,
and the whole arrangement only works while it stays pure. One `from
domains.strength import services` added here for convenience would drag
SQLAlchemy, CP-SAT, numpy and simpy into the sandbox, and the failure would
show up as a slow, confusing workflow-task failure at runtime rather than as
anything resembling its cause.

So this is prose turned into a test, the way `test_tracing.py` and
`test_calendar_solver.py` already do it for their own rules.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path
from typing import get_args

from domains.strength import enums, missed_policy
from domains.strength.workflows import contracts

BACKEND = Path(__file__).resolve().parents[1]
CONTRACTS = BACKEND / "domains" / "strength" / "workflows" / "contracts.py"

# Everything a workflow may not reach, directly or by import. The first group
# is non-deterministic by nature; the second is heavy machinery that has no
# business in a replay and that would pull the first group in behind it.
BANNED_ROOTS = frozenset(
    {
        "random",
        "secrets",
        "time",
        "socket",
        "zoneinfo",
        "sqlalchemy",
        "sqlmodel",
        "psycopg",
        "alembic",
        "ortools",
        "numpy",
        "simpy",
        "polars",
        "openpyxl",
        "httpx",
        "pydantic_ai",
        "langfuse",
        "opentelemetry",
    }
)

# The only project modules `contracts.py` may import. Each is itself nothing
# but `Literal`s and Pydantic models -- check that again before adding a third.
ALLOWED_PROJECT_MODULES = frozenset(
    {
        "domains.strength.enums",
        "domains.strength.schemas",
    }
)


def _imports(path: Path) -> list[str]:
    """Every module `path` imports, resolved to the module actually pulled in.

    `from domains.strength import enums` names the module `domains.strength`
    in the AST, but what it loads is `domains.strength.enums`. Resolving that
    is the difference between a rule that reads "only these two modules" and
    one that reads "anything under domains.strength", which is no rule at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            for alias in node.names:
                candidate = f"{module}.{alias.name}"
                found.append(candidate if _is_module(candidate) else module)
    return found


def _is_module(dotted: str) -> bool:
    base = BACKEND / Path(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def test_contracts_imports_nothing_the_sandbox_forbids():
    offenders = [name for name in _imports(CONTRACTS) if name.split(".")[0] in BANNED_ROOTS]

    assert offenders == []


def test_contracts_imports_only_the_two_pure_project_modules():
    """A fourth project import is the likely way this rule gets broken.

    `services`, `missed_policy`, `generator` and `models` all look harmless
    from the call site and all reach the database or the solver.
    """
    project = [
        name
        for name in _imports(CONTRACTS)
        if name.split(".")[0] in {"domains", "planner_core", "api", "scripts"}
    ]

    assert set(project) <= ALLOWED_PROJECT_MODULES, sorted(set(project) - ALLOWED_PROJECT_MODULES)


def test_the_modules_contracts_does_import_are_themselves_pure():
    """Purity is transitive, and this is the half that is easy to forget.

    `enums` and `schemas` are safe today. If either grew an import of
    `models`, `contracts.py` would still look clean while the sandbox filled
    up with SQLAlchemy.
    """
    for module in sorted(ALLOWED_PROJECT_MODULES):
        path = BACKEND / Path(*module.split(".")).with_suffix(".py")
        offenders = [name for name in _imports(path) if name.split(".")[0] in BANNED_ROOTS]

        assert offenders == [], f"{module} imports {offenders}"


def test_that_check_would_actually_catch_one():
    """Guards the guard. A matcher that finds nothing passes the tests above.

    `services.py` genuinely imports SQLAlchemy, so pointing the same walk at
    it is what proves the walk works at all.
    """
    hits = [
        name
        for name in _imports(BACKEND / "domains" / "strength" / "services.py")
        if name.split(".")[0] in BANNED_ROOTS
    ]

    assert hits != []


# --- The vocabulary the two sides share ----------------------------------


def test_the_tier_the_workflow_reports_is_the_tier_the_policy_decides():
    """One definition, so a new outcome cannot reach only half the system.

    `MissedTier` moved into `enums` for exactly this reason, following
    `HISTORY_STATUSES` (ADR-0010): the policy cannot be imported here, so
    without a shared source the two would drift and the drift would be
    invisible until a workflow reported a tier the UI had never heard of.
    """
    assert missed_policy.Tier is enums.MissedTier
    assert get_args(enums.MissedTier) == ("in_week_move", "week_shift", "fold", "restart")


def test_every_activity_name_appears_in_the_registry_tuple():
    """`ACTIVITY_NAMES` is what the worker-registration test checks against."""
    declared = {
        value
        for name, value in vars(contracts).items()
        if name.isupper() and isinstance(value, str) and name not in {"APPROVE", "REJECT", "EDIT"}
    }

    assert set(contracts.ACTIVITY_NAMES) <= declared
    assert len(set(contracts.ACTIVITY_NAMES)) == len(contracts.ACTIVITY_NAMES)


# --- The data converter's job --------------------------------------------


def test_a_decimal_survives_the_round_trip_as_a_decimal():
    """CLAUDE.md non-negotiable #4, at the one boundary that can quietly break it.

    A kilogram that becomes a float between the activity and the workflow is
    not a rounding bug anybody would notice until a weight came out as
    102.50000000000001.
    """
    before = contracts.FinishedBlock(
        block_id=uuid.uuid4(),
        new_one_rms={"squat": Decimal("147.5"), "bench": Decimal("97.5")},
        written=("squat",),
    )

    after = contracts.FinishedBlock.model_validate_json(before.model_dump_json())

    assert after == before
    assert after.new_one_rms["squat"] == Decimal("147.5")
    assert isinstance(after.new_one_rms["squat"], Decimal)


def test_the_contracts_are_frozen_and_closed():
    """Frozen because a workflow holds them across an await and must not find
    them changed; closed because a field added on one side and not the other
    should fail loudly rather than be dropped in transit."""
    for name, value in vars(contracts).items():
        if (
            isinstance(value, type)
            and issubclass(value, contracts.Contract)
            and value is not contracts.Contract
        ):
            assert value.model_config.get("frozen") is True, name
            assert value.model_config.get("extra") == "forbid", name


def test_a_session_still_planned_with_a_past_date_is_what_the_workflow_sees():
    """ADR-0010's rule, expressed in the type the workflow reads.

    `missed` means written off. A session the athlete may still make up keeps
    `planned`, which is what lets the calendar solver re-place it -- so
    `planned` must return it even though its day has gone.
    """
    yesterday = dt.date(2026, 10, 17)
    state = contracts.WorkflowBlockState(
        block_id=uuid.uuid4(),
        athlete_id=uuid.uuid4(),
        status="active",
        start_date=dt.date(2026, 10, 12),
        sessions=(
            _session(seq=2, date=dt.date(2026, 10, 19), status="planned"),
            _session(seq=1, date=yesterday, status="planned"),
            _session(seq=3, date=dt.date(2026, 10, 14), status="done"),
        ),
    )

    assert [s.seq for s in state.planned] == [1, 2]
    assert state.planned[0].date == yesterday


def _session(*, seq: int, date: dt.date, status: str) -> contracts.WorkflowSession:
    return contracts.WorkflowSession(
        id=uuid.uuid4(),
        seq=seq,
        week=1,
        slot=1,
        date=date,
        status=status,  # type: ignore[arg-type]
        deadline_utc=dt.datetime.combine(date, dt.time(12, 0), tzinfo=dt.UTC),
    )


# --- Every workflow the API can start must be registered on the worker ----


def test_the_worker_registers_every_workflow_the_api_can_start() -> None:
    """A workflow nobody runs HANGS the caller; it does not fail.

    `TemporalRunner` starts a workflow per job and awaits its result. A
    workflow class the worker does not register is rejected with "not
    registered on this worker", retried for ever, and never produces one -- so
    the caller waits, and the route that promised to answer anyway answers
    nothing. Logging a session did exactly that: the log committed and the
    response never arrived, because `IndexNoteWorkflow`, `SimulateWorkflow`
    and `ShapePreviewWorkflow` were defined and registered nowhere.

    Comparing the two lists catches the next one for free.
    """
    from domains.strength.workflows import block_workflow, worker

    defined = {
        node.name
        for node in ast.walk(ast.parse(Path(block_workflow.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "defn"
            for d in node.decorator_list
        )
    }
    registered = {
        name.id
        for node in ast.walk(ast.parse(Path(worker.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.keyword) and node.arg == "workflows"
        for name in ast.walk(node.value)
        if isinstance(name, ast.Name)
    }

    assert defined, "no workflows found; this test is looking in the wrong place"
    assert defined <= registered, (
        f"defined but never registered on the worker: {sorted(defined - registered)}"
    )


def test_the_worker_gives_its_activities_an_embedder() -> None:
    """Without one, indexing a note SUCCEEDS having done nothing.

    `StrengthActivities.index_note` returns `skipped="no embedder configured"`
    when none was passed, so the workflow completes, the route answers 201 and
    the note never reaches the retrieval index -- a failure that looks exactly
    like success from every side. Only the tests were passing an embedder,
    which is precisely why the suite could not see it.
    """
    source = Path(worker_module().__file__).read_text(encoding="utf-8")
    construction = next(line for line in source.splitlines() if "StrengthActivities(" in line)
    assert "embedder_factory" in construction, (
        f"the worker builds its activities without an embedder: {construction.strip()}"
    )


def worker_module():
    from domains.strength.workflows import worker

    return worker
