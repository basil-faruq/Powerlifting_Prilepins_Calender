"""The one place that knows which model answers and which one embeds.

Everything about the provider choice is behind `get_model()` and
`get_embedder()`, so flipping `LLM_PROVIDER` changes no other file: the router,
the tools and the tests all ask for a role ("agent" or "router") and get back
something that satisfies Pydantic AI's `Model`. No module outside this one
imports a provider SDK (CLAUDE.md), which is also what keeps the agent testable
-- a test overrides the model with `TestModel` and nothing reaches a network.

Embeddings are the deliberate asymmetry. They stay on Ollama in *every* mode,
including `LLM_PROVIDER=anthropic`, because the vectors in `note_chunk` are only
comparable to each other: swapping the embedding model mid-life silently
invalidates every stored vector, so the demo switch must not be able to do it.
`get_embedder()` therefore never looks at `llm_provider` at all.

The split of labour is the project's first non-negotiable: the model routes and
explains, and never computes. Nothing here returns a number that reaches the
athlete.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.ollama import OllamaProvider

from planner_core.config import get_settings
from planner_core.errors import LlmUnavailable, ModelNotPulled, OllamaUnreachable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.models import Model

logger = logging.getLogger(__name__)

ModelRole = Literal["agent", "router"]

# --- Embedding transport --------------------------------------------------
# Starting values, not derived. Picked to keep one note's worth of text in one
# request on a home GPU; raise the batch only with a measurement to justify it.
EMBED_BATCH = 32
EMBED_TIMEOUT_S = 30.0
EMBED_RETRIES = 2
EMBED_BACKOFF_S = 0.5

# `nomic-embed-text` is a task-conditioned model: the same sentence embeds to a
# different vector depending on the prefix, and a query embedded as a document
# retrieves badly. Ollama does not add these, so the only place they can be
# applied consistently is here -- which is the reason callers never build the
# request themselves.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

# The model handles 8192 tokens, but Ollama's own default for it is 2048 and
# `truncate` defaults to true, so a long note would be cut *silently* and embed
# as its first half. Sending this explicitly is what makes a full note a full
# vector.
EMBED_NUM_CTX = 8192

# A probe must not be able to hold startup open for the embedder's 30 s. Ollama
# answers `/api/tags` from memory, so this only has to cover a slow loopback.
TAGS_TIMEOUT_S = 5.0


class EmbedResponse(BaseModel):
    """Ollama's `/api/embed` body, parsed rather than indexed.

    This is a data shape crossing a trust boundary, so it is a Pydantic model
    like every other shape in the project. Reading it with `dict.get` was a
    real hole rather than a style question: `len()` is the only thing a manual
    check did, and `[[None] * 768]` and a 768-character *string* both have the
    right length. Both would have been handed back as "vectors" and stored.
    """

    embeddings: list[list[float]]


class Embedder(Protocol):
    """What the rest of the project is allowed to assume about embedding.

    A protocol rather than a base class so the tests' fake is a plain object
    with two methods and no import of this module's internals. The split
    between the two calls is not stylistic -- see `DOCUMENT_PREFIX`.
    """

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors for text being stored, in the order given."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """One vector for text being searched *with*."""
        ...


class OllamaEmbedder:
    """`POST /api/embed` on the host's Ollama, batched and retried.

    Holds no connection between calls: a client is opened per request batch, or
    injected whole by a test. At this project's scale (one athlete, a note per
    session) the handshake is noise next to the model's own latency, and not
    owning a long-lived client means nothing here can leak one.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = EMBED_TIMEOUT_S,
        retries: int = EMBED_RETRIES,
        backoff_s: float = EMBED_BACKOFF_S,
    ) -> None:
        if retries < 0:
            # Otherwise the attempt loop never runs and every call reports
            # "Ollama not reachable" without having contacted it once.
            raise ValueError(f"retries must be >= 0, got {retries}")
        if dim < 1:
            raise ValueError(f"dim must be >= 1, got {dim}")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        # An injected client is borrowed, never closed here, and carries its own
        # timeout -- so `timeout_s` applies only to the clients this class owns.
        self._client = client
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_s = backoff_s

    @property
    def _url(self) -> str:
        return f"{self.base_url}/api/embed"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._embed([DOCUMENT_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([QUERY_PREFIX + text])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(inputs), EMBED_BATCH):
            out.extend(self._embed_batch(inputs[start : start + EMBED_BATCH]))
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {
            "model": self.model,
            "input": batch,
            "options": {"num_ctx": EMBED_NUM_CTX},
        }
        vectors = self._post_with_retries(payload).embeddings
        if len(vectors) != len(batch):
            raise ValueError(
                f"model {self.model} returned {len(vectors)} embeddings for {len(batch)} inputs"
            )
        for vector in vectors:
            if len(vector) != self.dim:
                # Loud and immediate: a wrong width means the configured model
                # is not the one `note_chunk.embedding` was sized for, and a
                # vector that reached the database would poison every later
                # search rather than fail once here.
                raise ValueError(
                    f"model {self.model} returned {len(vector)}-dimensional embeddings, "
                    f"expected {self.dim}; set EMBED_DIM to match the model and re-embed"
                )
        return vectors

    def _post_with_retries(self, payload: dict[str, object]) -> EmbedResponse:
        """Retry the transient failures only, and translate the rest.

        A refused connection and a 5xx are worth another attempt; a 404 is not,
        because a model does not appear while we wait. The last failure is
        re-raised as `OllamaUnreachable` so a caller never has to know this
        module talks HTTP -- carrying its detail, because "answered 500" and
        "did not answer" lead to different places and the advice to run
        `ollama serve` only helps with the second.
        """
        last_detail = ""
        for attempt in range(self._retries + 1):
            if attempt:
                time.sleep(self._backoff_s * attempt)
            try:
                response = self._post(payload)
            except httpx.TransportError as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 404:
                raise ModelNotPulled(self.model, _error_text(response))
            if response.status_code >= 500:
                last_detail = f"HTTP {response.status_code}: {_error_text(response)}"
                continue
            # Anything that is not 2xx is ours to explain. A 3xx counts: httpx
            # does not follow redirects by default, so letting it through would
            # surface as a JSON parse error on an HTML body.
            if not response.is_success:
                raise ValueError(
                    f"Ollama rejected the embedding request ({response.status_code}): "
                    f"{_error_text(response)}"
                )
            try:
                return EmbedResponse.model_validate_json(response.content)
            except ValidationError as exc:
                raise ValueError(
                    f"model {self.model} returned a body that is not an embedding "
                    f"response: {exc.errors(include_url=False)[:3]}"
                ) from exc
        raise OllamaUnreachable(self.base_url, last_detail)

    def _post(self, payload: dict[str, object]) -> httpx.Response:
        if self._client is not None:
            return self._client.post(self._url, json=payload)
        with httpx.Client(timeout=self._timeout_s) as client:
            return client.post(self._url, json=payload)


def get_embedder() -> Embedder:
    """Always Ollama, in every provider mode. See this module's docstring."""
    settings = get_settings()
    return OllamaEmbedder(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embed_model,
        dim=settings.embed_dim,
    )


# --- Chat models ----------------------------------------------------------

_MODEL_CACHE: dict[ModelRole, Model] = {}

# Without this, two concurrent first requests both build a model and one is
# discarded still holding an unclosed HTTP client -- a socket-pool leak rather
# than a wrong answer, since the objects are stateless configuration holders,
# but a free thing to not have.
_MODEL_LOCK = threading.Lock()


def get_model(role: ModelRole) -> Model:
    """The model for a role, built once and reused.

    Two roles rather than one because the jobs differ in kind: "agent" answers
    the athlete and calls tools, "router" only picks a route from a list that
    deterministic Python has already filtered down to the safe ones (CLAUDE.md
    non-negotiable 3). On Ollama both default to the same local model -- there
    is one GPU and loading a second would evict the first -- while on Anthropic
    the router drops to Haiku, because choosing from a short list is the one job
    a small model is reliably good at.

    Cached by hand rather than with `functools.cache` so that `reset_models()`
    can clear it alongside the settings cache in tests.
    """
    cached = _MODEL_CACHE.get(role)
    if cached is not None:
        return cached
    with _MODEL_LOCK:
        # Checked again inside the lock: another thread may have built it while
        # this one waited, and the point of the lock is that only one wins.
        cached = _MODEL_CACHE.get(role)
        if cached is None:
            cached = _MODEL_CACHE[role] = _build_model(role)
        return cached


# HTTP statuses that mean "the machine behind the request is not serving it",
# as opposed to "the request was wrong". 404 is here because Ollama answers it
# for a model that is not pulled; 429 and 5xx because retrying unchanged is the
# right advice for them, which is what a 503 says (ADR-0013).
_UNAVAILABLE_STATUSES = frozenset({404, 408, 429})


def unavailable_from(exc: BaseException) -> LlmUnavailable | None:
    """The `LlmUnavailable` this failure amounts to, or `None` if it is not one.

    `LlmUnavailable` is raised directly only by the embedder and the startup
    probe. A *chat* model that is down raises Pydantic AI's own
    `ModelAPIError` / `ModelHTTPError` (or an `httpx` transport error beneath
    them) from inside `agent.run_sync`, and nothing translated those -- so a
    dead Ollama looked like a bad sentence: the router retried, gave up, and
    answered 200 "I couldn't understand that". Found by review; the tests
    missed it because `RefusingModel` raised `OllamaUnreachable` directly,
    which no real model call does.

    It lives here because this module is the one place allowed to know what a
    provider failure looks like (CLAUDE.md). It walks `__cause__` and
    `__context__`, since Pydantic AI wraps what the SDK raised.

    A 4xx that is not in `_UNAVAILABLE_STATUSES` is *not* translated: a 400 is
    our request being wrong, which is a bug and should surface as one.
    """
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, LlmUnavailable):
            return cause

        reachable = False
        detail = ""
        if isinstance(cause, ModelHTTPError):
            reachable = cause.status_code >= 500 or cause.status_code in _UNAVAILABLE_STATUSES
            detail = f"HTTP {cause.status_code} from {cause.model_name}"
        elif isinstance(cause, ModelAPIError | httpx.TransportError):
            reachable = True
            detail = str(cause)[:200]

        if reachable:
            settings = get_settings()
            if settings.llm_provider == "ollama":
                return OllamaUnreachable(settings.ollama_base_url, detail)
            return LlmUnavailable(f"The language model did not answer ({detail}). Try again.")
        cause = cause.__cause__ or cause.__context__
    return None


