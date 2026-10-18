"""A dead chat model has to become `LlmUnavailable`, which is the 503.

`LlmUnavailable` was raised only by the embedder and the startup probe. A chat
model that is down raises Pydantic AI's own `ModelAPIError`/`ModelHTTPError`
from inside `agent.run_sync`, and nothing translated those -- so a stopped
Ollama looked like a bad sentence: the router retried, gave up and answered
200 "I couldn't understand that". The tests missed it because their fake raised
`OllamaUnreachable` directly, which no real model call does (found by review).

Pure: no database, no real model.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from planner_core import llm
from planner_core.errors import LlmUnavailable, OllamaUnreachable


def test_a_connection_failure_is_ollama_unreachable():
    out = llm.unavailable_from(ModelAPIError("qwen2.5:14b", "connection refused"))

    assert isinstance(out, OllamaUnreachable)
    assert "ollama serve" in str(out)


@pytest.mark.parametrize("status", [500, 502, 503, 404, 408, 429])
def test_statuses_that_mean_it_is_not_serving_are_unavailable(status):
    out = llm.unavailable_from(ModelHTTPError(status, "qwen2.5:14b"))

    assert isinstance(out, LlmUnavailable)


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_a_request_that_was_wrong_is_not_an_outage(status):
    """A 400 is our request being wrong, which is a bug and should surface as
    one rather than being retried by a user into a 503 that fixes nothing."""
    assert llm.unavailable_from(ModelHTTPError(status, "qwen2.5:14b")) is None


def test_a_transport_error_is_unavailable():
    assert isinstance(llm.unavailable_from(httpx.ConnectError("refused")), OllamaUnreachable)


def test_it_looks_through_the_cause_chain():
    """Pydantic AI wraps what the SDK raised, so the outage is rarely top-level."""
    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert isinstance(llm.unavailable_from(outer), OllamaUnreachable)


def test_an_existing_llm_unavailable_passes_straight_through():
    original = OllamaUnreachable("http://h", "x")

    assert llm.unavailable_from(original) is original


@pytest.mark.parametrize("exc", [ValueError("bug"), KeyError("k"), RuntimeError("plain")])
def test_an_ordinary_bug_is_not_an_outage(exc):
    assert llm.unavailable_from(exc) is None


def test_a_cycle_in_the_cause_chain_terminates():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a

    assert llm.unavailable_from(a) is None


def test_anthropic_mode_gets_a_provider_neutral_message(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")

    out = llm.unavailable_from(ModelAPIError("claude", "timeout"))

    assert isinstance(out, LlmUnavailable)
    assert not isinstance(out, OllamaUnreachable)
    assert "ollama" not in str(out).lower()


def test_the_router_raises_on_an_outage_instead_of_clarifying():
    """The user-visible symptom: Ollama down must not read as a bad sentence."""
    import datetime as dt

    from domains.strength import enums
    from domains.strength.agent import router
    from tests.fakes import RefusingModel

    ctx = router.RouterContext(
        today=dt.date(2026, 3, 2),
        weekdays={0: "Monday"},
        lifts=tuple(enums.LIFT_CODES),
        has_one_rms=True,
        has_block=True,
    )

    with pytest.raises(OllamaUnreachable):
        router.select_route(RefusingModel(), "plan a block", ctx=ctx)
