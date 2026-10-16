"""The provider switch, the embedder's wire format, and the startup check.

Nothing here reaches a network: the chat models are only *built* (which is all
`get_model` does), and the embedder is driven through `httpx.MockTransport`, so
these assertions are about the request this project sends rather than about
Ollama's behaviour. `conftest` also pins `ALLOW_MODEL_REQUESTS = False`, so a
future test that accidentally runs an agent fails loudly instead of quietly
calling the developer's local model.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.ollama import OllamaModel

from planner_core import llm
from planner_core.config import get_settings
from planner_core.errors import LlmUnavailable, ModelNotPulled, OllamaUnreachable

BASE_URL = "http://localhost:11434"
EMBED_URL = f"{BASE_URL}/api/embed"
TAGS_URL = f"{BASE_URL}/api/tags"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A developer's own `.env` must not decide what these tests assert.

    The model and settings caches are dropped by the autouse fixture in
    `conftest.py`, which does it for every test rather than only this file.
    """
    for name in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OLLAMA_BASE_URL", "EMBED_DIM"):
        monkeypatch.delenv(name, raising=False)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)


def _embedder(handler, *, dim: int = 768, **kwargs) -> llm.OllamaEmbedder:
    return llm.OllamaEmbedder(
        BASE_URL,
        "nomic-embed-text",
        dim,
        client=_mock_client(handler),
        backoff_s=0.0,
        **kwargs,
    )


def _vectors(count: int, dim: int = 768) -> dict:
    return {"embeddings": [[0.1] * dim for _ in range(count)]}


# --- get_model ------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [("agent", "qwen2.5:14b"), ("router", "qwen2.5:14b")],
)
def test_ollama_is_the_default_provider_for_both_roles(role, expected):
    model = llm.get_model(role)
    assert isinstance(model, OllamaModel)
    assert model.model_name == expected


def test_anthropic_is_selected_by_the_env_var_alone(monkeypatch):
    """The whole point of spec 11: one variable, no other file changed."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()

    agent = llm.get_model("agent")
    router = llm.get_model("router")

    assert isinstance(agent, AnthropicModel)
    assert isinstance(router, AnthropicModel)
    assert agent.model_name == "claude-sonnet-5-5"
    # The router drops to the smaller model: picking from a filtered list is
    # the one job a small model does reliably.
    assert router.model_name == "claude-haiku-5-5"


def test_the_model_is_built_once_per_role():
    assert llm.get_model("agent") is llm.get_model("agent")
    assert llm.get_model("agent") is not llm.get_model("router")


def test_the_api_key_is_masked_in_settings_and_absent_from_logs(monkeypatch, caplog):
    """The paths a key actually escapes by: a settings dump and a log record.

    Deliberately *not* asserted against `repr(model)`: Pydantic AI's is a bare
    `AnthropicModel()`, so such an assertion passes no matter how the key is
    handled, while the key is in fact reachable at `model.client.api_key`. That
    is a property of the library, not of this module -- what this module owes
    is that it never puts the key anywhere itself.
    """
    secret = "sk-ant-secret-value"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    get_settings.cache_clear()

    with caplog.at_level(logging.DEBUG):
        llm.get_model("agent")

    assert secret not in str(get_settings())
    assert secret not in repr(get_settings())
    assert secret not in caplog.text


def test_a_failure_while_building_the_anthropic_model_does_not_leak_the_key(monkeypatch):
    """The key is passed straight through and never bound to a local, so a
    traceback raised from inside the provider cannot carry it in a frame."""
    secret = "sk-ant-secret-value"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    get_settings.cache_clear()

    def explode(*args, **kwargs):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr(llm, "AnthropicProvider", explode)

    with pytest.raises(RuntimeError) as excinfo:
        llm.get_model("agent")

    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)


def test_the_ollama_provider_is_pointed_at_the_openai_compatible_surface(monkeypatch):
    """`/v1` is where Ollama's OpenAI-compatible API lives; `/api` is its own."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/")
    get_settings.cache_clear()

    model = llm.get_model("agent")

    assert str(model.client.base_url).rstrip("/") == "http://gpu-box:11434/v1"


# --- OllamaEmbedder: the wire format --------------------------------------


