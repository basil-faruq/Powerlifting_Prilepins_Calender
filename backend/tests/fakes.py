"""Test doubles shared by the embedding and notes suites.

No test in this project calls a real model (CLAUDE.md). For the agent that
means Pydantic AI's `TestModel`; for embeddings it means this, because what the
retrieval tests actually need is not a *stub* but a vector space with real
geometry: "how has my knee been?" has to come back closer to a note about a
knee than to one about sleep, or the test proves nothing about the query.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

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
