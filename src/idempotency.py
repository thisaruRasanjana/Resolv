"""
src/idempotency.py — Webhook delivery deduplication.

ARCHITECTURE.md §6: GitHub retries webhook deliveries on non-2xx responses
or timeouts. Without protection, the same issue could get triaged and
commented on twice. Every webhook payload includes a unique X-GitHub-Delivery
header — we check it against the processed_deliveries table before processing.
"""

import sqlite3
from datetime import datetime, timezone

from src.logging import get_logger

log = get_logger(__name__)


def is_already_processed(conn: sqlite3.Connection, delivery_id: str, worker_group: str) -> bool:
    """Check if we've already processed this webhook delivery for this consumer group."""
    row = conn.execute(
        "SELECT 1 FROM processed_deliveries WHERE delivery_id = ? AND worker_group = ?",
        (delivery_id, worker_group),
    ).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, delivery_id: str, worker_group: str, repo_id: str) -> None:
    """Record that we've processed this delivery (INSERT OR IGNORE for safety)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO processed_deliveries (delivery_id, worker_group, repo_id, processed_at)
        VALUES (?, ?, ?, ?)
        """,
        (delivery_id, worker_group, repo_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    log.debug("delivery marked processed", delivery_id=delivery_id, worker_group=worker_group)