def structured[T](output_type: type[T]) -> object:
    """How to ask the configured model for a `T`, as an `Agent(output_type=...)`.

    **The mode matters more than the schema.** Pydantic AI's default is to
    expose the output type as a tool the model must call. On `qwen2.5:14b`
    through Ollama that is unreliable: with the router's one-field schema it
    scored 0/4, replying with nothing or with plain text; the full 15-field
    schema scored 7/30 on the eval (ADR-0015). The same model with Ollama's
    native structured output -- the schema handed to the server as a grammar
    -- scored 4/4 on the full schema in about 5 s a call instead of 78 s. My
    first diagnosis was schema *width*, and a two-stage router was built on it;
    measuring the modes side by side showed it was wrong, and that router was
    removed.

    So on Ollama the output is native. On Anthropic it stays the default tool
    call, which is what Claude is dependable at and what has not been shown to
    fail there -- and it is untested either way, since no request has ever been
    made to that provider (ADR-0013).

    It is here, not in the router, because which mode works is a fact about a
    provider, and this is the one module allowed to know about providers.
    """
    from pydantic_ai import NativeOutput

    if get_settings().llm_provider == "ollama":
        return NativeOutput(output_type)
    return output_type


def reset_models() -> None:
    """Drop the cache, so a changed `LLM_PROVIDER` is picked up.

    Called from the autouse fixture that clears the settings cache, not per
    test file. Those two caches have to be dropped together: a model built
    under one test's environment would otherwise be handed to the next test,
    which fails in a way that depends on test order.
    """
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()