def test_documents_and_queries_get_their_task_prefixes():
    """nomic is task-conditioned: the same text embeds differently per prefix."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json=_vectors(len(payload["input"])))

    embedder = _embedder(handler)
    embedder.embed_documents(["knee felt tight"])
    embedder.embed_query("how has my knee been?")

    assert seen[0]["input"] == ["search_document: knee felt tight"]
    assert seen[1]["input"] == ["search_query: how has my knee been?"]


def test_the_context_window_is_sent_explicitly():
    """Ollama defaults nomic to 2k and truncates silently; a long note would halve."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_vectors(1))

    _embedder(handler).embed_query("anything")

    assert seen[0]["options"] == {"num_ctx": 8192}
    assert seen[0]["model"] == "nomic-embed-text"


def test_the_request_goes_to_the_embed_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_vectors(1))

    _embedder(handler).embed_query("anything")

    assert seen == [EMBED_URL]


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_vectors(1))

    llm.OllamaEmbedder(
        f"{BASE_URL}/", "nomic-embed-text", 768, client=_mock_client(handler)
    ).embed_query("anything")

    assert seen == [EMBED_URL]


def test_long_input_is_split_into_batches_of_32():
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sizes.append(len(payload["input"]))
        return httpx.Response(200, json=_vectors(len(payload["input"])))

    vectors = _embedder(handler).embed_documents([f"note {i}" for i in range(70)])

    assert sizes == [32, 32, 6]
    # Order is the contract: `index_log_note` pairs these with their chunks by
    # position, so a reordering would attach the wrong vector to the wrong text.
    assert len(vectors) == 70


def test_embedding_nothing_asks_ollama_nothing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    assert _embedder(handler).embed_documents([]) == []


# --- OllamaEmbedder: failure modes ----------------------------------------


def test_a_wrong_width_is_refused_before_it_can_reach_the_database():
    """The column is sized from EMBED_DIM; a mismatched vector poisons search."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1] * 1024]})

    with pytest.raises(ValueError, match="nomic-embed-text") as excinfo:
        _embedder(handler).embed_query("anything")

    message = str(excinfo.value)
    assert "1024" in message
    assert "768" in message


def test_a_short_response_is_refused():
    """Two inputs, one vector: pairing by position would silently mislabel."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_vectors(1))

    with pytest.raises(ValueError, match="1 embeddings for 2 inputs"):
        _embedder(handler).embed_documents(["one", "two"])


@pytest.mark.parametrize(
    ("body", "label"),
    [
        ({"embeddings": [[None] * 768]}, "nulls"),
        ({"embeddings": ["a" * 768]}, "a string of the right length"),
        ({"embeddings": [5]}, "a bare number"),
        ({"embeddings": {"a": 1}}, "an object instead of a list"),
        ({"embeddings": [["x"] * 768]}, "strings instead of floats"),
        ({}, "no embeddings key"),
    ],
)
def test_a_body_that_is_not_an_embedding_response_is_refused(body, label):
    """`len()` was the only check, and these all have the right length or none.

    The two that matter most are the nulls and the 768-character string: both
    would have been returned to the caller as "vectors" and written to a
    `VECTOR(768)` column.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(ValueError):
        _embedder(handler).embed_query("anything")


def test_a_non_json_body_on_a_200_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    with pytest.raises(ValueError, match="not an embedding response"):
        _embedder(handler).embed_query("anything")


def test_a_redirect_is_not_mistaken_for_success():
    """httpx does not follow redirects by default, so a 3xx body is not JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/elsewhere"}, text="moved")

    with pytest.raises(ValueError, match="302"):
        _embedder(handler).embed_query("anything")


def test_a_bad_request_is_reported_as_one():
    """A 4xx that is not 404 means our request was wrong, not Ollama's absence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid options"})

    with pytest.raises(ValueError, match="invalid options"):
        _embedder(handler).embed_query("anything")


def test_a_negative_retry_count_is_refused_at_construction():
    """Otherwise the attempt loop never runs and every call reports
    "not reachable" without having contacted Ollama once."""
    with pytest.raises(ValueError, match="retries must be >= 0"):
        llm.OllamaEmbedder(BASE_URL, "nomic-embed-text", 768, retries=-1)


def test_a_nonsense_dimension_is_refused_at_construction():
    with pytest.raises(ValueError, match="dim must be >= 1"):
        llm.OllamaEmbedder(BASE_URL, "nomic-embed-text", 0)


def test_a_server_error_is_retried_and_can_then_succeed():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="loading model")
        return httpx.Response(200, json=_vectors(1))

    vector = _embedder(handler).embed_query("anything")

    assert len(attempts) == 3
    assert len(vector) == 768


def test_a_server_error_that_never_clears_says_what_ollama_answered():
    """Ollama *is* reachable here, so "start it with ollama serve" alone would
    send the reader somewhere that cannot help. The status survives."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "model requires more system memory"})

    with pytest.raises(OllamaUnreachable) as excinfo:
        _embedder(handler).embed_query("anything")

    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert "more system memory" in message


