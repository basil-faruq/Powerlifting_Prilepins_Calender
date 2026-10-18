"""The tool-call recorder: timing, redaction, errors, and thread visibility.

Pure -- no database, no model. The one test that matters most is
`test_a_recorder_is_visible_from_an_anyio_worker_thread`: the whole design rests
on a contextvar surviving the hop into the thread Pydantic AI runs sync tools
in, and that is a property of anyio, not of this code. If it ever stops being
true, `tool_calls` quietly loses every call the `answer` route makes.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal

import anyio
import pytest

from domains.strength.agent import recorder
from domains.strength.agent.schemas import ToolCall


@recorder.records("example", summarise=lambda out: {"value": out})
def example(session: object, athlete: object, *, query: str, k: int = 5) -> int:
    return k * 2


@recorder.records("raiser")
def raiser(session: object) -> None:
    raise ValueError("the plan broke")


def test_a_call_is_recorded_with_its_input_and_summary():
    with recorder.recording() as rec:
        assert example("a session", "an athlete", query="knee", k=3) == 6

    (call,) = rec.calls
    assert call.name == "example"
    assert call.input == {"query": "knee", "k": 3}
    assert call.output_summary == {"value": 6}
    assert call.error is None


def test_the_machinery_arguments_are_never_recorded():
    """Spec 14 §10: no full DB rows, no secrets. Cheapest way is to omit them.

    `session` and `athlete` are passed positionally here on purpose -- the
    arguments are bound through the signature, so hiding them cannot depend on
    how the caller chose to pass them.
    """
    with recorder.recording() as rec:
        example("a session", "an athlete", query="knee")

    (call,) = rec.calls
    assert "session" not in call.input
    assert "athlete" not in call.input
    assert set(call.input) == {"query", "k"}


def test_defaults_are_recorded_too():
    """The entry should read the same whether the caller relied on a default."""
    with recorder.recording() as rec:
        example(None, None, query="knee")

    (call,) = rec.calls
    assert call.input["k"] == 5


def test_duration_is_recorded():
    with recorder.recording() as rec:
        example(None, None, query="knee")

    (call,) = rec.calls
    assert call.duration_ms >= 0


def test_a_raising_tool_is_recorded_and_the_error_still_propagates():
    """A turn that failed halfway is the one most worth reading afterwards."""
    with recorder.recording() as rec, pytest.raises(ValueError, match="the plan broke"):
        raiser(None)

    (call,) = rec.calls
    assert call.name == "raiser"
    assert call.error == "ValueError: the plan broke"
    assert call.output_summary == {}


def test_calls_are_recorded_in_order():
    with recorder.recording() as rec:
        example(None, None, query="first")
        example(None, None, query="second")

    assert [call.input["query"] for call in rec.calls] == ["first", "second"]


def test_a_tool_runs_fine_with_no_recorder_active():
    """A unit test calling a tool directly must not need a turn around it."""
    assert recorder.current() is None
    assert example(None, None, query="knee", k=4) == 8
    assert recorder.current() is None


def test_recording_nests_without_leaking():
    with recorder.recording() as outer:
        example(None, None, query="outer")
        with recorder.recording() as inner:
            example(None, None, query="inner")
        example(None, None, query="outer again")

    assert [call.input["query"] for call in inner.calls] == ["inner"]
    assert [call.input["query"] for call in outer.calls] == ["outer", "outer again"]


def test_dump_is_json_ready():
    """It goes into a JSONB column, so it has to survive `model_dump(mode="json")`."""

    @recorder.records(
        "rich",
        summarise=lambda out: {"id": out},
        stats=lambda _out: {"status": "OPTIMAL", "wall_time_s": 0.02},
        validation=lambda _out: {"ok": True, "warnings": 2},
    )
    def rich(*, when: dt.date, weight: Decimal) -> uuid.UUID:
        return uuid.UUID("00000000-0000-0000-0000-00000000000a")

    with recorder.recording() as rec:
        rich(when=dt.date(2026, 3, 2), weight=Decimal("72.5"))

    (dumped,) = rec.dump()
    assert dumped["input"] == {"when": "2026-03-02", "weight": "72.5"}
    assert dumped["solver_stats"] == {"status": "OPTIMAL", "wall_time_s": 0.02}
    assert dumped["validation"] == {"ok": True, "warnings": 2}
    assert dumped["output_summary"]["id"] == "00000000-0000-0000-0000-00000000000a"
    import json

    json.dumps(dumped)


def test_a_long_input_value_is_truncated():
    with recorder.recording() as rec:
        example(None, None, query="x" * 5000)

    (call,) = rec.calls
    assert len(call.input["query"]) == recorder.MAX_ATTR_CHARS + 3
    assert call.input["query"].endswith("...")


def test_a_broken_summary_is_logged_and_the_tool_still_returns(caplog):
    """A summary exists for the trace. It must never be able to fail the work."""

    @recorder.records("fragile", summarise=lambda _out: 1 / 0)
    def fragile() -> str:
        return "done"

    with (
        caplog.at_level(logging.WARNING, logger="domains.strength.agent.recorder"),
        recorder.recording() as rec,
    ):
        assert fragile() == "done"

    assert "summarise failed" in caplog.text
    (call,) = rec.calls
    assert call.output_summary == {}


def test_a_recorder_is_visible_from_an_anyio_worker_thread():
    """The claim the whole design rests on.

    Pydantic AI runs a sync `@agent.tool_plain` through
    `anyio.to_thread.run_sync`, which copies the context at submission. If that
    ever stopped being true, every tool call on the `answer` route would vanish
    from `decision.tool_calls` with nothing failing to say so.
    """
    seen: dict[str, object] = {}

    async def main() -> None:
        with recorder.recording() as rec:

            def in_worker() -> None:
                import threading

                seen["thread"] = threading.current_thread().name
                example(None, None, query="from the worker")

            await anyio.to_thread.run_sync(in_worker)
            seen["calls"] = [call.input["query"] for call in rec.calls]

    anyio.run(main)

    assert seen["calls"] == ["from the worker"]
    assert seen["thread"] != "MainThread"


def test_the_decorator_preserves_the_wrapped_signature():
    """`_describe` binds through `inspect.signature`, so `wraps` is load-bearing."""
    assert example.__name__ == "example"
    assert "query" in example.__doc__ if example.__doc__ else True


def test_tool_call_rejects_a_negative_duration():
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        ToolCall(name="x", duration_ms=-1)
