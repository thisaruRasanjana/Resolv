"""
src/queue.py — Redis Streams producer/consumer.

Architecture: ARCHITECTURE.md §3.2
- One stream: resolv:events
- Two consumer groups: ingestion-group, triage-group
- Dead-letter stream: resolv:dead-letters (after MAX_RETRIES failures)

Redis Streams key concepts:
- XADD: append a message to a stream
- XREADGROUP: read messages as part of a consumer group (each message delivered to one consumer)
- XACK: acknowledge processing is complete
- Consumer groups ensure each event is processed exactly once per group
"""

import asyncio
import json

import redis.asyncio as aioredis

from src import config
from src.logging import get_logger

log = get_logger(__name__)

# Lazy singleton — created on first use
_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return a cached async Redis connection."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        log.debug("redis connected", url=config.REDIS_URL)
    return _redis


async def ensure_consumer_groups() -> None:
    """
    Create the consumer groups if they don't exist.

    Must be called once at startup before any consumer tries to read.
    MKSTREAM=True creates the stream if it doesn't exist yet.
    """
    r = await get_redis()
    for group in ("ingestion-group", "triage-group"):
        try:
            await r.xgroup_create(
                config.EVENTS_STREAM, group, id="0", mkstream=True
            )
            log.info("consumer group created", group=group)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                log.debug("consumer group already exists", group=group)
            else:
                raise


async def publish_event(event_data: dict) -> str:
    """
    Publish an event to the Redis stream.

    All values must be strings (Redis Streams requirement).
    We JSON-serialize the full event_data as a single field for simplicity.

    Returns the Redis stream message ID (e.g., "1234567890-0").
    """
    r = await get_redis()
    message_id = await r.xadd(
        config.EVENTS_STREAM,
        {"data": json.dumps(event_data)},
    )
    log.debug("event published", stream=config.EVENTS_STREAM, message_id=message_id)
    return message_id


async def consume_events(
    group: str,
    consumer: str,
    count: int = 1,
    block_ms: int = 5000,
) -> list[tuple[str, dict]]:
    """
    Read pending events from the stream for this consumer group.

    Returns a list of (message_id, event_data) tuples.
    If no messages are available, sleeps for block_ms to avoid tight polling.
    """
    r = await get_redis()
    
    # We do a non-blocking read to avoid redis-py socket timeout issues,
    # and handle the delay manually if no results are found.
    results = await r.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams={config.EVENTS_STREAM: ">"},  # ">" means only new messages
        count=count,
    )

    events = []
    if results:
        for stream_name, messages in results:
            for msg_id, fields in messages:
                event_data = json.loads(fields["data"])
                events.append((msg_id, event_data))

    if not events and block_ms > 0:
        await asyncio.sleep(block_ms / 1000.0)

    return events


async def ack_event(group: str, message_id: str) -> None:
    """Acknowledge that a message has been successfully processed."""
    r = await get_redis()
    await r.xack(config.EVENTS_STREAM, group, message_id)


async def move_to_dead_letter(event_data: dict, error: str) -> None:
    """
    Move a failed event to the dead-letter stream after MAX_RETRIES.

    Dead-letter events include the original data plus error details
    for manual review and replay.
    """
    r = await get_redis()
    dl_data = {
        "data": json.dumps(event_data),
        "error": error,
    }
    await r.xadd(config.DEAD_LETTER_STREAM, dl_data)
    log.error(
        "event moved to dead letter",
        delivery_id=event_data.get("delivery_id"),
        error=error,
    )
