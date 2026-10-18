"""Test doubles shared by the embedding and notes suites.

No test in this project calls a real model (CLAUDE.md). For the agent that
means Pydantic AI's `TestModel`; for embeddings it means this, because what the
retrieval tests actually need is not a *stub* but a vector space with real
geometry: "how has my knee been?" has to come back closer to a note about a
knee than to one about sleep, or the test proves nothing about the query.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

TOKEN = re.compile(r"[a-z0-9]+")

# Without these the fake is dominated by the words that carry no meaning. The
# first version of this file had no stopword list, and a note about a knee came
# out *closer* to "slept five hours and the bar felt slow" than to "how has my
# knee been?" -- because the two shared "the" and "felt" while the real match
# shared only "knee". A real embedder does not weigh "the" that way, so neither
# can a double that retrieval tests assert an ordering against.
STOPWORDS = frozenset(
    # fmt: off
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "out",
        "so",
        "that",
        "the",
        "then",
        "this",
        "to",
        "up",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "without",
        "you",
        "your",
    ]
    # fmt: on
)

# `nomic-embed-text`'s width, so a fake vector fits the real `note_chunk`
# column and the dimension check in `OllamaEmbedder` would pass on it too.
DIM = 768


def _vector(text: str, dim: int) -> list[float]:
    """A hashed bag of words, L2-normalised.

    Deterministic, so a committed assertion about ordering stays true, and
    *meaningfully* positioned, because shared words land on shared axes: two
    texts with a word in common have a smaller cosine distance than two with
    none. That is the only property the retrieval tests rely on.

    Normalising matters for cosine distance specifically -- without it a long
    note would sit further from everything simply for being long.
    """
    vector = [0.0] * dim
    for token in TOKEN.findall(text.lower()):
        if token in STOPWORDS:
            continue
        # Blake2b rather than `hash()`: Python salts string hashing per
        # process, so `hash()` would make these vectors differ between runs.
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        vector[int.from_bytes(digest, "big") % dim] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        # An empty or punctuation-only note. A zero vector has no direction, so
        # cosine distance to it is undefined; one fixed axis keeps it finite and
        # keeps it far from everything with actual words in it.
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


class FakeEmbedder:
    """Satisfies `planner_core.llm.Embedder` without a network.

    Records what it was asked for, so a test can assert that indexing embedded
    the chunks it expected, or that a failed path embedded nothing at all.
    """

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.documents: list[list[str]] = []
        self.queries: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.documents.append(list(texts))
        return [_vector(text, self.dim) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return _vector(text, self.dim)


class BrokenEmbedder:
    """Raises whatever it was given, on any call.

    For the spec 12 guarantee that a training log survives Ollama being down:
    the log is saved and the note is left for `reindex`.
    """

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise self.error

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        raise self.error


# --- Models (spec 13) -----------------------------------------------------
#
# Functions returning Pydantic AI models rather than new classes: `TestModel`
# and `FunctionModel` already are the doubles, and `conftest.py` pins
# `ALLOW_MODEL_REQUESTS = False` session-wide so a test that forgets to pass
# one of these fails loudly instead of reaching the developer's Ollama.
# Verified by probe: `TestModel` is exempt from that switch, as it must be.


def router_model(choice: object, *, fail_times: int = 0) -> Model:
    """A router that returns `choice`, after `fail_times` unusable answers.

    Answers in whichever structured-output mode the agent asked for. On Ollama
    that is *native* -- the reply is JSON text -- and on Anthropic it is a tool
    call (`llm.structured`), so a fake that only spoke one would silently stop
    testing the other path.

    `fail_times` is what makes spec 13 §11's two-strikes rule testable: the
    first answers are plain text that is not the schema, which is how a 14b
    model fails at this in practice.
    """
    args = dict(choice if isinstance(choice, Mapping) else choice.model_dump(mode="json"))
    requests = {"n": 0}

    def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests["n"] += 1
        if requests["n"] <= fail_times:
            return ModelResponse(parts=[TextPart("I think you want to plan something")])
        if info.output_tools:
            tool = info.output_tools[0]
            declared = set(tool.parameters_json_schema.get("properties", {}))
            return ModelResponse(
                parts=[ToolCallPart(tool.name, {k: v for k, v in args.items() if k in declared})]
            )
        return ModelResponse(parts=[TextPart(json.dumps(args, default=str))])

    return FunctionModel(respond)


def replying_model(text: str) -> Model:
    """A model whose answer is exactly `text`.

    For the explainer, where the point of the test is the post-check: an exact
    reply is the only way to assert both that a good one survives and that one
    quoting an invented number is thrown away.
    """
    return TestModel(custom_output_text=text)


def tool_calling_model(tool: str, then: str) -> Model:
    """Calls `tool` once with no arguments, then answers `then`.

    For the `answer` route, where what matters is the sequence -- the agent
    consulted the database before speaking -- which `TestModel` cannot express.
    """

    def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        called = any(
            isinstance(part, ToolCallPart) and part.tool_name == tool
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if not called:
            return ModelResponse(parts=[ToolCallPart(tool, {})])
        return ModelResponse(parts=[TextPart(then)])

    return FunctionModel(respond)


class RefusingModel(Model):
    """Raises `OllamaUnreachable` on any request.

    Two jobs. It stands in for a dead Ollama, so the 503 path and the
    `outcome = "failed"` row can be tested; and it is how "no LLM call was
    made" becomes an assertion rather than a claim -- a filter test that passes
    with this model installed cannot have reached layer 2.
    """

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        super().__init__()
        # Not `self.base_url`: `Model` declares that a read-only property.
        self._url = base_url
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "refusing"

    @property
    def system(self) -> str:
        return "refusing"

    async def request(self, *args: object, **kwargs: object) -> ModelResponse:
        self.calls += 1
        # Pydantic AI's own error, not `OllamaUnreachable`: that is what a real
        # dead server raises from inside `agent.run`, and the first version of
        # this fake raised the latter directly -- which no real call does, so
        # every test passed while a genuine outage became a bare 500 (found by
        # review). `llm.unavailable_from` is what turns this into the 503.
        raise ModelAPIError("refusing", f"connection refused at {self._url}")
