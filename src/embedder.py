"""
src/embedder.py — Sentence-transformers embedding wrapper.

Provides two functions:
    embed(text)              → list[float]  (single embedding)
    embed_batch(texts)       → list[list[float]]  (batch, much faster)

Model: all-MiniLM-L6-v2
    - 384-dimensional output vectors
    - ~22 M parameters, MIT licensed
    - Runs on Apple MPS (M1/M2/M3 Metal GPU) automatically when available,
      falling back to CPU otherwise
    - Max input: 256 word-pieces; longer text is truncated by the model

Input text: concatenate issue title + "\n" + body (body truncated to first
512 characters) before calling embed(). The indexer and triage modules do
this via the `prepare_text` helper below.

To swap to a higher-quality model (e.g. all-mpnet-base-v2, 768-dim):
    1. Change EMBEDDING_MODEL in .env
    2. Update QDRANT_COLLECTION name so the old index doesn't conflict
    3. Re-run `make fetch && make backtest`
"""

from functools import lru_cache

from sentence_transformers import SentenceTransformer

from src import config
from src.logging import get_logger

log = get_logger(__name__)

# Maximum characters of issue body to use (keeps embeddings focused on the
# key problem description, avoids noise from long template boilerplate)
BODY_MAX_CHARS = 512


# ── Model singleton ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    """
    Load the embedding model once and cache it for the process lifetime.
    lru_cache ensures we never load the ~90 MB model weights twice.

    Device selection:
    - Defaults to CPU to avoid competing with Ollama for GPU memory.
      On M2 Air (8 GB unified), llama3.1:8b alone uses ~5 GB, leaving
      no room for MPS embeddings (triggers Metal OOM).
    - Set EMBEDDING_DEVICE=mps in .env if you are NOT running Ollama
      concurrently (e.g. for batch indexing only) and want faster embeddings.
    - Embedding is never the throughput bottleneck vs LLM inference anyway.
    """
    import os

    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    log.info("loading embedding model", model=config.EMBEDDING_MODEL)
    model = SentenceTransformer(config.EMBEDDING_MODEL, device=device)
    log.info("embedding model loaded", device=str(model.device))
    return model


# ── Public helpers ───────────────────────────────────────────────────────────

def prepare_text(title: str, body: str | None) -> str:
    """
    Build the text string that gets embedded for an issue.

    We concatenate title + body because:
    - Title alone misses detail (e.g. "crash on startup" is ambiguous).
    - Full body can be thousands of tokens; the first 512 chars almost
      always contain the problem description and reproduction steps.
    """
    body_excerpt = (body or "").strip()[:BODY_MAX_CHARS]
    return f"{title.strip()}\n{body_excerpt}"


def embed(text: str) -> list[float]:
    """
    Embed a single string and return a list of floats.

    For bulk indexing use embed_batch() — it's substantially faster because
    the model processes texts in parallel on GPU/MPS.
    """
    model = _get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of strings in one forward pass.

    normalize_embeddings=True scales vectors to unit length so that cosine
    similarity == dot product, which Qdrant uses when the collection is
    configured with Cosine distance.
    """
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=len(texts) > 50)
    return [v.tolist() for v in vectors]


def embedding_dim() -> int:
    """Return the dimensionality of the loaded model's output vectors."""
    return _get_model().get_embedding_dimension()
