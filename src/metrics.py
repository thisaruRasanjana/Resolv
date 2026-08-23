"""
src/metrics.py — Prometheus metrics instrumentation.

Metrics defined:
- triage_stage_duration_seconds (Histogram, labels: stage)
- queue_depth (Gauge)
- webhook_events_received_total (Counter)
- webhook_events_deduped_total (Counter)
- triage_comments_posted_total (Counter)
- llm_call_errors_total (Counter)
"""

from prometheus_client import Counter, Gauge, Histogram

triage_stage_duration = Histogram(
    "triage_stage_duration_seconds",
    "Duration of each triage stage",
    ["stage"],  # embed, retrieve, generate, post
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120],
)

queue_depth = Gauge("queue_depth", "Pending messages in event queue")
webhook_events_received = Counter("webhook_events_received_total", "Total webhook events received")
webhook_events_deduped = Counter("webhook_events_deduped_total", "Total events skipped as duplicates")
triage_comments_posted = Counter("triage_comments_posted_total", "Total triage comments posted")
llm_call_errors = Counter("llm_call_errors_total", "Total LLM call errors")
