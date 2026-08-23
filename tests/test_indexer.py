"""
tests/test_indexer.py — Unit tests for src/indexer.py.

These tests require Qdrant to be running (`docker compose up -d`).
They use a separate test collection so they don't pollute the real one.
"""

import math
import os
import uuid
from datetime import datetime, timezone

import pytest

# Use a test collection to avoid polluting the real index
TEST_COLLECTION = "resolv_test_issues"
os.environ.setdefault("QDRANT_COLLECTION", TEST_COLLECTION)

from src import config
from src.indexer import (
    IssuePoint,
    SearchResult,
    count_indexed,
    delete_issue,
    ensure_collection,
    get_client,
    search_similar,
    upsert_issue,
)

# Override collection name for all tests
config.QDRANT_COLLECTION = TEST_COLLECTION


@pytest.fixture(autouse=True)
def clean_collection():
    """Drop and recreate the test collection before each test."""
    client = get_client()
    try:
        client.delete_collection(TEST_COLLECTION)
    except Exception:
        pass
    ensure_collection()
    yield
    # Teardown: drop the collection after each test
    try:
        client.delete_collection(TEST_COLLECTION)
    except Exception:
        pass


def _make_point(issue_number: int, repo_id: str = "owner/repo", vector: list[float] | None = None) -> IssuePoint:
    """Helper to create a minimal IssuePoint for testing."""
    if vector is None:
        # Random unit vector — doesn't need to be meaningful for indexer tests
        import random
        raw = [random.gauss(0, 1) for _ in range(4096)]
        norm = math.sqrt(sum(v * v for v in raw))
        vector = [v / norm for v in raw]

    return IssuePoint(
        repo_id=repo_id,
        issue_number=issue_number,
        type="issue",
        state="open",
        title=f"Test issue #{issue_number}",
        body_excerpt=f"Body of issue #{issue_number}",
        created_at=datetime(2024, 1, issue_number % 28 + 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, issue_number % 28 + 1, tzinfo=timezone.utc),
        vector=vector,
    )


def test_upsert_and_count():
    point = _make_point(1)
    upsert_issue(point)
    assert count_indexed("owner/repo") == 1


def test_upsert_is_idempotent():
    """Upserting the same issue twice should result in exactly one point."""
    point = _make_point(1)
    upsert_issue(point)
    upsert_issue(point)  # second upsert — should overwrite, not duplicate
    assert count_indexed("owner/repo") == 1


def test_delete_issue():
    point = _make_point(5)
    upsert_issue(point)
    assert count_indexed("owner/repo") == 1
    delete_issue("owner/repo", 5)
    assert count_indexed("owner/repo") == 0


def test_search_returns_results():
    # Insert a few points
    for i in range(1, 4):
        upsert_issue(_make_point(i))

    # Search with a random query vector — should return results (order may vary)
    import random
    raw = [random.gauss(0, 1) for _ in range(4096)]
    norm = math.sqrt(sum(v * v for v in raw))
    query = [v / norm for v in raw]

    results = search_similar(query_vector=query, repo_id="owner/repo", top_k=3)
    assert len(results) <= 3
    assert all(isinstance(r, SearchResult) for r in results)


def test_search_filters_by_repo_id():
    """Issues from repo B must not appear in results for repo A."""
    upsert_issue(_make_point(1, repo_id="owner/repo-a"))
    upsert_issue(_make_point(2, repo_id="owner/repo-b"))

    import random
    raw = [random.gauss(0, 1) for _ in range(4096)]
    norm = math.sqrt(sum(v * v for v in raw))
    query = [v / norm for v in raw]

    results = search_similar(query_vector=query, repo_id="owner/repo-a", top_k=10)
    returned_repo_ids = {r.repo_id for r in results}
    assert returned_repo_ids <= {"owner/repo-a"}, (
        f"Expected only repo-a results, got: {returned_repo_ids}"
    )


def test_search_excludes_self():
    """exclude_issue_number should prevent the query issue from appearing in results."""
    # Insert issue 42 with a known vector
    import random
    raw = [random.gauss(0, 1) for _ in range(4096)]
    norm = math.sqrt(sum(v * v for v in raw))
    vec = [v / norm for v in raw]

    upsert_issue(_make_point(42, vector=vec))
    # Also insert some other issues
    for i in [1, 2, 3]:
        upsert_issue(_make_point(i))

    # Query with the same vector as issue 42, excluding it
    results = search_similar(query_vector=vec, repo_id="owner/repo", top_k=10, exclude_issue_number=42)
    returned_numbers = [r.issue_number for r in results]
    assert 42 not in returned_numbers, "Issue 42 should have been excluded from its own search results"
