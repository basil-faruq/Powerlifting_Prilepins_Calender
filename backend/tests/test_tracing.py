"""Tracing, mostly proving that it does nothing.

Spec 14 §11 requires the suite to run with tracing disabled and to assert that
`traced` is a no-op, which makes "the app works identically with tracing off"
a tested property rather than a hope. Every test here runs disabled except the
handful that monkeypatch `enabled()` to exercise the failure paths.

No test constructs a real Langfuse client or opens a socket.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import sys
import types
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from opentelemetry import trace

from planner_core import tracing
from planner_core.config import Settings

BACKEND = Path(tracing.__file__).resolve().parent.parent


def _fake_langfuse(client_cls) -> types.ModuleType:
    """A stand-in for the `langfuse` module whose client is `client_cls`.

    A module rather than a class with a `Langfuse` attribute, because
    `init_tracing` does `import langfuse` and then `langfuse.Langfuse(...)`;
    anything else would pass for the wrong reason.
    """
    module = types.ModuleType("langfuse")
    module.Langfuse = lambda **kwargs: client_cls()
    return module


@pytest.fixture
def no_keys() -> Settings:
    return Settings(langfuse_public_key=None, langfuse_secret_key=None)


# --- The no-op (spec 14 §11, §12) -----------------------------------------


def test_init_tracing_is_false_without_keys(no_keys):
    assert tracing.init_tracing(no_keys) is False
    assert tracing.enabled() is False


def test_blank_keys_are_the_same_as_absent(caplog):
    """`.env.example` ships the keys empty, so blank must disable, not crash.

    `Settings` normalises blank to `None` (pinned in `test_config.py`); this is
    the other half -- that `init_tracing` then takes the disabled path rather
    than handing an empty string to the SDK.
    """
    settings = Settings(langfuse_public_key="   ", langfuse_secret_key="")

    with caplog.at_level(logging.INFO, logger="planner_core.tracing"):
        assert tracing.init_tracing(settings) is False

    assert tracing.DISABLED_MESSAGE in caplog.text


def test_traced_is_a_noop_with_no_keys(no_keys):
    tracing.init_tracing(no_keys)

    with tracing.traced("chat_turn", athlete_id=uuid.uuid4(), message_length=12) as span:
        span.set("route", "plan")
        span.set_many(outcome="answered", count=3)
        assert span.trace_id is None

    assert tracing.current_trace_id() is None


def test_a_noop_span_nests(no_keys):
    """Nesting is what the instrumentation points do, so it has to be safe off."""
    tracing.init_tracing(no_keys)

    with tracing.traced("chat_turn") as outer, tracing.traced("tool.get_block") as inner:
        inner.set("duration_ms", 4)
        outer.set("route", "answer")


def test_traced_is_a_noop_before_init_is_ever_called():
    """A unit test that calls a tool directly never runs the lifespan."""
    tracing.reset_tracing()

    assert tracing.enabled() is False
    with tracing.traced("tool.plan_block") as span:
        span.set("k", 1)
        assert span.trace_id is None


def test_the_body_of_a_traced_block_still_raises(no_keys):
    """The one thing `traced` must never swallow.

    Tracing's own failures are swallowed; the caller's are not. Without this
    test the `except BaseException` in `traced` could grow a `return` and no
    other test would notice -- every tool error would vanish into a trace.
    """
    tracing.init_tracing(no_keys)

    with pytest.raises(ValueError, match="the plan broke"), tracing.traced("tool.plan_block"):
        raise ValueError("the plan broke")


def test_trace_url_is_none_when_disabled(no_keys):
    tracing.init_tracing(no_keys)

    assert tracing.trace_url("49a4891be21e516c9bc541c41760c6b0") is None
    assert tracing.trace_url(None) is None


# --- Failure paths --------------------------------------------------------


def test_traced_never_breaks_a_request_when_the_tracer_raises(monkeypatch, caplog):
    """Spec 14 §13: Langfuse unreachable is a warning, not a failed request."""
    monkeypatch.setattr(tracing, "_state", tracing._State(True, "http://h", "p1"))

    def exploding(_name: str):
        raise RuntimeError("no exporter")

    monkeypatch.setattr(tracing.trace, "get_tracer", exploding)

    with (
        caplog.at_level(logging.WARNING, logger="planner_core.tracing"),
        tracing.traced("chat_turn") as span,
    ):
        span.set("route", "plan")
        assert span.trace_id is None

    assert "could not open span" in caplog.text


def test_a_bad_attribute_is_logged_and_not_raised(monkeypatch, caplog):
    class Exploding:
        def set_attribute(self, key, value):
            raise RuntimeError("bad attribute")

        def get_span_context(self):
            raise RuntimeError("no context")

    span = tracing._OtelSpan(Exploding())

    with caplog.at_level(logging.WARNING, logger="planner_core.tracing"):
        span.set("route", "plan")
        assert span.trace_id is None

    assert "could not set attribute" in caplog.text


def test_init_tracing_survives_a_missing_package(monkeypatch, caplog):
    """`langfuse` is imported inside the function precisely so this is possible."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "langfuse":
            raise ImportError("no langfuse")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    settings = Settings(langfuse_public_key="pk-lf-x", langfuse_secret_key="sk-lf-x")

    with caplog.at_level(logging.WARNING, logger="planner_core.tracing"):
        assert tracing.init_tracing(settings) is False

    assert "not installed" in caplog.text
    assert tracing.enabled() is False


