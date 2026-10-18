"""Langfuse tracing, and the no-op it is when the keys are absent.

Spec 14. One trace per chat turn, its id stored on the `decision` row, so the
UI can open the exact trace behind any change to the plan.

Three rules shape this module.

**Domain code imports `traced` and nothing else** (spec 14 §11). The Langfuse
SDK is imported *inside* `init_tracing`, never at module scope, so nothing in
`domains/` or `api/` can reach it even by accident -- and an AST test in
`tests/test_tracing.py` enforces that. What stays at module scope is the
OpenTelemetry *API*, which is the vendor-neutral half: `langfuse` installs
itself as the global `TracerProvider`, and `trace.get_tracer()` then finds it
without this module ever naming the vendor.

**Tracing's own failures are swallowed; the caller's are not.** Every OTel call
below is wrapped. A broken exporter, an unreachable Langfuse or a bad attribute
type must cost a log line, never a request (spec 14 §13). The body of a
`traced` block is the one thing never protected -- see `traced`.

**It is a true no-op with no keys.** Not merely cheap: `enabled()` is checked
before any tracer is asked for. The structural no-op would almost do it on its
own -- with no SDK installed `trace.get_tracer()` returns a `ProxyTracer` whose
spans do not record -- but `langfuse` brings the SDK in, and once *any* library
sets a real global provider, "disabled" would quietly become
real-spans-with-no-exporter. The flag is the guarantee; OTel's default is only
the fallback behind it.

Solver and simulator durations are *not* measured here. They are read from the
solvers' own `SolverStats` / `SimStats`, because `calendar_solver.py`,
`shaping_solver.py`, `simulator.py` and `validator.py` are pure and AST-tested
(ADR-0006/0009/0011/0012) -- `calendar_solver.py` may not even import `time`.
So spans wrap the *impure call sites* in `services.py` and the agent, which is
a deliberate departure from spec 14 §7; ADR-0016 records it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from planner_core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The instrumentation scope every span this module opens is attributed to.
TRACER_NAME = "powerlifting-planner"

# W3C trace-context, the standard way to say "this span belongs to that trace"
# in a string. Spec 15 needs it: a Temporal activity runs in another thread and
# may run in another process, and OpenTelemetry's current span lives in a
# contextvar that crosses neither. So the parent travels as an argument.
_PROPAGATOR = TraceContextTextMapPropagator()

DISABLED_MESSAGE = "Tracing disabled (no Langfuse keys)"


@dataclass(frozen=True, slots=True)
class _State:
    """What `init_tracing` resolved. `None` means it was never called."""

    enabled: bool
    host: str
    project_id: str | None


# Module globals rather than a singleton object: `init_tracing` is called once
# at startup and read from every request thread, and `reset_tracing` exists so
# a test that enables tracing does not leak into the next one -- the same
# reason `llm.reset_models()` exists, and it sits beside it in the autouse
# fixture in `tests/conftest.py`.
_state: _State | None = None
_client: Any = None


def enabled() -> bool:
    """True only when `init_tracing` found two keys and the client accepted them."""
    return _state is not None and _state.enabled


# --- Attribute coercion ---------------------------------------------------


def _attr(value: object) -> Any:
    """Coerce a value to something OTel will accept as an attribute.

    OTel takes strings, bools, ints, floats and homogeneous sequences of those,
    and logs a warning *per call* for anything else. The planner's natural
    types are none of those: weights are `Decimal`, dates are `date`, and ids
    are `UUID`. Coercing here means a call site never has to think about it.
    """
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        # str, not float: a weight that reaches a trace as 72.50000000000001 is
        # worse than useless, because it reads as the number we computed.
        return str(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple | set | frozenset):
        return [str(_attr(item)) for item in value]
    if value is None:
        return ""
    return repr(value)


class Span:
    """What `traced` yields: somewhere to put attributes, and a trace id.

    Two implementations, one interface -- `_NullSpan` when tracing is off and
    `_OtelSpan` when it is on -- so a call site never branches on `enabled()`.
    """

    def set(self, key: str, value: object) -> None:
        """Record one attribute. Never raises."""
        raise NotImplementedError

    def set_many(self, **attrs: object) -> None:
        """Record several attributes. Never raises."""
        for key, value in attrs.items():
            self.set(key, value)

    @property
    def trace_id(self) -> str | None:
        """The W3C trace id this span belongs to, or `None` when disabled."""
        raise NotImplementedError


class _NullSpan(Span):
    """The no-op. Shared, immutable, and the whole of the disabled path."""

    __slots__ = ()

    def set(self, key: str, value: object) -> None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None


_NULL_SPAN = _NullSpan()


class _OtelSpan(Span):
    """A real span, with every OTel call wrapped so it cannot break a request."""

    __slots__ = ("_span",)

    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def set(self, key: str, value: object) -> None:
        try:
            self._span.set_attribute(key, _attr(value))
        except Exception:  # noqa: BLE001 - an attribute must not break a request
            logger.warning("tracing: could not set attribute %r", key, exc_info=True)

    @property
    def trace_id(self) -> str | None:
        try:
            ctx = self._span.get_span_context()
            if not ctx.is_valid or ctx.trace_id == 0:
                return None
            return trace.format_trace_id(ctx.trace_id)
        except Exception:  # noqa: BLE001 - as above
            logger.warning("tracing: could not read the trace id", exc_info=True)
            return None


# --- The context manager --------------------------------------------------


@contextmanager
def traced(name: str, *, parent: str | None = None, **attrs: object) -> Iterator[Span]:
    """Open a span named `name`, or yield a no-op when tracing is off.

    The asymmetry in the error handling is the point of the whole module:
    everything *tracing* does is wrapped and logged, and the body's own
    exception is re-raised untouched. A failure to observe the work must never
    become a failure of the work (spec 14 §11).

    Nesting is normally automatic -- OpenTelemetry keeps the current span in a
    contextvar, so a `traced` inside another is its child without being told.
    `parent` is for the one case where that fails: a `traceparent` string from
    `current_traceparent`, naming a span in another thread or another process.
    Spec 15 needs it so the `approval` span lands in the same Langfuse trace as
    the reschedule it approves, which a Temporal activity could not otherwise
    know about.
    """
    if not enabled():
        yield _NULL_SPAN
        return

    try:
        context = _PROPAGATOR.extract({"traceparent": parent}) if parent else None
        manager = trace.get_tracer(TRACER_NAME).start_as_current_span(name, context=context)
        otel_span = manager.__enter__()
    except Exception:  # noqa: BLE001 - tracing must not break a request
        logger.warning("tracing: could not open span %r", name, exc_info=True)
        yield _NULL_SPAN
        return

    span = _OtelSpan(otel_span)
    span.set_many(**attrs)
    try:
        yield span
    except BaseException as exc:
        # Close the span recording the failure, then let the caller's exception
        # continue. `suppress` guards the *close*, never the re-raise.
        with contextlib.suppress(Exception):
            manager.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        with contextlib.suppress(Exception):
            manager.__exit__(None, None, None)


def current_trace_id() -> str | None:
    """The trace id of whatever span is current, or `None` when disabled.

    Langfuse v3+ is OpenTelemetry-native and uses the W3C trace id as its own,
    so this string is literally what `trace_url` needs -- no SDK call on the
    request path.
    """
    if not enabled():
        return None
    try:
        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid or ctx.trace_id == 0:
            return None
        return trace.format_trace_id(ctx.trace_id)
    except Exception:  # noqa: BLE001 - tracing must not break a request
        logger.warning("tracing: could not read the current trace id", exc_info=True)
        return None


def current_traceparent() -> str | None:
    """The current span as a W3C `traceparent`, for carrying somewhere else.

    `current_trace_id` names the *trace*; this names the trace **and the span
    inside it**, which is what a child in another process needs in order to
    hang off the right branch rather than off the root.
    """
    if not enabled():
        return None
    try:
        carrier: dict[str, str] = {}
        _PROPAGATOR.inject(carrier)
        return carrier.get("traceparent")
    except Exception:  # noqa: BLE001 - tracing must not break a request
        logger.warning("tracing: could not read the current traceparent", exc_info=True)
        return None


def trace_url(trace_id: str | None) -> str | None:
    """A link to the trace in Langfuse, or `None` when it cannot be built.

    `None` when tracing is off, when there is no id, *and* when
    `LANGFUSE_PROJECT_ID` is unset -- spec 14 §5D's "disabled or ids missing".
    Tracing can be working while the project id is absent, because the keys are
    what the SDK needs and the project id is only what the URL needs.
    """
    state = _state
    if trace_id is None or state is None or not state.enabled or not state.project_id:
        return None
    return f"{state.host.rstrip('/')}/project/{state.project_id}/traces/{trace_id}"


# --- Startup and shutdown -------------------------------------------------


def _export_filter():
    """Which spans Langfuse exports: its defaults, plus every span of ours.

    Langfuse 5's default filter exports only its own spans, spans carrying
    `gen_ai.*` attributes, and a fixed list of known instrumentation scopes.
    Ours -- `chat_turn`, `tool.*`, `validator`, `solver.*`, `simulator.run` --
    is none of those, so with the default every one of them was silently
    dropped and a trace held only Pydantic AI's model spans, with no
    application root. The IDs and the `trace_url` were still well-formed, so
    nothing would have said so (found by review, from the installed source).

    Imported lazily like everything Langfuse. If the submodule is missing the
    filter degrades to ours alone rather than failing startup.
    """
    try:
        from langfuse.span_filter import is_default_export_span
    except ImportError:

        def is_default_export_span(span: object) -> bool:
            return False

    def export(span: object) -> bool:
        scope = getattr(span, "instrumentation_scope", None)
        if scope is not None and getattr(scope, "name", None) == TRACER_NAME:
            return True
        return bool(is_default_export_span(span))

    return export


def _discard(client: Any) -> None:
    """Shut down a client we decided not to use. A rejected one still owns a
    background thread and has installed itself as the global provider."""
    with contextlib.suppress(Exception):
        client.shutdown()


def init_tracing(settings: Settings | None = None) -> bool:
    """Turn tracing on if both Langfuse keys are present. Never raises.

    Returns whether tracing ended up enabled, which is what makes this
    testable without reading a log. Idempotent: calling it twice with the same
    settings leaves one client, because `api/main.py` calls it at startup and a
    test may call it again.

    Every failure mode -- no keys, no package, bad keys, unreachable host --
    ends the same way: a log line and `False`. An app that cannot be observed
    still has to run (spec 14 §1).
    """
    global _state, _client

    settings = settings or get_settings()

    public = settings.langfuse_public_key
    secret = settings.langfuse_secret_key
    if public is None or secret is None:
        # Blank is already `None` by the time it reaches here: `Settings`
        # normalises it, because `.env.example` ships these keys empty.
        logger.info(DISABLED_MESSAGE)
        _state = _State(enabled=False, host=settings.langfuse_host, project_id=None)
        _client = None
        return False

    if _client is not None and enabled():
        return True

    try:
        # Imported here and nowhere else. This is the line spec 14 §11's rule
        # about domain code exists to protect, and it is also what keeps
        # `langfuse` an optional install rather than an import-time hard edge.
        import langfuse
    except ImportError:
        logger.warning("tracing: the `langfuse` package is not installed; tracing disabled")
        _state = _State(enabled=False, host=settings.langfuse_host, project_id=None)
        return False

    try:
        client = langfuse.Langfuse(
            public_key=public.get_secret_value(),
            secret_key=secret.get_secret_value(),
            host=settings.langfuse_host,
            should_export_span=_export_filter(),
        )
    except Exception:  # noqa: BLE001 - a bad config must not stop the app
        logger.warning("tracing: could not construct the Langfuse client", exc_info=True)
        _state = _State(enabled=False, host=settings.langfuse_host, project_id=None)
        return False

    # Checked rather than assumed, because the alternative is discovering a
    # typo'd key as a silently empty dashboard a week later (spec 14 §13).
    try:
        if not client.auth_check():
            logger.warning("tracing: Langfuse rejected the keys; tracing disabled")
            _discard(client)
            _state = _State(enabled=False, host=settings.langfuse_host, project_id=None)
            return False
    except Exception:  # noqa: BLE001 - unreachable is not invalid, but both disable
        logger.warning("tracing: could not reach Langfuse to check the keys", exc_info=True)
        _discard(client)
        _state = _State(enabled=False, host=settings.langfuse_host, project_id=None)
        return False

    _instrument_pydantic_ai()

    _client = client
    _state = _State(
        enabled=True,
        host=settings.langfuse_host,
        project_id=settings.langfuse_project_id,
    )
    logger.info("Tracing enabled (Langfuse at %s)", settings.langfuse_host)
    return True


def _instrument_pydantic_ai() -> None:
    """Make Pydantic AI emit its own spans into the trace we just opened.

    `InstrumentationSettings` with no `tracer_provider` resolves to the *global*
    provider, and constructing a `Langfuse` client installs itself as exactly
    that -- verified by introspection against langfuse 5.0.2 and pydantic-ai
    2.56.0, not from documentation. So the model spans nest under `chat_turn`
    with no reach into a private attribute of the client.

    `include_binary_content=False` because nothing here sends images and a
    trace is not the place to find out otherwise.
    """
    try:
        from pydantic_ai import Agent
        from pydantic_ai.agent import InstrumentationSettings

        Agent.instrument_all(InstrumentationSettings(include_binary_content=False))
    except Exception:  # noqa: BLE001 - tracing must not break the agent
        logger.warning("tracing: could not instrument Pydantic AI", exc_info=True)


def shutdown_tracing() -> None:
    """Flush pending spans and close the client. Never raises.

    Langfuse batches, so without this a short-lived process -- `make api`
    stopped with Ctrl-C, a one-off script -- loses the trace it just made. This
    is the first thing after the `yield` in `api/main.py`'s lifespan.
    """
    global _client
    client = _client
    if client is None:
        return
    try:
        client.flush()
        client.shutdown()
    except Exception:  # noqa: BLE001 - a failed flush must not fail a shutdown
        logger.warning("tracing: could not flush Langfuse on shutdown", exc_info=True)
    finally:
        _client = None


def reset_tracing() -> None:
    """Forget everything `init_tracing` decided. For tests.

    `_state` is derived from settings, so it has to be dropped whenever the
    settings cache is -- otherwise one test that enables tracing leaves it on
    for every test after it, and the no-op assertions become order-dependent.
    That is the failure that reads as flakiness rather than as a missing reset,
    which is why this sits beside `llm.reset_models()` in the autouse fixture.

    The Pydantic AI instrumentation is global too, so it is turned off here for
    the same reason.
    """
    global _state, _client
    shutdown_tracing()
    _state = None
    _client = None
    try:
        from pydantic_ai import Agent

        Agent.instrument_all(False)
    except Exception:  # noqa: BLE001 - a test helper must not raise
        logger.warning("tracing: could not clear Pydantic AI instrumentation", exc_info=True)
