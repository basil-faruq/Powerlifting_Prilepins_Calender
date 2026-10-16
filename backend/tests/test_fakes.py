"""The test doubles are themselves tested, because spec 12 will trust them.

`FakeEmbedder` is not a stub that returns anything plausible: spec 12's
retrieval tests assert an *ordering* ("how has my knee been?" finds the knee
note first), and that only holds if shared words really do reduce cosine
distance. If this file is wrong, those tests pass or fail for reasons that have
nothing to do with the code they are meant to be checking.
"""

from __future__ import annotations

import math

import pytest

from planner_core.errors import OllamaUnreachable
from planner_core.llm import Embedder
from tests.fakes import DIM, BrokenEmbedder, FakeEmbedder


def _cosine_distance(a: list[float], b: list[float]) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b, strict=True))


def test_it_satisfies_the_embedder_protocol():
    """No type checker is configured, so protocol drift is caught here or not
    at all. The annotation is the assertion."""
    embedder: Embedder = FakeEmbedder()

    assert embedder.embed_query("x")


def test_every_vector_has_the_real_models_width():
    """So a fake vector fits the real `VECTOR(768)` column."""
    embedder = FakeEmbedder()

    assert len(embedder.embed_query("knee")) == DIM
    assert [len(v) for v in embedder.embed_documents(["a", "b"])] == [DIM, DIM]


def test_the_same_text_always_embeds_the_same_way():
    """Across calls *and* across processes: the hash is Blake2b rather than
    `hash()`, which Python salts per process."""
    assert FakeEmbedder().embed_query("knee pain") == FakeEmbedder().embed_query("knee pain")


def test_the_note_a_query_is_about_ranks_first():
    """The one property the retrieval tests depend on, in the shape they use it:
    one query against several notes, ordered by distance.

    These are spec 12 §12's own example texts, so if the ordering holds here it
    holds there.
    """
    embedder = FakeEmbedder()
    knee, sleep = embedder.embed_documents(
        [
            "left knee felt tight at the bottom of squats",
            "slept five hours and the bar felt slow",
        ]
    )
    query = embedder.embed_query("how has my knee been?")

    assert _cosine_distance(knee, query) < _cosine_distance(sleep, query)


def test_a_word_shared_only_through_stopwords_does_not_make_a_match():
    """What broke the first version of the fake: "the" and "felt" in common
    outranked "knee" in common."""
    embedder = FakeEmbedder()
    vector = embedder.embed_documents(["the bar felt slow"])[0]
    stopwords_only = embedder.embed_query("what is the bottom of this")

    assert _cosine_distance(vector, stopwords_only) == pytest.approx(1.0)


def test_every_vector_is_normalised():
    """Without this a long note would sit further from everything merely for
    being long, which would make an ordering assertion meaningless."""
    vector = FakeEmbedder().embed_query("a somewhat longer sentence with several words in it")

    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


@pytest.mark.parametrize("text", ["", "   ", "!!! ... ???"])
def test_a_text_with_no_words_still_has_a_direction(text):
    """A zero vector has no direction, so its cosine distance is undefined.
    One fixed axis keeps it finite and keeps it far from any real note."""
    embedder = FakeEmbedder()
    empty = embedder.embed_query(text)
    real = embedder.embed_query("knee")

    assert math.isclose(math.sqrt(sum(v * v for v in empty)), 1.0, rel_tol=1e-9)
    assert _cosine_distance(empty, real) == pytest.approx(1.0)


def test_it_records_what_it_was_asked_to_embed():
    """Spec 12 asserts that a failed path embedded nothing at all."""
    embedder = FakeEmbedder()
    embedder.embed_documents(["one", "two"])
    embedder.embed_query("three")

    assert embedder.documents == [["one", "two"]]
    assert embedder.queries == ["three"]


def test_the_broken_embedder_raises_what_it_was_given():
    error = OllamaUnreachable("http://localhost:11434")
    embedder = BrokenEmbedder(error)

    with pytest.raises(OllamaUnreachable):
        embedder.embed_documents(["anything"])
    with pytest.raises(OllamaUnreachable):
        embedder.embed_query("anything")
    assert embedder.calls == 2
