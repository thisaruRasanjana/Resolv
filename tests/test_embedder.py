"""
tests/test_embedder.py — Unit tests for src/embedder.py.

These tests load the actual model (cached after first run) to verify real
behaviour rather than mocking. They run fast (<5 s total) because the model
is small (22 M params) and gets cached in memory.
"""

import pytest

from src.embedder import embed, embed_batch, embedding_dim, prepare_text


def test_prepare_text_concatenates():
    text = prepare_text("Bug in login", "Steps to reproduce: 1. Open app")
    assert "Bug in login" in text
    assert "Steps to reproduce" in text


def test_prepare_text_truncates_long_body():
    long_body = "x" * 1000
    text = prepare_text("Title", long_body)
    # Body should be truncated to 512 chars; title is separate
    assert len(text) < 600


def test_prepare_text_handles_none_body():
    text = prepare_text("Title only", None)
    assert "Title only" in text
    assert text  # not empty


def test_embedding_dim_is_384():
    """all-MiniLM-L6-v2 always produces 384-dim vectors."""
    assert embedding_dim() == 384


def test_embed_returns_correct_length():
    vector = embed("Hello, world!")
    assert len(vector) == 384


def test_embed_returns_floats():
    vector = embed("Some issue text")
    assert all(isinstance(v, float) for v in vector)


def test_embed_is_unit_length():
    """Embeddings are normalized to unit length (norm ≈ 1.0)."""
    import math
    vector = embed("Test issue about a crash on startup")
    norm = math.sqrt(sum(v * v for v in vector))
    assert abs(norm - 1.0) < 1e-5, f"Expected unit norm, got {norm}"


def test_embed_batch_consistent_with_single():
    """embed_batch and embed should produce identical vectors for the same input."""
    import math
    texts = ["First issue", "Second issue"]
    batch = embed_batch(texts)
    single_0 = embed(texts[0])
    single_1 = embed(texts[1])

    # Vectors should be nearly identical (floating point tolerance)
    for a, b in zip(batch[0], single_0):
        assert abs(a - b) < 1e-5
    for a, b in zip(batch[1], single_1):
        assert abs(a - b) < 1e-5


def test_embed_batch_empty_input():
    result = embed_batch([])
    assert result == []


def test_similar_issues_rank_higher():
    """
    Semantically similar issues should have higher cosine similarity than
    unrelated issues. This is the core property the retrieval depends on.
    """
    import math

    query = embed("Application crashes when opening a file")
    similar = embed("App crashes on file open — stack overflow error")
    unrelated = embed("Add dark mode to the settings panel")

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        # Vectors are already unit length, so cosine == dot product
        return dot

    sim_score = cosine(query, similar)
    unrel_score = cosine(query, unrelated)

    assert sim_score > unrel_score, (
        f"Expected similar issue ({sim_score:.3f}) to score higher than "
        f"unrelated ({unrel_score:.3f})"
    )