def _build_model(role: ModelRole) -> Model:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        name = settings.ollama_chat_model if role == "agent" else settings.ollama_router_model
        # The provider wants Ollama's OpenAI-compatible surface, which lives
        # under `/v1`; the bare base URL is where its own REST API is, and that
        # is what the embedder and `/api/tags` use.
        base_url = settings.ollama_base_url.rstrip("/")
        return OllamaModel(name, provider=OllamaProvider(base_url=f"{base_url}/v1"))

    name = settings.anthropic_model if role == "agent" else settings.anthropic_router_model
    key = settings.anthropic_api_key
    if key is None:  # pragma: no cover - Settings refuses this combination first
        raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
    # `get_secret_value()` is called here and the result is handed straight to
    # the provider: it is never stored on a local, logged or put in a message,
    # so a traceback out of this module cannot carry the key.
    return AnthropicModel(name, provider=AnthropicProvider(api_key=key.get_secret_value()))


# --- Startup check --------------------------------------------------------


def check_ollama_models(client: httpx.Client | None = None) -> list[str]:
    """Which configured Ollama models are not pulled, in configured order.

    Returns rather than raises for the missing ones, because a missing model is
    only fatal to the request that needs it: the API still starts and serves
    every deterministic route, which is most of them. Being unable to *ask*
    Ollama at all is different, and does raise.

    `client` is for tests, the same as on `OllamaEmbedder`: an injected client
    is borrowed and never closed, so the caller keeps ownership of it.
    """
    settings = get_settings()
    base_url = settings.ollama_base_url.rstrip("/")
    wanted = list(
        dict.fromkeys(
            [
                settings.ollama_chat_model,
                settings.ollama_router_model,
                settings.ollama_embed_model,
            ]
        )
    )
    # The parse is inside the `try` on purpose. A body that is JSON but not the
    # shape expected -- `{"models": ["qwen2.5:14b"]}` rather than a list of
    # objects -- used to raise `AttributeError` from here, which escaped both
    # `warn_missing_models` and `check_llm.py`: a diagnostic whose whole job is
    # to report a problem died with a traceback instead of reporting one.
    try:
        payload = _get_tags(base_url, client)
        installed = _installed_tags(payload)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise OllamaUnreachable(base_url, f"{type(exc).__name__}: {exc}") from exc

    return [model for model in wanted if _normalise_tag(model) not in installed]


