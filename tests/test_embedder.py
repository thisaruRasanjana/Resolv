"""
tests/test_embedder.py — Unit tests for src/embedder.py.

Tests prepare_text logic (pure functions, no network needed) and embedding
dim config. The actual Ollama embedding calls are tested via integration
tests only (require a running Ollama instance).
"""

import pytest

from src.embedder import embedding_dim, prepare_text


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


def test_embedding_dim_is_4096():
    """llama3.1:8b produces 4096-dim vectors via Ollama."""
    assert embedding_dim() == 4096


def test_embed_batch_empty_input():
    from src.embedder import embed_batch
    result = embed_batch([])
    assert result == []