def test_invalid_keys_disable_tracing(monkeypatch, caplog):
    """Spec 14 §13. A typo'd key must say so at startup.

    The alternative is finding out a week later, from an empty dashboard.
    """

    class Rejecting:
        def auth_check(self):
            return False

    monkeypatch.setitem(sys.modules, "langfuse", _fake_langfuse(Rejecting))
    settings = Settings(langfuse_public_key="pk-lf-x", langfuse_secret_key="sk-lf-x")

    with caplog.at_level(logging.WARNING, logger="planner_core.tracing"):
        assert tracing.init_tracing(settings) is False

    assert "rejected the keys" in caplog.text


def test_an_unreachable_langfuse_disables_tracing(monkeypatch, caplog):
    class Unreachable:
        def auth_check(self):
            raise OSError("connection refused")

    monkeypatch.setitem(sys.modules, "langfuse", _fake_langfuse(Unreachable))
    settings = Settings(langfuse_public_key="pk-lf-x", langfuse_secret_key="sk-lf-x")

    with caplog.at_level(logging.WARNING, logger="planner_core.tracing"):
        assert tracing.init_tracing(settings) is False

    assert "could not reach Langfuse" in caplog.text


def test_shutdown_tracing_is_safe_with_no_client():
    tracing.reset_tracing()
    tracing.shutdown_tracing()


def test_shutdown_tracing_swallows_a_failing_flush(monkeypatch, caplog):
    class Failing:
        def flush(self):
            raise OSError("gone")

        def shutdown(self):
            raise OSError("gone")

    monkeypatch.setattr(tracing, "_client", Failing())

    with caplog.at_level(logging.WARNING, logger="planner_core.tracing"):
        tracing.shutdown_tracing()

    assert "could not flush" in caplog.text
    assert tracing._client is None


# --- Attribute coercion ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("72.50"), "72.50"),
        (dt.date(2026, 3, 2), "2026-03-02"),
        (uuid.UUID("00000000-0000-0000-0000-000000000001"), "00000000-0000-0000-0000-000000000001"),
        (None, ""),
        (True, True),
        (3, 3),
        ("plan", "plan"),
    ],
)
def test_attributes_are_coerced_to_what_otel_accepts(value, expected):
    """OTel logs a warning per call for a non-primitive, so coercion is here.

    `Decimal` becomes a *string* rather than a float on purpose: a weight that
    reaches a trace as 72.50000000000001 reads as the number we computed.
    """
    assert tracing._attr(value) == expected


def test_a_sequence_attribute_is_flattened():
    assert tracing._attr(["prilepin_reps", "volume_trend"]) == ["prilepin_reps", "volume_trend"]
    assert tracing._attr((1, 2)) == ["1", "2"]


def test_trace_url_shape(monkeypatch):
    state = tracing._State(True, "https://cloud.langfuse.com/", "p7")
    monkeypatch.setattr(tracing, "_state", state)

    assert (
        tracing.trace_url("49a4891be21e516c9bc541c41760c6b0")
        == "https://cloud.langfuse.com/project/p7/traces/49a4891be21e516c9bc541c41760c6b0"
    )


