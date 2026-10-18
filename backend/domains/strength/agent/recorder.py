"""One decorator that both records a tool call and opens its span.

Spec 13 §5A needs every tool call appended to `decision.tool_calls`; spec 14
§5C needs each one wrapped in a `tool.<name>` span. Those are the same event,
so they are one decorator rather than two things to keep in step -- forget the
second at one call site and the trace and the audit row disagree, with nothing
to notice it.

**Why a contextvar.** A tool is reached two ways. For `plan`, `reschedule` and
`simulate`, `agent.py` calls it directly: same thread, same context. For the
`answer` route, Pydantic AI calls it -- and a sync `@agent.tool_plain` runs in
an anyio worker thread. `anyio.to_thread.run_sync` does `copy_context()` at
submission, so the worker sees both this contextvar and OpenTelemetry's
current span. Verified by probe against anyio as installed, not assumed.

That is the whole reason for a contextvar rather than Pydantic AI's own
`RunContext`/`deps`: `deps` is the idiomatic choice for the LLM path, but the
plain path would have to synthesise a `RunContext` to use it. The contextvar is
identical on both.

With no recorder active -- a unit test calling a tool directly -- the decorator
still opens its span and simply has nowhere to append. That is what keeps the
tools testable in isolation.

`time.perf_counter` here is deliberate and in scope: ADR-0009's prohibition on
`time` covers `calendar_solver.py`, which must stay pure. `duration_ms` is the
*tool's* wall time. Solver and simulator wall time still comes only from their
own `SolverStats` / `SimStats`, never from a clock.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

from domains.strength.agent.schemas import ToolCall
from planner_core import tracing

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_CURRENT: ContextVar[Recorder | None] = ContextVar("tool_recorder", default=None)

# Arguments that must never reach a trace or an audit row. Sessions and
# embedders are machinery; an athlete is a whole DB row; `settings` would carry
# secrets. Spec 14 §10 forbids keys and full rows in span attributes, and the
# cheapest way to honour that is to never put them in.
#
# `obj` and `fallback` are the explainer's whole payload and its precomputed
# reply. Recorded, a simulation payload would sit in `decision.tool_calls` *and*
# on the span as an uncapped repr -- the full object, not a summary (spec 14
# §10). The verdict is recorded instead, via the tool's own `summarise`.
HIDDEN_ARGS = frozenset(
    {
        "session",
        "embedder",
        "athlete",
        "models",
        "model",
        "ctx",
        "settings",
        "history",
        "obj",
        "fallback",
    }
)

# One summary value should not be able to bloat a row or a span.
MAX_ATTR_CHARS = 300


@dataclass
class Recorder:
    """The tool calls made during one chat turn, in the order they happened."""

    calls: list[ToolCall] = field(default_factory=list)

    def append(self, call: ToolCall) -> None:
        self.calls.append(call)

    def dump(self) -> list[dict[str, Any]]:
        """JSON for the `tool_calls` JSONB column."""
        return [call.model_dump(mode="json") for call in self.calls]


@contextmanager
def recording() -> Iterator[Recorder]:
    """Collect every decorated call made inside this block."""
    recorder = Recorder()
    token = _CURRENT.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT.reset(token)


def current() -> Recorder | None:
    """The active recorder, or `None` outside a turn."""
    return _CURRENT.get()


def _truncate(value: object) -> object:
    """Cap a recorded value's size, whatever shape it is.

    Strings were capped from the start; a list or a dict was not, which is how
    a whole payload could reach a span as one unbounded `repr`.
    """
    if isinstance(value, str):
        return value[:MAX_ATTR_CHARS] + "..." if len(value) > MAX_ATTR_CHARS else value
    if isinstance(value, list | dict):
        text = repr(value)
        return text[:MAX_ATTR_CHARS] + "..." if len(text) > MAX_ATTR_CHARS else value
    return value


def _describe(
    fn: Callable[..., Any],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> dict[str, Any]:
    """The tool's arguments, minus the machinery, as something JSON-dumpable.

    Bound by name through the signature rather than by position, so the entry
    reads the same whether the caller passed positionally or by keyword.
    """
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        return {
            name: _truncate(_jsonable(value))
            for name, value in bound.arguments.items()
            if name not in HIDDEN_ARGS
        }
    except Exception:  # noqa: BLE001 - a trace detail must not break a tool
        logger.warning("recorder: could not describe the input to %r", fn, exc_info=True)
        return {}


def _jsonable(value: object) -> Any:
    """Enough coercion for a summary. Not a serialiser -- see `ToolCall.input`."""
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            return repr(value)
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _emit(span: tracing.Span, call: ToolCall) -> None:
    """Put the entry on the span and in the recorder, if either is listening."""
    for key, value in call.output_summary.items():
        span.set(f"output.{key}", value)
    if call.solver_stats:
        for key, value in call.solver_stats.items():
            span.set(f"solver.{key}", value)
    if call.validation:
        for key, value in call.validation.items():
            span.set(f"validation.{key}", value)
    span.set("duration_ms", call.duration_ms)
    if call.error is not None:
        span.set("error", call.error)

    recorder = current()
    if recorder is not None:
        recorder.append(call)


def records(
    name: str,
    *,
    summarise: Callable[[Any], dict[str, Any]] | None = None,
    stats: Callable[[Any], dict[str, Any] | None] | None = None,
    validation: Callable[[Any], dict[str, Any] | None] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Record calls to this function as a `ToolCall` and a `tool.<name>` span.

    `summarise`, `stats` and `validation` each take the return value and
    produce part of the entry. They are passed in rather than inferred because
    what is worth recording differs per tool -- a plan's session count, a
    solver's status, a validator's violated rules -- and because a summary that
    dumped the whole return value would put plan payloads in every trace.

    A raising tool still gets an entry, with `error` set, and the exception
    continues untouched: a turn that failed halfway is exactly the one worth
    reading afterwards.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracing.traced(f"tool.{name}") as span:
                described = _describe(fn, args, kwargs)
                for key, value in described.items():
                    span.set(f"input.{key}", value)

                started = time.perf_counter()
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    _emit(
                        span,
                        ToolCall(
                            name=name,
                            input=described,
                            duration_ms=_elapsed_ms(started),
                            # Truncated: a SQLAlchemy or psycopg message can
                            # carry the SQL *parameters*, which for a note is
                            # the note.
                            error=f"{type(exc).__name__}: {str(exc)[:MAX_ATTR_CHARS]}",
                        ),
                    )
                    raise

                _emit(
                    span,
                    ToolCall(
                        name=name,
                        input=described,
                        output_summary=_safely(summarise, result, "summarise", name),
                        duration_ms=_elapsed_ms(started),
                        solver_stats=_safely(stats, result, "stats", name) or None,
                        validation=_safely(validation, result, "validation", name) or None,
                    ),
                )
                return result

        return wrapper

    return decorate


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _safely(
    extract: Callable[[Any], dict[str, Any] | None] | None,
    result: object,
    what: str,
    tool: str,
) -> dict[str, Any]:
    """Run one summary callback. A broken summary must not fail the tool."""
    if extract is None:
        return {}
    try:
        return extract(result) or {}
    except Exception:  # noqa: BLE001 - as above
        logger.warning("recorder: %s failed for tool %r", what, tool, exc_info=True)
        return {}
