"""
src/webhook.py — FastAPI webhook receiver.

Verifies GitHub webhook signatures (HMAC-SHA256), extracts the event,
and pushes it to Redis Streams for async processing by workers.

Security: ARCHITECTURE.md §7 — reject any payload that fails HMAC verification.
Performance: ARCHITECTURE.md §3.1 — return 200 immediately, never block on slow work.
"""

import hashlib
import hmac
import json

from fastapi import FastAPI, Request, Response

from src import config
from src.logging import get_logger
from src.queue import publish_event
from src.metrics import webhook_events_received
from src.tracing import tracer
from prometheus_client import make_asgi_app
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

log = get_logger(__name__)

app = FastAPI(title="Resolv Webhook Receiver", version="0.1.0")

# Mount Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


def _verify_signature(payload_body: bytes, signature_header: str | None) -> bool:
    """
    Verify the X-Hub-Signature-256 header against the raw payload body.

    IMPORTANT: Use hmac.compare_digest() for constant-time comparison
    to prevent timing attacks. Never use == for signature comparison.

    Returns True if valid, False if invalid or missing.
    """
    if not signature_header:
        return False

    secret = config.get_github_webhook_secret().encode("utf-8")
    expected = "sha256=" + hmac.new(secret, payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Receive a GitHub webhook event.

    Flow:
    1. Read raw body bytes (must be raw for HMAC — not parsed JSON).
    2. Verify HMAC-SHA256 signature.
    3. Check event type and action.
    4. Push to Redis stream.
    5. Return 200.
    """
    webhook_events_received.inc()
    
    with tracer.start_as_current_span("webhook_receive"):
        body = await request.body()
    
        # Step 1: Verify signature — MUST happen before parsing JSON
        signature = request.headers.get("X-Hub-Signature-256")
        if not _verify_signature(body, signature):
            log.warning("webhook signature verification failed")
            return Response(status_code=401, content="Invalid signature")
    
        # Step 2: Parse event metadata from headers
        event_type = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
    
        # Step 3: Parse payload
        payload = json.loads(body)
        action = payload.get("action", "")

        # Step 4: Handle specific event types
        if event_type in ("installation", "installation_repositories"):
            event_data = {
                "delivery_id": delivery_id,
                "event_type": event_type,
                "action": action,
                "installation_id": str(payload.get("installation", {}).get("id", "")),
                # Redis requires string values, so we JSON serialize the lists of repos
                "repositories_added": json.dumps(payload.get("repositories_added", [])),
                "repositories_removed": json.dumps(payload.get("repositories_removed", [])),
                "repositories": json.dumps(payload.get("repositories", [])), # Present on 'created'
            }

        elif event_type == "issues":
            # We only care about: opened, edited, closed
            if action not in ("opened", "edited", "closed"):
                log.debug("ignoring issue action", action=action)
                return Response(status_code=200, content="OK (ignored)")
        
            issue = payload["issue"]
            repo = payload["repository"]
            repo_id = repo["full_name"]  # "owner/repo"
        
            event_data = {
                "delivery_id": delivery_id,
                "event_type": event_type,
                "action": action,
                "repo_id": repo_id,
                "issue_number": issue["number"],
                "title": issue["title"],
                "body": issue.get("body") or "",
                "state": issue["state"],
                "created_at": issue["created_at"],
                "updated_at": issue["updated_at"],
                "installation_id": str(payload.get("installation", {}).get("id", "")),
            }
            
        else:
            log.debug("ignoring unhandled event", event_type=event_type)
            return Response(status_code=200, content="OK (ignored)")
    
    with tracer.start_as_current_span("enqueue"):
        await publish_event(event_data)
    
    if event_type == "issues":
        log.info(
            "webhook processed",
            delivery_id=delivery_id,
            action=action,
            repo_id=event_data["repo_id"],
            issue_number=event_data["issue_number"],
        )
    else:
        log.info(
            "webhook processed",
            delivery_id=delivery_id,
            action=action,
            event_type=event_type,
        )
    return Response(status_code=200, content="OK")


@app.get("/health")
async def health():
    """Health check endpoint for liveness probes."""
    return {"status": "ok"}


# Auto-instrument FastAPI routes with OpenTelemetry
FastAPIInstrumentor().instrument_app(app)
