"""The rules that keep `BlockWorkflow` replayable, as tests rather than prose.

A Temporal workflow recovers by re-executing its own code against its
recorded history. Every line therefore has to produce the same result the
second time, which is CLAUDE.md non-negotiable #6 and spec 15 §10. The usual
way that breaks is not dramatic -- someone adds `datetime.now()` to work out
whether a session is late, or imports `services` to avoid a round trip, and
the workflow keeps working perfectly until the day a worker restarts.

So the boundary is machine-checked here, the same way `test_tracing.py` checks
the tracing backend and `test_calendar_solver.py` checks the solver's purity.
These tests need no database and no Temporal server; the behaviour they
protect is exercised in `tests/api/test_block_workflow_run.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from domains.strength.workflows import contracts
from domains.strength.workflows.activities import StrengthActivities
from domains.strength.workflows.block_workflow import BlockWorkflow

BACKEND = Path(__file__).resolve().parents[1]
WORKFLOW = BACKEND / "domains" / "strength" / "workflows" / "block_workflow.py"

# Anything whose answer could differ between the first run and the replay,
# plus the heavy machinery that would drag the first group in behind it.
BANNED_IMPORTS = frozenset(
    {
        "random",
        "secrets",
        "time",
        "socket",
        "zoneinfo",
        "sqlalchemy",
        "sqlmodel",
        "psycopg",
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

# Project modules the workflow must not reach. Each is either impure itself
# or a door to something that is: `services` opens transactions, `generator`
# and `missed_policy` reach the solver, `models` is SQLAlchemy, `config`
# reads the environment.
BANNED_PROJECT_MODULES = frozenset(
    {
        "domains.strength.services",
        "domains.strength.models",
        "domains.strength.generator",
        "domains.strength.missed_policy",
        "domains.strength.simulator",
        "domains.strength.notes",
        "domains.strength.calendar_solver",
        "domains.strength.shaping_solver",
        "domains.strength.validator",
        "domains.strength.workflows.activities",
        "planner_core.db",
        "planner_core.config",
        "planner_core.tracing",
    }
)

# Calls whose value is "whatever is true right now". `workflow.now()` is the
# one clock a workflow may read, because Temporal replays it from history.
BANNED_CALLS = {
    ("datetime", "now"),
    ("datetime", "today"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
    ("time", "monotonic"),
    ("uuid", "uuid4"),
    ("uuid", "uuid1"),
    ("random", "random"),
    ("random", "choice"),
    ("random", "randint"),
    ("os", "environ"),
}


def _tree() -> ast.Module:
    return ast.parse(WORKFLOW.read_text(encoding="utf-8"))


def _imported(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            for alias in node.names:
                candidate = f"{module}.{alias.name}"
                names.append(candidate if _is_module(candidate) else module)
    return names


def _is_module(dotted: str) -> bool:
    base = BACKEND / Path(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _attribute_calls(tree: ast.Module) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            found.add((func.value.id, func.attr))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
            found.add((func.value.attr, func.attr))
    return found


# --- Imports --------------------------------------------------------------


def test_the_workflow_imports_nothing_that_could_replay_differently():
    offenders = [name for name in _imported(_tree()) if name.split(".")[0] in BANNED_IMPORTS]

    assert offenders == []


def test_the_workflow_cannot_reach_the_database_or_the_solver():
    """Importing `activities` would be the convenient mistake, and it drags
    SQLAlchemy, CP-SAT, numpy and simpy into the sandbox through `services`.

    This is why activities are named by string at every call site, and why
    the two tests below exist to buy the type safety back.
    """
    offenders = [name for name in _imported(_tree()) if name in BANNED_PROJECT_MODULES]

    assert offenders == []


def test_contracts_is_imported_under_passthrough():
    """Pydantic v2 is a compiled extension, and the sandbox re-imports a
    workflow's modules per run. Letting it reimport Pydantic is slow at best
    and breaks at worst."""
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "with workflow.unsafe.imports_passed_through():" in source
    passthrough = source.split("with workflow.unsafe.imports_passed_through():", 1)[1]
    assert "from domains.strength.workflows import contracts" in passthrough.split("\n\n", 1)[0]


# --- Calls ----------------------------------------------------------------


def test_the_workflow_reads_no_clock_but_temporals():
    """`workflow.now()` replays from history; `datetime.now()` does not.

    A deadline computed from the wall clock would be a different deadline on
    every replay, and the workflow would decide a session was missed that it
    had previously decided was not.
    """
    offenders = sorted(_attribute_calls(_tree()) & BANNED_CALLS)

    assert offenders == []


def test_the_workflow_does_use_temporals_clock():
    """Guards the guard: a workflow that read no clock at all would also
    pass the test above, and would also never notice a missed session."""
    assert ("workflow", "now") in _attribute_calls(_tree())


def test_that_check_would_actually_catch_one():
    """`services.py` really does read the clock, in `today_for`."""
    tree = ast.parse((BACKEND / "domains" / "strength" / "services.py").read_text("utf-8"))
    calls = _attribute_calls(tree)

    assert calls & BANNED_CALLS


# --- The string-named activities ------------------------------------------


def test_every_name_the_workflow_can_call_is_a_registered_activity():
    """Half the price of naming activities by string: a typo is a runtime
    failure minutes into a block, not an import error."""
    registered = {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for fn in StrengthActivities().all()
    }

    assert set(contracts.ACTIVITY_NAMES) == registered


def test_every_activity_the_workflow_calls_has_a_constant():
    """The other half: an activity called by a bare string literal would
    dodge the check above entirely."""
    source = WORKFLOW.read_text(encoding="utf-8")
    called = {
        node.args[0].attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_activity"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
    }

    assert called, "no activity calls found -- has the call shape changed?"
    assert all(getattr(contracts, name) in contracts.ACTIVITY_NAMES for name in called), sorted(
        called
    )


def test_no_activity_is_called_by_a_bare_string():
    literals = {
        node.args[0].value
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_activity"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    assert literals == set()


# --- The handler surface --------------------------------------------------


def test_approve_reject_and_edit_answer_their_caller():
    """Spec 15 §3 made these signals. They cannot be.

    `POST /proposals/{id}/approve` returns the decided `ProposalOut` -- that
    is the existing contract, `tests/api/test_flow.py` asserts the approved
    plan immediately afterwards, and spec 16 generates frontend types from
    it. A signal is fire-and-forget.
    """
    defn = BlockWorkflow.__temporal_workflow_definition  # type: ignore[attr-defined]

    assert set(defn.updates) >= {contracts.APPROVE, contracts.REJECT, contracts.EDIT}


def test_the_four_fire_and_forget_events_are_signals():
    """These carry nothing the database does not already have, which is why
    the log route can commit first and signal afterwards."""
    defn = BlockWorkflow.__temporal_workflow_definition  # type: ignore[attr-defined]

    assert set(defn.signals) >= {
        contracts.SESSION_LOGGED,
        contracts.REPORT_UNAVAILABLE,
        contracts.PAUSE,
        contracts.RESUME,
    }


def test_the_state_can_be_queried_without_disturbing_it():
    defn = BlockWorkflow.__temporal_workflow_definition  # type: ignore[attr-defined]

    assert set(defn.queries) >= {contracts.CURRENT_STATE, contracts.NEXT_SESSION}


# --- Tunables are declared as guesses -------------------------------------


def test_the_two_guessed_thresholds_say_they_are_guesses():
    """CLAUDE.md: a threshold that is a starting value must be labelled one
    in the code, not presented as derived."""
    source = (BACKEND / "domains" / "strength" / "workflows" / "contracts.py").read_text("utf-8")

    for name in ("GRACE_HOURS", "MISSED_RETRY_COOLDOWN_HOURS"):
        before = source.split(f"{name} =", 1)[0]
        comment = before.rsplit("\n\n", 1)[-1]
        assert "starting value" in comment, name
