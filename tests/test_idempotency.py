import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src.db import get_connection
from src.idempotency import is_already_processed, mark_processed


@pytest.fixture
def db_conn():
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        yield conn
        conn.close()


def test_idempotency(db_conn):
    delivery_id = "test-123"
    worker_group = "test-group"
    repo_id = "owner/repo"

    assert not is_already_processed(db_conn, delivery_id, worker_group)

    mark_processed(db_conn, delivery_id, worker_group, repo_id)
    assert is_already_processed(db_conn, delivery_id, worker_group)

    # Calling it again shouldn't crash (INSERT OR IGNORE)
    mark_processed(db_conn, delivery_id, worker_group, repo_id)
    assert is_already_processed(db_conn, delivery_id, worker_group)