def _installed_tags(payload: dict) -> set[str]:
    entries = payload.get("models")
    if not isinstance(entries, list):
        return set()
    installed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for field in ("name", "model"):
            value = entry.get(field)
            if isinstance(value, str):
                installed.add(_normalise_tag(value))
    return installed


def _get_tags(base_url: str, client: httpx.Client | None) -> dict:
    url = f"{base_url}/api/tags"
    if client is not None:
        response = client.get(url)
    else:
        with httpx.Client(timeout=TAGS_TIMEOUT_S) as owned:
            response = owned.get(url)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _normalise_tag(name: str) -> str:
    """`nomic-embed-text` and `nomic-embed-text:latest` are the same model.

    Ollama reports every model with an explicit tag but accepts it without one,
    so a configured name has to be compared in the tagged form or a pulled
    model reads as missing.

    The colon is looked for in the last path segment only. A registry-qualified
    name like `registry:5000/qwen2.5` has a colon in its *host*, and treating
    that as a tag would leave it untagged on both sides of the comparison --
    so it would read as missing no matter what was pulled.
    """
    _, _, last = name.rpartition("/")
    return name if ":" in last else f"{name}:latest"


def warn_missing_models() -> None:
    """Log one line per missing model, naming the command that fixes it.

    Called at API startup and never allowed to stop it: a planner whose solvers
    all work is worth serving even with no model behind the chat panel.
    """
    settings = get_settings()
    if settings.llm_provider != "ollama":
        return
    try:
        missing = check_ollama_models()
    except OllamaUnreachable as exc:
        logger.warning("%s Agent routes will fail until it is running.", exc)
        return
    for model in missing:
        logger.warning("Model %s not found. Run: ollama pull %s", model, model)


def _error_text(response: httpx.Response) -> str:
    """Ollama's own `{"error": ...}`, or the raw body if it is not JSON."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            return error
    return response.text.strip()
