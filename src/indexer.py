"""
src/indexer.py — Qdrant vector store client wrapper.

One Qdrant collection holds every issue across every repo. Issues from
different repos are isolated at query time via a `repo_id` filter
(ARCHITECTURE.md §3.4 — multi-tenant isolation without separate databases).

Point payload schema (ARCHITECTURE.md §4):
    id            UUID  (deterministic: uuid5(NAMESPACE_DNS, f"{repo_id}/{issue_number}"))
    repo_id       str   ("owner/repo")
    issue_number  int
    type          "issue" | "pull_request"
    state         "open" | "closed"
    title         str
    body_excerpt  str   (first 512 chars of body)
    created_at    str   (ISO-8601 timestamp)
    updated_at    str   (ISO-8601 timestamp)
    vector        list[float]  (384-dim for all-MiniLM-L6-v2)

Functions:
    ensure_collection()     — create collection if it doesn't exist
    upsert_issue(...)       — insert or overwrite a single issue point
    upsert_issues_batch(...)— bulk upsert (used during backtest indexing)
    delete_issue(...)       — remove a point (rarely needed)
    search_similar(...)     — cosine similarity search filtered by repo_id
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    UpdateStatus,
    VectorParams,
)

from src import config
from src.embedder import embedding_dim
from src.logging import get_logger

log = get_logger(__name__)

# Deterministic UUID namespace for issue point IDs.
# uuid5(NAMESPACE_DNS, "microsoft/vscode/42") → always the same UUID,
# so upsert is truly idempotent: inserting the same issue twice overwrites cleanly.
_UUID_NAMESPACE = uuid.NAMESPACE_DNS


def _issue_id(repo_id: str, issue_number: int) -> str:
    """Deterministic UUID for a (repo_id, issue_number) pair."""
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{repo_id}/{issue_number}"))


# ── Client singleton ─────────────────────────────────────────────────────────

_client: QdrantClient | None = None


def get_client() -> QdrantClient:
    """Return a cached Qdrant client. Creates one on first call."""
    global _client
    if _client is None:
        _client = QdrantClient(
            host=config.QDRANT_HOST,
            port=config.QDRANT_PORT,
            # Suppress the version-mismatch warning — we manage compatibility
            # via pinned versions in pyproject.toml and docker-compose.yml.
            check_compatibility=False,
        )
        log.debug("qdrant client created", host=config.QDRANT_HOST, port=config.QDRANT_PORT)
    return _client


# ── Collection management ────────────────────────────────────────────────────

def ensure_collection() -> None:
    """
    Create the Qdrant collection if it does not already exist.

    Uses Cosine distance because embedder.py normalizes all vectors to unit
    length, making cosine == dot product (faster on some hardware).
    """
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if config.QDRANT_COLLECTION in existing:
        log.debug("collection already exists", collection=config.QDRANT_COLLECTION)
        return

    client.create_collection(
        collection_name=config.QDRANT_COLLECTION,
        vectors_config=VectorParams(size=embedding_dim(), distance=Distance.COSINE),
    )
    log.info("qdrant collection created", collection=config.QDRANT_COLLECTION, dim=embedding_dim())


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class IssuePoint:
    """Everything needed to create or update a Qdrant point for an issue."""
    repo_id: str
    issue_number: int
    type: str           # "issue" | "pull_request"
    state: str          # "open" | "closed"
    title: str
    body_excerpt: str   # first 512 chars of body
    created_at: datetime
    updated_at: datetime
    vector: list[float]


# ── Write operations ─────────────────────────────────────────────────────────

def upsert_issue(point: IssuePoint) -> None:
    """
    Insert or overwrite a single issue in Qdrant.

    Because point IDs are deterministic (based on repo_id + issue_number),
    calling this twice for the same issue is safe — the second call simply
    overwrites the first with fresh data. This is how we handle `edited` events.
    """
    client = get_client()
    result = client.upsert(
        collection_name=config.QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=_issue_id(point.repo_id, point.issue_number),
                vector=point.vector,
                payload={
                    "repo_id": point.repo_id,
                    "issue_number": point.issue_number,
                    "type": point.type,
                    "state": point.state,
                    "title": point.title,
                    "body_excerpt": point.body_excerpt,
                    "created_at": point.created_at.isoformat(),
                    "updated_at": point.updated_at.isoformat(),
                },
            )
        ],
    )
    if result.status != UpdateStatus.COMPLETED:
        log.warning("upsert may have failed", status=result.status, repo_id=point.repo_id,
                    issue_number=point.issue_number)


def upsert_issues_batch(points: list[IssuePoint], batch_size: int = 128) -> None:
    """
    Bulk upsert a list of IssuePoints in batches.

    Used during the backtest replay to build up the index incrementally.
    batch_size=128 is a practical limit that keeps memory usage reasonable
    while still getting good throughput from Qdrant's batch insert API.
    """
    client = get_client()
    for i in range(0, len(points), batch_size):
        batch = points[i : i + batch_size]
        qdrant_points = [
            PointStruct(
                id=_issue_id(p.repo_id, p.issue_number),
                vector=p.vector,
                payload={
                    "repo_id": p.repo_id,
                    "issue_number": p.issue_number,
                    "type": p.type,
                    "state": p.state,
                    "title": p.title,
                    "body_excerpt": p.body_excerpt,
                    "created_at": p.created_at.isoformat(),
                    "updated_at": p.updated_at.isoformat(),
                },
            )
            for p in batch
        ]
        client.upsert(collection_name=config.QDRANT_COLLECTION, points=qdrant_points)
    log.debug("batch upsert complete", count=len(points))


def delete_issue(repo_id: str, issue_number: int) -> None:
    """Remove an issue point from Qdrant. Rarely needed — prefer state updates."""
    client = get_client()
    client.delete(
        collection_name=config.QDRANT_COLLECTION,
        points_selector=[_issue_id(repo_id, issue_number)],
    )
    log.debug("issue deleted", repo_id=repo_id, issue_number=issue_number)


# ── Read operations ───────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """One result from a similarity search."""
    issue_number: int
    repo_id: str
    title: str
    body_excerpt: str
    state: str
    score: float        # cosine similarity [0, 1]; higher == more similar
    created_at: str
    type: str


def search_similar(
    query_vector: list[float],
    repo_id: str,
    top_k: int = 10,
    exclude_issue_number: int | None = None,
) -> list[SearchResult]:
    """
    Find the top-k most similar issues in a given repo.

    Always filters by repo_id so results never bleed across tenants.
    Optionally excludes a specific issue number (used to exclude the query
    issue itself, which would otherwise always be the top result once indexed).

    Returns results sorted by descending cosine similarity.
    """
    client = get_client()

    # Build a filter: repo_id must match.
    # Qdrant's filter API takes a list of must-conditions (logical AND).
    must_conditions = [
        FieldCondition(key="repo_id", match=MatchValue(value=repo_id))
    ]

    from qdrant_client.models import QueryResponse

    results = client.query_points(
        collection_name=config.QDRANT_COLLECTION,
        query=query_vector,
        query_filter=Filter(must=must_conditions),
        limit=top_k + (1 if exclude_issue_number is not None else 0),
        with_payload=True,
    ).points

    output: list[SearchResult] = []
    for hit in results:
        payload = hit.payload or {}
        if exclude_issue_number is not None and payload.get("issue_number") == exclude_issue_number:
            continue
        output.append(
            SearchResult(
                issue_number=payload["issue_number"],
                repo_id=payload["repo_id"],
                title=payload.get("title", ""),
                body_excerpt=payload.get("body_excerpt", ""),
                state=payload.get("state", "unknown"),
                score=hit.score,
                created_at=payload.get("created_at", ""),
                type=payload.get("type", "issue"),
            )
        )
    return output[:top_k]


def count_indexed(repo_id: str) -> int:
    """Return the number of indexed points for a given repo."""
    client = get_client()
    result = client.count(
        collection_name=config.QDRANT_COLLECTION,
        count_filter=Filter(
            must=[FieldCondition(key="repo_id", match=MatchValue(value=repo_id))]
        ),
        exact=True,
    )
    return result.count
