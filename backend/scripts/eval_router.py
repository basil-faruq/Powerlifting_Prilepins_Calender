"""Score the router against 30 labelled messages, and print a confusion table.

Spec 13 §5E. Layer 2 is the one part of the agent whose quality is a property
of the *model*, not of the code, so it needs measuring rather than asserting --
and measuring it on the model that will actually run.

What this measures is the route only. Parameter extraction is covered by
`tests/api/test_router.py` against a fixture model; what a real model can get
wrong, and what nothing else checks, is which of four actions a sentence is
asking for.

No database and no athlete: the prompt's context is supplied from the fixed
values below, so the score is about the model and not about whatever rows
happen to be in the developer's database.

    uv run python -m scripts.eval_router
    uv run python -m scripts.eval_router --cases tests/fixtures/router_cases.yaml
    uv run python -m scripts.eval_router --verbose

Costs money on `LLM_PROVIDER=anthropic`, so that path requires `--yes`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pydantic_ai
import yaml

from domains.strength import enums
from domains.strength.agent.router import RouterContext, select_route
from planner_core.config import get_settings
from planner_core.errors import LlmUnavailable
from planner_core.llm import get_model

# The banner is for interactive use and this writes a table.
pydantic_ai.BANNER_ENABLED = False

CASES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "router_cases.yaml"

# The four routes the model may choose. `safety` and `clarify` are layer 1's,
# and no case in the fixture should reach the model at all if it would fire a
# filter -- so they are not scored here.
ROUTES = ("answer", "plan", "reschedule", "simulate")

# Spec 13 §12's target, on Claude. The Ollama number is recorded honestly
# rather than aimed at.
TARGET = 0.90

# A fixed context, so the same fixture scores the same way on any machine. A
# Monday, which is what the template's week grid starts on, and the author's
# own Mon/Wed/Fri.
TODAY = dt.date(2026, 3, 2)
CONTEXT = RouterContext(
    today=TODAY,
    weekdays={0: "Monday", 1: "Wednesday", 2: "Friday"},
    block_week=3,
    block_weeks=11,
    lifts=tuple(enums.LIFT_CODES),
    has_one_rms=True,
    has_block=True,
)


@dataclass(frozen=True, slots=True)
class Case:
    message: str
    expected: str
    notes: str | None = None


def load_cases(path: Path) -> list[Case]:
    """Read the fixture. `safe_load`, never `load`: it is data, not code."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [
        Case(message=entry["message"], expected=entry["expected"], notes=entry.get("notes"))
        for entry in raw["cases"]
    ]
    unknown = sorted({case.expected for case in cases} - set(ROUTES))
    if unknown:
        raise ValueError(f"fixture labels a route the model cannot choose: {unknown}")
    return cases


def confusion(results: list[tuple[Case, str | None]]) -> str:
    """Expected down, predicted across. `-` is an unusable structured output.

    A table rather than a single number because the failures of a small model
    are usually systematic -- every `simulate` read as `reschedule`, say --
    and that is actionable where "73%" is not.
    """
    columns = (*ROUTES, "-")
    counts: Counter[tuple[str, str]] = Counter(
        (case.expected, predicted or "-") for case, predicted in results
    )

    width = max(len(name) for name in columns) + 2
    header = "expected \\ got".ljust(16) + "".join(name.rjust(width) for name in columns)
    lines = [header, "-" * len(header)]
    for expected in ROUTES:
        row = expected.ljust(16) + "".join(
            str(counts[(expected, got)]).rjust(width) for got in columns
        )
        lines.append(row)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES, help="the labelled fixture")
    parser.add_argument(
        "--verbose", action="store_true", help="print every case, not only the misses"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm a paid run (required when LLM_PROVIDER=anthropic)",
    )
    args = parser.parse_args()

    settings = get_settings()
    model_name = (
        settings.anthropic_router_model
        if settings.llm_provider == "anthropic"
        else settings.ollama_router_model
    )
    # First line, every time: the provider is read from `.env`, and a stray
    # `LLM_PROVIDER=anthropic` would otherwise bill on the first run with
    # nothing on screen to say so.
    print(f"provider: {settings.llm_provider}   router model: {model_name}")
    sys.stdout.flush()

    if settings.llm_provider == "anthropic" and not args.yes:
        print(
            "\nThis would send 30 requests to the Anthropic API and cost money.\n"
            "Re-run with --yes if that is what you want.",
            file=sys.stderr,
        )
        return 2

    try:
        cases = load_cases(args.cases)
    except (OSError, KeyError, ValueError) as exc:
        print(f"could not read {args.cases}: {exc}", file=sys.stderr)
        return 2

    per_route = Counter(case.expected for case in cases)
    thin = sorted(route for route in ROUTES if per_route[route] < 6)
    print(f"cases: {len(cases)}   per route: {dict(sorted(per_route.items()))}")
    sys.stdout.flush()
    if thin:
        # Spec 13 §5E asks for at least six per route; a thinner one makes its
        # own accuracy a coin flip.
        print(f"warning: fewer than 6 cases for {thin}", file=sys.stderr)

    try:
        model = get_model("router")
    except LlmUnavailable as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    print()
    results: list[tuple[Case, str | None]] = []
    for index, case in enumerate(cases, start=1):
        try:
            choice = select_route(model, case.message, ctx=CONTEXT)
        except LlmUnavailable as exc:
            sys.stdout.flush()
            print(f"\n{exc}", file=sys.stderr)
            return 2
        predicted = choice.kind if choice is not None else None
        results.append((case, predicted))

        hit = predicted == case.expected
        if args.verbose or not hit:
            mark = "ok  " if hit else "MISS"
            print(f"{mark} {index:>2}. [{case.expected} -> {predicted or '-'}] {case.message}")
            if not hit and case.notes:
                print(f"       note: {case.notes.strip()}")
        # Flushed per case: a 14b model takes a few seconds each, and piped
        # stdout is block-buffered -- without this a `make eval-router | tee`
        # shows nothing at all until the run is over. The same reason
        # `check_llm.py` flushes.
        sys.stdout.flush()

    correct = sum(1 for case, predicted in results if predicted == case.expected)
    accuracy = correct / len(results) if results else 0.0
    unparsed = sum(1 for _case, predicted in results if predicted is None)

    print()
    print(confusion(results))
    print()
    print(f"accuracy: {correct}/{len(results)} = {accuracy:.0%}")
    if unparsed:
        print(f"unusable structured output: {unparsed}")
    print(f"target (spec 13 §12, on Claude): {TARGET:.0%}")

    if accuracy < TARGET:
        sys.stdout.flush()
        print(
            f"\nBelow target on {settings.llm_provider}. Record the number rather than "
            "tuning the fixture to it.\n"
            "If it is far below (under ~70%), spec 13's escape hatch is to split the "
            "router in two: one call for the kind alone, then parameter extraction "
            "typed to the chosen kind.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
