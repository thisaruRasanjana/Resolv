import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.rate_limiter import acquire_token


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE rate_limit_state (
            repo_id        TEXT PRIMARY KEY,
            tokens_remaining REAL NOT NULL,
            last_refill_at TEXT NOT NULL
        );
        """
    )
    yield conn
    conn.close()


def test_acquire_token_first_time(db_conn, monkeypatch):
    monkeypatch.setattr("src.config.RATE_LIMIT_TOKENS", 10)
    monkeypatch.setattr("src.config.RATE_LIMIT_REFILL_SEC", 60)

    # First time, should succeed and consume 1 token (leaving 9)
    assert acquire_token(db_conn, "owner/repo") is True

    cursor = db_conn.cursor()
    cursor.execute("SELECT tokens_remaining FROM rate_limit_state WHERE repo_id = 'owner/repo'")
    row = cursor.fetchone()
    assert row["tokens_remaining"] == 9.0


def test_acquire_token_exhaustion(db_conn, monkeypatch):
    monkeypatch.setattr("src.config.RATE_LIMIT_TOKENS", 3)
    monkeypatch.setattr("src.config.RATE_LIMIT_REFILL_SEC", 60)

    assert acquire_token(db_conn, "owner/repo") is True  # 2 left
    assert acquire_token(db_conn, "owner/repo") is True  # 1 left
    assert acquire_token(db_conn, "owner/repo") is True  # 0 left
    
    # 4th time should fail immediately since time hasn't passed
    assert acquire_token(db_conn, "owner/repo") is False


def test_acquire_token_refill(db_conn, monkeypatch):
    monkeypatch.setattr("src.config.RATE_LIMIT_TOKENS", 10)
    monkeypatch.setattr("src.config.RATE_LIMIT_REFILL_SEC", 60)

    now = datetime.now(timezone.utc)
    
    # Simulate an empty bucket from 6 seconds ago (should have refilled 1 token)
    db_conn.execute(
        """
        INSERT INTO rate_limit_state (repo_id, tokens_remaining, last_refill_at)
        VALUES (?, ?, ?)
        """,
        ("owner/repo", 0.0, (now - timedelta(seconds=6)).isoformat())
    )
    db_conn.commit()

    # We should have exactly 1 token now, so we can acquire once
    assert acquire_token(db_conn, "owner/repo") is True
    
    # But immediately fail on the next
    assert acquire_token(db_conn, "owner/repo") is False


def test_acquire_token_max_cap(db_conn, monkeypatch):
    monkeypatch.setattr("src.config.RATE_LIMIT_TOKENS", 5)
    monkeypatch.setattr("src.config.RATE_LIMIT_REFILL_SEC", 60)

    now = datetime.now(timezone.utc)
    
    # Simulate a bucket from a long time ago
    db_conn.execute(
        """
        INSERT INTO rate_limit_state (repo_id, tokens_remaining, last_refill_at)
        VALUES (?, ?, ?)
        """,
        ("owner/repo", 0.0, (now - timedelta(seconds=3600)).isoformat())
    )
    db_conn.commit()

    # Acquire should succeed, but tokens should be capped at 5-1 = 4.
    assert acquire_token(db_conn, "owner/repo") is True

    cursor = db_conn.cursor()
    cursor.execute("SELECT tokens_remaining FROM rate_limit_state WHERE repo_id = 'owner/repo'")
    row = cursor.fetchone()
    assert row["tokens_remaining"] == 4.0
