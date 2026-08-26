"""
src/workers/triage.py — Triage worker.

Consumes events from Redis stream (triage-group). Only acts on 'opened'
events. Runs the full triage pipeline and posts a comment. ARCHITECTURE.md §3.5.
"""

import asyncio

from src import config
from src.db import get_connection
from src.github_client import format_triage_comment, post_comment
from src.idempotency import is_already_processed, mark_processed
from src.indexer import ensure_collection
from src.logging import get_logger
from src.queue import ack_event, consume_events, ensure_consumer_groups, move_to_dead_letter
from src.triage import triage_issue

log = get_logger(__name__)

GROUP = "triage-group"
CONSUMER = "triage-worker-1"


from src.metrics import triage_comments_posted
from src.tracing import tracer

from src.rate_limiter import acquire_token

class RateLimitExceeded(Exception):
    """Raised when a repo has exhausted its token bucket."""
    pass

async def process_event(event_data: dict, conn) -> None:
    """Process a single triage event."""
    event_type = event_data.get("event_type", "issues")
    if event_type != "issues":
        return

    action = event_data["action"]

    # Only triage newly opened issues
    if action != "opened":
        log.debug("skipping non-opened action for triage", action=action)
        return

    repo_id = event_data["repo_id"]

    # 1. Enforce rate limit
    if not acquire_token(conn, repo_id):
        raise RateLimitExceeded(f"rate limit exceeded for {repo_id}")


    issue_number = event_data["issue_number"]
    title = event_data["title"]
    body = event_data.get("body", "")
    installation_id = event_data.get("installation_id", "")

    # Run triage (this calls embed → search → LLM → parse)
    result = triage_issue(
        repo_id=repo_id,
        issue_number=issue_number,
        title=title,
        body=body,
    )

    # Format and post comment
    comment_body = format_triage_comment(result)

    if installation_id:
        with tracer.start_as_current_span("post_comment"):
            post_comment(
                installation_id=installation_id,
                repo_id=repo_id,
                issue_number=issue_number,
                body=comment_body,
            )
            triage_comments_posted.inc()
    else:
        log.warning(
            "no installation_id, cannot post comment",
            repo_id=repo_id,
            issue_number=issue_number,
        )

    log.info(
        "triage complete",
        repo_id=repo_id,
        issue_number=issue_number,
        is_duplicate=result.is_duplicate,
        confidence=result.confidence,
    )


from src.queue import publish_event

async def run_worker():
    """Main worker loop — consume events forever."""
    ensure_collection()
    await ensure_consumer_groups()
    conn = get_connection(config.SQLITE_PATH)

    log.info("triage worker started", group=GROUP, consumer=CONSUMER)

    retry_counts: dict[str, int] = {}  # delivery_id → attempt count

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
                        mark_processed(conn, delivery_id, GROUP, event_data["repo_id"])
                    await ack_event(GROUP, msg_id)
                    # Reset retry count on success
                    retry_counts.pop(delivery_id, None)

                except RateLimitExceeded as exc:
                    log.warning(
                        "rate limit exceeded, deferring event",
                        repo_id=event_data.get("repo_id"),
                        delivery_id=delivery_id
                    )
                    # Acknowledge current message and push a fresh copy to the back of the queue
                    await ack_event(GROUP, msg_id)
                    await publish_event(event_data)
                    await asyncio.sleep(2)  # brief pause before pulling next message

                except Exception as exc:
                    attempts = retry_counts.get(delivery_id, 0) + 1
                    retry_counts[delivery_id] = attempts

                    if attempts >= config.MAX_RETRIES:
                        log.error(
                            "max retries exceeded, moving to dead letter",
                            delivery_id=delivery_id,
                            attempts=attempts,
                            error=str(exc),
                        )
                        await move_to_dead_letter(event_data, str(exc))
                        await ack_event(GROUP, msg_id)
                        retry_counts.pop(delivery_id, None)
                    else:
                        log.warning(
                            "triage failed, will retry",
                            delivery_id=delivery_id,
                            attempt=attempts,
                            error=str(exc),
                        )
                        # Re-publish to the back of the queue for simple retry semantics
                        await ack_event(GROUP, msg_id)
                        await publish_event(event_data)
                        await asyncio.sleep(2 ** attempts)

        except Exception as exc:
            log.error("worker loop error", error=str(exc))
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_worker())
