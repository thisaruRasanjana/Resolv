"""
src/workers/ingestion.py — Ingestion worker.

Consumes events from Redis stream (ingestion-group) and maintains
the Qdrant index. ARCHITECTURE.md §3.3.

Actions:
- opened: embed + upsert the new issue into Qdrant
- edited: re-embed + overwrite (same deterministic UUID)
- closed: update state field only (no re-embed needed)
"""

import asyncio
from datetime import datetime

from src import config
from src.db import get_connection
from src.embedder import embed, prepare_text
from src.idempotency import is_already_processed, mark_processed
from src.indexer import IssuePoint, ensure_collection, upsert_issue
from src.logging import get_logger
from src.queue import ack_event, consume_events, ensure_consumer_groups, move_to_dead_letter

log = get_logger(__name__)

GROUP = "ingestion-group"
CONSUMER = "ingestion-worker-1"


async def process_event(event_data: dict, conn) -> None:
    """Process a single ingestion event."""
    event_type = event_data.get("event_type", "issues")
    action = event_data["action"]

    if event_type in ("installation", "installation_repositories"):
        import json
        now_iso = datetime.now().isoformat()
        cursor = conn.cursor()

        # Handle 'created' (app installed on repos) or 'added' (new repo added to existing install)
        if action in ("created", "added"):
            repos_key = "repositories" if action == "created" else "repositories_added"
            repos = json.loads(event_data.get(repos_key, "[]"))
            for repo in repos:
                repo_id = repo.get("full_name")
                if repo_id:
                    cursor.execute(
                        """
                        INSERT INTO installations (repo_id, installed_at, is_active)
                        VALUES (?, ?, 1)
                        ON CONFLICT(repo_id) DO UPDATE SET is_active = 1
                        """,
                        (repo_id, now_iso)
                    )
                    log.info("repo installed", repo_id=repo_id)

        # Handle 'removed' (repo removed from existing install)
        elif action == "removed":
            repos = json.loads(event_data.get("repositories_removed", "[]"))
            for repo in repos:
                repo_id = repo.get("full_name")
                if repo_id:
                    cursor.execute("UPDATE installations SET is_active = 0 WHERE repo_id = ?", (repo_id,))
                    log.info("repo uninstalled", repo_id=repo_id)

        conn.commit()
        return

    # From here on, we handle issue events
    repo_id = event_data["repo_id"]
    issue_number = event_data["issue_number"]
    title = event_data["title"]
    body = event_data.get("body", "")

    if action in ("opened", "edited"):
        # Embed and upsert
        text = prepare_text(title, body)
        vector = embed(text)

        point = IssuePoint(
            repo_id=repo_id,
            issue_number=issue_number,
            type="issue",
            state=event_data.get("state", "open"),
            title=title,
            body_excerpt=body[:512],
            created_at=datetime.fromisoformat(event_data["created_at"]),
            updated_at=datetime.fromisoformat(event_data["updated_at"]),
            vector=vector,
        )
        upsert_issue(point)
        log.info("issue indexed", action=action, repo_id=repo_id, issue_number=issue_number)

    elif action == "closed":
        # Just update the state — the embedding doesn't change on close.
        # For now, re-upsert with updated state. The vector stays the same.
        text = prepare_text(title, body)
        vector = embed(text)
        point = IssuePoint(
            repo_id=repo_id,
            issue_number=issue_number,
            type="issue",
            state="closed",
            title=title,
            body_excerpt=body[:512],
            created_at=datetime.fromisoformat(event_data["created_at"]),
            updated_at=datetime.fromisoformat(event_data["updated_at"]),
            vector=vector,
        )
        upsert_issue(point)
        log.info("issue state updated", repo_id=repo_id, issue_number=issue_number)


async def run_worker():
    """Main worker loop — consume events forever."""
    ensure_collection()
    await ensure_consumer_groups()
    conn = get_connection(config.SQLITE_PATH)

    log.info("ingestion worker started", group=GROUP, consumer=CONSUMER)

    while True:
        try:
            events = await consume_events(GROUP, CONSUMER, count=1, block_ms=5000)

            for msg_id, event_data in events:
                delivery_id = event_data.get("delivery_id", "")

                if delivery_id and is_already_processed(conn, delivery_id, GROUP):
                    log.debug("skipping duplicate delivery", delivery_id=delivery_id)
                    await ack_event(GROUP, msg_id)
                    continue

                try:
                    await process_event(event_data, conn)
                    if delivery_id:
                        mark_processed(conn, delivery_id, GROUP, event_data.get("repo_id", ""))
                    await ack_event(GROUP, msg_id)
                except Exception as exc:
                    log.error(
                        "ingestion failed",
                        delivery_id=delivery_id,
                        error=str(exc),
                    )
                    await move_to_dead_letter(event_data, str(exc))
                    await ack_event(GROUP, msg_id)

        except Exception as exc:
            log.error("worker loop error", error=str(exc))
            await asyncio.sleep(5)  # Back off on unexpected errors


if __name__ == "__main__":
    asyncio.run(run_worker())