def test_a_refused_connection_names_the_command_that_fixes_it():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OllamaUnreachable) as excinfo:
        _embedder(handler).embed_query("anything")

    message = str(excinfo.value)
    assert BASE_URL in message
    assert "ollama serve" in message


def test_a_missing_model_names_the_pull_command():
    """Retrying will not download a model, so this one does not retry."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, json={"error": 'model "nomic-embed-text" not found'})

    with pytest.raises(ModelNotPulled) as excinfo:
        _embedder(handler).embed_query("anything")

    assert "ollama pull nomic-embed-text" in str(excinfo.value)
    assert attempts == [1]


def test_both_failures_share_one_base_so_callers_need_one_except():
    """Spec 12 catches this base to keep a training log when embedding fails."""
    assert issubclass(OllamaUnreachable, LlmUnavailable)
    assert issubclass(ModelNotPulled, LlmUnavailable)


# --- check_ollama_models --------------------------------------------------


def _tags(*names: str) -> dict:
    return {"models": [{"name": name, "model": name} for name in names]}


def test_an_untagged_name_matches_the_latest_tag():
    """Ollama always reports a tag; the setting is written without one."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TAGS_URL
        return httpx.Response(200, json=_tags("qwen2.5:14b", "nomic-embed-text:latest"))

    assert llm.check_ollama_models(_mock_client(handler)) == []


def test_the_models_that_are_not_pulled_are_named():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_tags("llama3:8b"))

    missing = llm.check_ollama_models(_mock_client(handler))

    assert missing == ["qwen2.5:14b", "nomic-embed-text"]


def test_the_chat_and_router_models_are_reported_once_when_they_are_the_same():
    """They default to the same model: one GPU, and a second would evict it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_tags("nomic-embed-text"))

    assert llm.check_ollama_models(_mock_client(handler)) == ["qwen2.5:14b"]


def test_being_unable_to_ask_ollama_at_all_does_raise():
    """Distinct from a missing model: nothing can be concluded, so nothing is."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OllamaUnreachable):
        llm.check_ollama_models(_mock_client(handler))


