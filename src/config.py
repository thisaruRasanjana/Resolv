"""
src/config.py — Centralized configuration loaded from environment variables.

All other modules import from here rather than reading os.environ directly,
so there is a single place to see every knob the system has.

Note on GITHUB_TOKEN: it is loaded lazily via get_github_token() rather than
at import time, so modules that don't need it (embedder, indexer, triage) can
be imported and tested without a token in the environment.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env if it exists (does nothing in production where env vars are set externally)
load_dotenv()


def _require(key: str) -> str:
    """Return the value of an env var, raising clearly if it is not set."""
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return value


# ── GitHub ──────────────────────────────────────────────────────────────────
# Loaded lazily so importing config doesn't fail in test environments that
# don't need GitHub access (embedder, indexer, triage tests).
def get_github_token() -> str:
    """Return the GitHub token, raising clearly if it is not configured."""
    return _require("GITHUB_TOKEN")

# ── LLM (Ollama) ────────────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# ── Vector store (Qdrant) ───────────────────────────────────────────────────
QDRANT_HOST: str = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "resolv_issues")

# ── State store (SQLite) ────────────────────────────────────────────────────
SQLITE_PATH: Path = Path(os.getenv("SQLITE_PATH", "backtest/data/resolv.db"))

# ── Retrieval ───────────────────────────────────────────────────────────────
# Number of similar issues to retrieve per query (used by the triage worker)
TOP_K: int = int(os.getenv("TOP_K", "10"))

# ── Embedding model ─────────────────────────────────────────────────────────
# all-MiniLM-L6-v2: 384 dims, ~22 M params, fast on CPU/MPS.
# Swap to all-mpnet-base-v2 for higher quality at ~2x latency cost.
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# ── GitHub App (Phase 2) ────────────────────────────────────────────────
# Loaded lazily like GITHUB_TOKEN — webhook tests don't need these.
def get_github_app_id() -> int:
    """Return the GitHub App ID."""
    return int(_require("GITHUB_APP_ID"))

def get_github_private_key() -> str:
    """Read the GitHub App private key from the PEM file."""
    key_path = Path(_require("GITHUB_PRIVATE_KEY_PATH"))
    if not key_path.exists():
        raise RuntimeError(f"Private key file not found: {key_path}")
    return key_path.read_text()

def get_github_webhook_secret() -> str:
    """Return the webhook secret for HMAC verification."""
    return _require("GITHUB_WEBHOOK_SECRET")

# ── Redis ───────────────────────────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

# ── Queue ───────────────────────────────────────────────────────────────
EVENTS_STREAM: str = "resolv:events"
DEAD_LETTER_STREAM: str = "resolv:dead-letters"
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
