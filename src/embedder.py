"""
src/embedder.py — Ollama embedding wrapper.

Provides two functions:
    embed(text)              → list[float]  (single embedding)
    embed_batch(texts)       → list[list[float]]  (batch, much faster)

Model: llama3.1:8b (using Ollama's /api/embed endpoint)
    - 4096-dimensional output vectors
    - Uses the same LLM running locally to produce embeddings!
"""

import os
import httpx
from src import config
from src.logging import get_logger

log = get_logger(__name__)

BODY_MAX_CHARS = 512

# We hit the Ollama /api/embed API. Since it is running on the host, 
# Docker pods access it via host.docker.internal.
OLLAMA_EMBED_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434") + "/api/embed"
# Use the same model as the LLM for embeddings to save RAM
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "llama3.1:8b")

def prepare_text(title: str, body: str | None) -> str:
    body_excerpt = (body or "").strip()[:BODY_MAX_CHARS]
    return f"{title.strip()}\n{body_excerpt}"

def embed(text: str) -> list[float]:
    log.debug("embedding text via Ollama", model=EMBEDDING_MODEL)
    with httpx.Client() as client:
        resp = client.post(OLLAMA_EMBED_URL, json={
            "model": EMBEDDING_MODEL,
            "input": text
        }, timeout=30.0)
        resp.raise_for_status()
        # Ollama returns {"embeddings": [[float, ...]]}
        return resp.json()["embeddings"][0]

def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    log.debug("embedding batch via Ollama", num_texts=len(texts))
    with httpx.Client() as client:
        resp = client.post(OLLAMA_EMBED_URL, json={
            "model": EMBEDDING_MODEL,
            "input": texts
        }, timeout=120.0)
        resp.raise_for_status()
        return resp.json()["embeddings"]

def embedding_dim() -> int:
    """Return the dimensionality of the loaded model's output vectors."""
    return config.EMBEDDING_DIM