@pytest.mark.parametrize(
    "body",
    [
        {"models": ["qwen2.5:14b"]},
        {"models": "qwen2.5:14b"},
        {"models": [None]},
        {"models": [{}]},
        {},
        [],
    ],
)
def test_a_tags_body_of_the_wrong_shape_is_an_ollama_error_not_a_crash(body):
    """This used to raise `AttributeError` from outside the `try`, which escaped
    both `warn_missing_models` and `check_llm.py` -- so a diagnostic died with a
    traceback instead of reporting the problem it exists to report."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    try:
        missing = llm.check_ollama_models(_mock_client(handler))
    except LlmUnavailable:
        return  # Reported, which is all that is required.
    # Or treated as "nothing is installed", which is also honest.
    assert missing == ["qwen2.5:14b", "nomic-embed-text"]


def test_a_registry_qualified_name_is_not_read_as_already_tagged():
    """The colon in `registry:5000/model` is a port, not a tag. Treating it as
    one leaves the name untagged on both sides, so it never matches."""
    assert llm._normalise_tag("registry:5000/qwen2.5") == "registry:5000/qwen2.5:latest"
    assert llm._normalise_tag("registry:5000/qwen2.5:14b") == "registry:5000/qwen2.5:14b"
    assert llm._normalise_tag("nomic-embed-text") == "nomic-embed-text:latest"
    assert llm._normalise_tag("qwen2.5:14b") == "qwen2.5:14b"


def test_the_tags_probe_does_not_wait_as_long_as_an_embedding():
    """A hung Ollama would otherwise hold the awaited startup open for 30 s."""
    assert llm.TAGS_TIMEOUT_S < llm.EMBED_TIMEOUT_S


# --- warn_missing_models (what startup does) ------------------------------


def test_startup_warns_once_per_missing_model_with_its_pull_command(monkeypatch, caplog):
    monkeypatch.setattr(llm, "check_ollama_models", lambda: ["qwen2.5:14b"])

    with caplog.at_level(logging.WARNING, logger="planner_core.llm"):
        llm.warn_missing_models()

    assert "ollama pull qwen2.5:14b" in caplog.text


def test_startup_warns_but_does_not_raise_when_ollama_is_absent(monkeypatch, caplog):
    """Every deterministic route works with no model present, so it still starts."""

    def unreachable() -> list[str]:
        raise OllamaUnreachable(BASE_URL)

    monkeypatch.setattr(llm, "check_ollama_models", unreachable)

    with caplog.at_level(logging.WARNING, logger="planner_core.llm"):
        llm.warn_missing_models()

    assert "ollama serve" in caplog.text


def test_nothing_is_checked_when_the_provider_is_anthropic(monkeypatch, caplog):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()

    def fail() -> list[str]:  # pragma: no cover
        raise AssertionError("Ollama must not be probed in anthropic mode")

    monkeypatch.setattr(llm, "check_ollama_models", fail)

    with caplog.at_level(logging.WARNING, logger="planner_core.llm"):
        llm.warn_missing_models()

    assert caplog.text == ""


# --- get_embedder ---------------------------------------------------------


def test_embeddings_stay_local_even_in_anthropic_mode(monkeypatch):
    """ADR: the demo switch must not be able to invalidate stored vectors."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()

    embedder = llm.get_embedder()

    assert isinstance(embedder, llm.OllamaEmbedder)
    assert embedder.model == "nomic-embed-text"


def test_the_embedder_is_built_from_settings(monkeypatch):
    monkeypatch.setenv("EMBED_DIM", "1024")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434")
    get_settings.cache_clear()

    embedder = llm.get_embedder()

    assert isinstance(embedder, llm.OllamaEmbedder)
    assert embedder.dim == 1024
    assert embedder.base_url == "http://gpu-box:11434"


# --- The paths that only run in production --------------------------------
# Every test above injects a client, which skips the branch that builds one --
# so these two cover the only code that actually runs under `make api`.


def test_the_embedder_builds_its_own_client_when_none_is_injected(monkeypatch):
    built: list[float | None] = []
    mock = _mock_client(lambda request: httpx.Response(200, json=_vectors(1)))

    class Recording(httpx.Client):
        def __new__(cls, **kwargs):
            built.append(kwargs.get("timeout"))
            return mock

    monkeypatch.setattr(llm.httpx, "Client", Recording)

    vector = llm.OllamaEmbedder(BASE_URL, "nomic-embed-text", 768).embed_query("x")

    assert len(vector) == 768
    assert built == [llm.EMBED_TIMEOUT_S]


def test_the_tags_probe_builds_its_own_client_when_none_is_injected(monkeypatch):
    built: list[float | None] = []
    mock = _mock_client(lambda request: httpx.Response(200, json=_tags("qwen2.5:14b")))

    class Recording(httpx.Client):
        def __new__(cls, **kwargs):
            built.append(kwargs.get("timeout"))
            return mock

    monkeypatch.setattr(llm.httpx, "Client", Recording)

    assert llm.check_ollama_models() == ["nomic-embed-text"]
    assert built == [llm.TAGS_TIMEOUT_S]


