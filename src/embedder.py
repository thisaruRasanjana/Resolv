"""
src/embedder.py — Ollama embedding wrapper.

Provides two functions:
    embed(text)              → list[float]  (single embedding)
    embed_batch(texts)       → list[list[float]]  (batch, much faster)

Model: llama3.1:8b (4096-dimensional output vectors)

Endpoint compatibility:
    - Ollama >= 0.1.26: POST /api/embed   { "model": ..., "input": [...] }
    - Ollama <  0.1.26: POST /api/embeddings { "model": ..., "prompt": ... }
    embed_batch() tries /api/embed first; falls back to /api/embeddings
    automatically so the code works on any installed Ollama version.
"""

import httpx
from src import config
from src.logging import get_logger

log = get_logger(__name__)

BODY_MAX_CHARS = 512

_EMBED_URL = f"{config.OLLAMA_BASE_URL}/api/embed"
_EMBED_LEGACY_URL = f"{config.OLLAMA_BASE_URL}/api/embeddings"
EMBEDDING_MODEL = config.EMBEDDING_MODEL


def prepare_text(title: str, body: str | None) -> str:
    body_excerpt = (body or "").strip()[:BODY_MAX_CHARS]
    return f"{title.strip()}\n{body_excerpt}"


def _embed_batch_new_api(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    """POST /api/embed — Ollama >= 0.1.26. Accepts a list of inputs."""
    resp = client.post(_EMBED_URL, json={
        "model": EMBEDDING_MODEL,
        "input": texts,
    }, timeout=120.0)
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _embed_batch_legacy_api(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    """POST /api/embeddings — Ollama < 0.1.26. Single prompt per request."""
    vectors = []
    for text in texts:
        resp = client.post(_EMBED_LEGACY_URL, json={
            "model": EMBEDDING_MODEL,
            "prompt": text,
        }, timeout=30.0)
        resp.raise_for_status()
        vectors.append(resp.json()["embedding"])
    return vectors


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts. Tries new API first, falls back to legacy."""
    if not texts:
        return []
    log.debug("embedding batch via Ollama", num_texts=len(texts))
    with httpx.Client() as client:
        try:
            return _embed_batch_new_api(client, texts)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.warning(
                    "Ollama /api/embed returned 404 — falling back to legacy /api/embeddings",
                    ollama_url=config.OLLAMA_BASE_URL,
                )
                return _embed_batch_legacy_api(client, texts)
            raise


def embed(text: str) -> list[float]:
    """Embed a single text string."""
    log.debug("embedding text via Ollama", model=EMBEDDING_MODEL)
    return embed_batch([text])[0]

def embedding_dim() -> int:
    """Return the dimensionality of the loaded model's output vectors."""
    return config.EMBEDDING_DIM