def test_trace_url_is_none_without_a_project_id(monkeypatch):
    """Spec 14 §5D: `None` when disabled *or ids missing*.

    Tracing can be working while `LANGFUSE_PROJECT_ID` is unset -- the keys are
    what the SDK needs, the project id is only what the URL needs.
    """
    monkeypatch.setattr(tracing, "_state", tracing._State(True, "https://h", None))

    assert tracing.trace_url("49a4891be21e516c9bc541c41760c6b0") is None


def test_a_real_span_reports_its_trace_id(monkeypatch):
    """`decision.langfuse_trace_id` is written from this, so it has to work.

    Exercised against OTel's own in-memory machinery rather than Langfuse: the
    id is the W3C one, which is exactly why no SDK call is needed to read it.
    """
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(tracing, "_state", tracing._State(True, "https://h", "p1"))
    provider = TracerProvider()
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: provider.get_tracer(name))

    with tracing.traced("chat_turn") as span:
        trace_id = span.trace_id
        assert trace_id is not None
        assert len(trace_id) == 32
        assert trace_id == trace_id.lower()
        with tracing.traced("tool.get_block") as inner:
            assert inner.trace_id == trace_id


def test_a_traceparent_reattaches_a_span_to_a_trace_in_another_context(monkeypatch):
    """Spec 15's `approval` span has to land in the reschedule's own trace.

    Contextvars do not cross a Temporal activity: the approval runs in a
    different thread, often a different process, minutes or days after the
    span it belongs under. So the parent travels as a `traceparent` string and
    is reattached here. Without this the UI would show two unrelated traces
    for one decision -- the options, and the answer.
    """
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(tracing, "_state", tracing._State(True, "https://h", "p1"))
    provider = TracerProvider()
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: provider.get_tracer(name))

    with tracing.traced("workflow.missed_session") as root:
        carried = tracing.current_traceparent()
        root_trace_id = root.trace_id

    assert carried is not None
    assert root_trace_id is not None
    # Back at the top level, nothing is current any more -- exactly the
    # situation an activity starts in.
    assert tracing.current_trace_id() is None

    with tracing.traced("approval", parent=carried) as child:
        assert child.trace_id == root_trace_id


def test_a_span_with_no_parent_starts_its_own_trace(monkeypatch):
    """The fallback when a proposal reaches the workflow from somewhere it
    did not see -- an approval of a reschedule proposed in chat."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(tracing, "_state", tracing._State(True, "https://h", "p1"))
    provider = TracerProvider()
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: provider.get_tracer(name))

    with tracing.traced("workflow.missed_session") as first:
        first_id = first.trace_id
    with tracing.traced("approval") as second:
        second_id = second.trace_id

    assert first_id != second_id


def test_a_nonsense_traceparent_does_not_break_the_body(monkeypatch):
    """Tracing's failures are swallowed; the work goes on (spec 14 §11)."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(tracing, "_state", tracing._State(True, "https://h", "p1"))
    provider = TracerProvider()
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: provider.get_tracer(name))

    ran = False
    with tracing.traced("approval", parent="not-a-traceparent"):
        ran = True

    assert ran


def test_current_traceparent_is_none_when_disabled(no_keys):
    tracing.init_tracing(no_keys)

    with tracing.traced("chat_turn"):
        assert tracing.current_traceparent() is None


# --- The boundary (spec 14 §11) -------------------------------------------

# `langfuse` is the vendor; `opentelemetry` is the vendor-neutral API but still
# an observability concern that spec 14 §11 keeps out of domain code. Both are
# `planner_core/tracing.py`'s alone, so that swapping the backend is one file.
TRACING_PACKAGES = ("langfuse", "opentelemetry")


def _tracing_imports(root: Path, skip: tuple[Path, ...] = ()) -> list[str]:
    offenders: list[str] = []
    for package in ("domains", "api", "scripts"):
        for path in sorted((root / package).rglob("*.py")):
            if path in skip:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                offenders += [
                    f"{path.relative_to(root).as_posix()}: {name}"
                    for name in names
                    if name.split(".")[0] in TRACING_PACKAGES
                ]
    return offenders


