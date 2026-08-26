"""
src/rate_limiter.py — Token bucket rate limiter.

Prevents noisy neighbor issues or API abuse by rate limiting webhooks per repository.
Uses SQLite for state storage with a BEGIN IMMEDIATE transaction to prevent race
conditions between concurrent workers.
"""

import sqlite3
from datetime import datetime, timezone

from src import config
from src.logging import get_logger

log = get_logger(__name__)


def acquire_token(conn: sqlite3.Connection, repo_id: str) -> bool:
    """
    Attempt to consume 1 token for the given repo_id.
    Returns True if a token was consumed (allowed), False if rate limited.

    Uses a token bucket algorithm:
    - MAX_TOKENS = config.RATE_LIMIT_TOKENS (default 10)
    - REFILL_RATE = 1 token per (config.RATE_LIMIT_REFILL_SEC / MAX_TOKENS) seconds,
      meaning it takes config.RATE_LIMIT_REFILL_SEC to refill from 0 to MAX_TOKENS.
    """
    max_tokens = float(config.RATE_LIMIT_TOKENS)
    refill_time_sec = float(config.RATE_LIMIT_REFILL_SEC)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    cursor = conn.cursor()

    try:
        # Use BEGIN IMMEDIATE to exclusively lock the DB for writes, preventing
        # concurrent workers from reading the same token count and decrementing it.
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "SELECT tokens_remaining, last_refill_at FROM rate_limit_state WHERE repo_id = ?",
            (repo_id,)
        )
        row = cursor.fetchone()

        if row is None:
            # First time seeing this repo. Start with a full bucket and consume 1 token.
            tokens = max_tokens - 1.0
            cursor.execute(
                """
                INSERT INTO rate_limit_state (repo_id, tokens_remaining, last_refill_at)
                VALUES (?, ?, ?)
                """,
                (repo_id, tokens, now_iso)
            )
            conn.commit()
            return True

        tokens_remaining = float(row["tokens_remaining"])
        last_refill_at = datetime.fromisoformat(row["last_refill_at"])

        # Calculate time elapsed
        elapsed_sec = (now - last_refill_at).total_seconds()
        
        # Calculate tokens to add based on elapsed time
        # E.g., if refill_time_sec=60 and max_tokens=10, we get 1 token every 6 seconds.
        tokens_to_add = elapsed_sec * (max_tokens / refill_time_sec)
        
        tokens = min(max_tokens, tokens_remaining + tokens_to_add)

        if tokens >= 1.0:
            # We have a token! Consume it.
            tokens -= 1.0
            cursor.execute(
                """
                UPDATE rate_limit_state
                SET tokens_remaining = ?, last_refill_at = ?
                WHERE repo_id = ?
                """,
                (tokens, now_iso, repo_id)
            )
            conn.commit()
            return True
        else:
            # Rate limited. Do NOT update last_refill_at, keep it as is so it continues refilling.
            conn.rollback()
            log.warning("rate_limit_exceeded", repo_id=repo_id, tokens_remaining=round(tokens, 2))
            return False

    except Exception as e:
        conn.rollback()
        log.error("rate_limit_error", error=str(e), repo_id=repo_id)
        # If DB errors, fail closed (return False) is safer but could block processing.
        # Let's raise the exception to let the caller handle it.
        raise
