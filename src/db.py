"""
src/db.py — SQLite state store (Phase 1).

Creates and manages the four tables defined in ARCHITECTURE.md §4:

    installations       — which repos have the app installed (used in Phase 2+)
    rate_limit_state    — per-repo token bucket state (used in Phase 4)
    processed_deliveries — idempotency: delivery IDs we have already handled (Phase 2+)
    backtest_results    — metrics from each backtest run (Phase 1)

In Phase 3 this module will be refactored to use asyncpg / PostgreSQL behind
the same interface. For now, sqlite3 (synchronous) is sufficient.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.logging import get_logger

log = get_logger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS installations (
    repo_id       TEXT PRIMARY KEY,
    installed_at  TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,   -- 0 = uninstalled
    settings_json TEXT
);

CREATE TABLE IF NOT EXISTS rate_limit_state (
    repo_id        TEXT PRIMARY KEY,
    tokens_remaining REAL NOT NULL,
    last_refill_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_deliveries (
    delivery_id  TEXT NOT NULL,
    worker_group TEXT NOT NULL,
    repo_id      TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (delivery_id, worker_group)
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id   TEXT NOT NULL,
    run_at    TEXT NOT NULL,
    precision REAL,
    recall    REAL,
    f1        REAL,
    notes     TEXT
);
"""


# ── Connection helper ────────────────────────────────────────────────────────

def get_connection(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database at db_path, apply the schema,
    and return the connection.

    Thread safety: sqlite3 connections are not thread-safe; callers that
    run in multiple threads should open one connection per thread.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row   # rows are accessible by column name
    conn.executescript(_CREATE_TABLES)
    conn.commit()
    log.debug("db connected", path=str(db_path))
    return conn


# ── Backtest results ─────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    repo_id: str
    run_at: datetime
    precision: float
    recall: float
    f1: float
    notes: str = ""


def save_backtest_result(conn: sqlite3.Connection, result: BacktestResult) -> None:
    """Insert a backtest run's metrics into the backtest_results table."""
    conn.execute(
        """
        INSERT INTO backtest_results (repo_id, run_at, precision, recall, f1, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            result.repo_id,
            result.run_at.isoformat(),
            result.precision,
            result.recall,
            result.f1,
            result.notes,
        ),
    )
    conn.commit()
    log.info(
        "backtest result saved",
        repo_id=result.repo_id,
        precision=round(result.precision, 4),
        recall=round(result.recall, 4),
        f1=round(result.f1, 4),
    )