def test_no_domain_or_api_module_imports_the_tracing_backend():
    """Spec 14 §11 was prose, which is how prose rules get broken.

    Mirrors `test_llm.py`'s provider-SDK walk. Domain code gets spans through
    `planner_core.tracing.traced` and the agent's `@records` decorator, so a
    hit here means the backend has leaked out of the one module that owns it.
    """
    assert _tracing_imports(BACKEND) == []


def test_that_check_would_actually_catch_one():
    """Guards the guard, as its counterpart in `test_llm.py` does.

    The walk covers `domains`, `api` and `scripts` and never `planner_core`,
    so the clean result above would also be what a matcher that detects
    nothing returns. Pointing the same matcher at `planner_core`, which really
    does import OpenTelemetry, is what proves it works.
    """
    hits = _imports_under(BACKEND / "planner_core")

    assert any(entry.startswith("planner_core/tracing.py") for entry in hits)


def _imports_under(package: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            found += [
                f"{path.relative_to(BACKEND).as_posix()}: {name}"
                for name in names
                if name.split(".")[0] in TRACING_PACKAGES
            ]
    return found


def test_the_tracing_module_only_imports_the_otel_api_at_module_scope():
    """`import langfuse` must stay inside `init_tracing`.

    That is what keeps the SDK out of every import of `planner_core`, and what
    makes the package effectively optional. A module-scope import would also
    set the global tracer provider at import time and quietly turn the
    "disabled" path into real-spans-with-no-exporter.
    """
    tree = ast.parse((BACKEND / "planner_core" / "tracing.py").read_text(encoding="utf-8"))
    module_scope: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_scope += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_scope.append(node.module or "")

    assert "langfuse" not in module_scope
    assert any(name.startswith("opentelemetry") for name in module_scope)


def test_the_global_tracer_provider_is_untouched_by_import():
    """Importing `planner_core.tracing` must not install a provider.

    Constructing a Langfuse client does install itself as the global provider
    (verified against langfuse 5.0.2), which is how Pydantic AI's spans nest
    into ours -- but that has to happen in `init_tracing`, not at import.
    """
    assert type(trace.get_tracer_provider()).__name__ == "ProxyTracerProvider"


# --- What Langfuse exports ----------------------------------------------


class _Scope:
    def __init__(self, name):
        self.name = name


class _Span:
    def __init__(self, scope_name):
        self.instrumentation_scope = _Scope(scope_name) if scope_name else None
        self.attributes = {}


def test_our_own_spans_are_exported():
    """Langfuse 5's default filter exports only its own spans, `gen_ai.*` spans
    and a fixed list of scopes. Ours is none of those, so with the default
    every `chat_turn`, `tool.*`, `validator` and `solver.*` span was silently
    dropped -- the IDs and `trace_url` stayed well-formed, so nothing said so
    (found by review, from the installed source)."""
    export = tracing._export_filter()

    assert export(_Span(tracing.TRACER_NAME)) is True


def test_a_foreign_scope_is_left_to_langfuses_default():
    export = tracing._export_filter()

    assert export(_Span("some.unrelated.library")) is False


def test_a_span_with_no_scope_does_not_crash():
    assert tracing._export_filter()(_Span(None)) is False


def test_the_filter_is_handed_to_the_client(monkeypatch):
    seen = {}

    class Accepting:
        def auth_check(self):
            return True

        def flush(self):
            pass

        def shutdown(self):
            pass

    def make(**kwargs):
        seen.update(kwargs)
        return Accepting()

    module = types.ModuleType("langfuse")
    module.Langfuse = make
    monkeypatch.setitem(sys.modules, "langfuse", module)
    settings = Settings(langfuse_public_key="pk-lf-x", langfuse_secret_key="sk-lf-x")

    assert tracing.init_tracing(settings) is True

    assert callable(seen["should_export_span"])
    assert seen["should_export_span"](_Span(tracing.TRACER_NAME)) is True


def test_a_rejected_client_is_shut_down(monkeypatch):
    """It still owns a background thread and has installed itself as the global
    provider, so discarding it without shutdown leaks both."""
    events = []

    class Rejecting:
        def auth_check(self):
            return False

        def shutdown(self):
            events.append("shutdown")

    monkeypatch.setitem(sys.modules, "langfuse", _fake_langfuse(Rejecting))
    settings = Settings(langfuse_public_key="pk-lf-x", langfuse_secret_key="sk-lf-x")

    assert tracing.init_tracing(settings) is False
    assert events == ["shutdown"]