def test_a_404_that_is_not_json_still_names_the_pull_command():
    """What a proxy's HTML error page does. `detail` exists for exactly this."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>404 Not Found</html>")

    with pytest.raises(ModelNotPulled) as excinfo:
        _embedder(handler).embed_query("anything")

    message = str(excinfo.value)
    assert "ollama pull nomic-embed-text" in message
    assert "404 Not Found" in message


# --- The boundary (CLAUDE.md guardrail; spec 11 §11) ----------------------

# The raw vendor SDKs. Nothing in this project should ever reach them.
PROVIDER_SDKS = ("openai", "anthropic")

# Pydantic AI's *provider-specific* bindings. These matter as much as the SDKs
# and are easier to import by accident: `pydantic_ai.models.anthropic` names a
# vendor just as `anthropic` does, so a module importing it has hard-coded the
# provider and `LLM_PROVIDER` no longer switches anything. Generic
# `pydantic_ai` is fine and necessary -- `get_model` returns its `Model` type,
# and the test doubles under `models.test` / `models.function` are how the rest
# of the suite avoids a real model.
PROVIDER_BINDINGS = (
    "pydantic_ai.providers",
    "pydantic_ai.models.anthropic",
    "pydantic_ai.models.ollama",
    "pydantic_ai.models.openai",
)


def _is_provider_specific(module: str) -> bool:
    if module.split(".")[0] in PROVIDER_SDKS:
        return True
    return any(module == name or module.startswith(f"{name}.") for name in PROVIDER_BINDINGS)


def _provider_imports(root, skip=()) -> list[str]:
    import ast

    offenders: list[str] = []
    for package in ("planner_core", "domains", "api", "scripts"):
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
                    if _is_provider_specific(name)
                ]
    return offenders


def test_no_module_outside_this_one_binds_itself_to_a_provider():
    """The rule was prose until now, which is how prose rules get broken.

    Mirrors `test_planner_core_imports_nothing_from_domains`: the same AST walk,
    for the other boundary in CLAUDE.md.
    """
    from pathlib import Path

    backend = Path(llm.__file__).resolve().parent.parent
    llm_module = backend / "planner_core" / "llm.py"

    assert _provider_imports(backend, skip=(llm_module,)) == []


def test_that_check_would_actually_catch_one():
    """Guards the guard: without the exemption, `llm.py` itself must trip it.

    The first version of this pair passed while detecting nothing, because
    `llm.py` imports `pydantic_ai.models.anthropic` rather than `anthropic` and
    only the latter was being looked for.
    """
    from pathlib import Path

    backend = Path(llm.__file__).resolve().parent.parent

    found = _provider_imports(backend)

    assert any(entry.startswith("planner_core/llm.py") for entry in found)


# --- How the app uses all this --------------------------------------------


def test_the_api_runs_the_model_check_on_startup(monkeypatch):
    """The check is worth nothing if it is never called."""
    from fastapi.testclient import TestClient

    from api import main

    called: list[bool] = []
    monkeypatch.setattr(main, "warn_missing_models", lambda: called.append(True))

    # `with`, because that is what runs the lifespan -- the bare `TestClient`
    # the other fixtures build deliberately does not.
    with TestClient(main.app):
        pass

    assert called == [True]


def test_a_startup_check_that_raises_does_not_stop_the_api(monkeypatch, caplog):
    """Most routes need no model at all, so none of them should be lost to this.

    A bug in a diagnostic is still only a bug in a diagnostic: it is logged and
    the API serves every solver route as usual.
    """
    from fastapi.testclient import TestClient

    from api import main

    def boom() -> None:
        raise RuntimeError("probe is broken")

    monkeypatch.setattr(main, "warn_missing_models", boom)

    with caplog.at_level(logging.ERROR, logger="api.main"), TestClient(main.app) as client:
        assert client.get("/openapi.json").status_code == 200

    assert "starting anyway" in caplog.text.lower()


def test_an_absent_model_service_is_a_503_naming_the_fix():
    """Not 500: the request was fine, so retrying it unchanged is right."""
    import asyncio

    from api.main import _llm_unavailable

    response = asyncio.run(_llm_unavailable(None, OllamaUnreachable(BASE_URL)))

    assert response.status_code == 503
    assert b"ollama serve" in response.body


@pytest.mark.parametrize(
    "error",
    [OllamaUnreachable(BASE_URL), ModelNotPulled("qwen2.5:14b")],
)
def test_a_subclass_reaches_the_handler_registered_on_the_base(error):
    """The whole two-subclass design rests on this one framework behaviour.

    Registering a handler for `LlmUnavailable` only helps if Starlette walks the
    MRO to find it -- otherwise `ModelNotPulled` would fall through to a 500 and
    the shared base would buy nothing. Asserted through a real app rather than
    by calling the handler directly, because the lookup is the untested part.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.main import _llm_unavailable

    app = FastAPI()
    app.add_exception_handler(LlmUnavailable, _llm_unavailable)

    @app.get("/boom")
    def boom() -> None:
        raise error

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")

    assert response.status_code == 503
    assert response.json()["detail"] == str(error)
