# System Architecture — GitHub Issue Triage Bot

## 1. Overview

An AI bot that installs on open source GitHub repositories and automatically triages new issues: detecting likely duplicates, suggesting labels, and linking related PRs/discussions, by retrieving semantically similar past issues and having an LLM synthesize a comment.

**Non-goals:** this is not built to chase installs or viral adoption. The project's value comes from (1) the engineering itself, (2) a rigorous offline backtest against real historical GitHub data proving the retrieval actually works, and (3) a well-documented, genuinely open sourced repo. A handful of real installs is a bonus, not the success metric.

## 2. High-Level Architecture (live flow)

```
 GitHub repo (issues / PRs)
          |
          |  webhook: issue opened / edited / closed
          v
 +----------------------+       verify HMAC         +----------------------+
 |   Webhook Receiver    | -------------------------> |    Event Queue       |
 |  (GitHub App server)  |                             |  (Redis Streams)     |
 +----------------------+                             +----------+-----------+
                                                                   |
                                     ------------------------------+-----------------------------
                                     |                                                            |
                                     v                                                            v
                     +----------------------------+                            +----------------------------+
                     |  Ingestion & Index Worker   |                            |       Triage Worker         |
                     |  - chunk + embed issue      |                            |  - embed the new issue      |
                     |  - upsert/delete in Qdrant  |                            |  - search Qdrant (top-k)    |
                     +----------------------------+                            |  - build prompt w/ context  |
                                                                                 |  - call LLM (Ollama/vLLM)   |
                                                                                 +--------------+---------------+
                                                                                                |
                                                                                                v
                                                                                 +----------------------------+
                                                                                 |     Comment Poster          |
                                                                                 |  posts triage comment via   |
                                                                                 |      GitHub API             |
                                                                                 +----------------------------+
```

## 3. Component Breakdown

### 3.1 Webhook Receiver (GitHub App server)
- Verifies every payload's `X-Hub-Signature-256` header (HMAC-SHA256 against the app's webhook secret) before trusting anything in it.
- Does almost nothing else — reads the event, pushes it to the queue, returns `200` fast. GitHub has a short timeout on webhook responses and will retry on failure or timeout, so this service must never block on slow work (see §6 on reliability).
- *Open decision:* Node (Octokit/Probot) vs. Python (FastAPI) — both are reasonable, worth deciding with a mentor based on which ecosystem you want more depth in.

### 3.2 Event Queue (Redis Streams)
- Decouples "GitHub sent us something" from "we finished processing it." This is what makes a burst of 50 issues after a release survivable.
- Two consumer groups read the same stream independently: one for ingestion/indexing, one for triage — so indexing a new issue and triaging it can happen in parallel rather than one blocking the other.

### 3.3 Ingestion & Index Worker
- Chunks and embeds issue/PR text, upserts into Qdrant.
- On an `edited` event, re-embeds and overwrites the existing point rather than inserting a duplicate.
- On a `closed` event, updates the `state` field rather than deleting — closed issues are still useful as duplicate-detection targets, just down-ranked or filtered depending on the query.

### 3.4 Vector Store (Qdrant)
- One collection, every point filtered by `repo_id` at query time — this is what gives you multi-tenant isolation without running separate databases per repo.
- See §4 for the full payload schema.

### 3.5 Triage Worker
- Embeds the incoming issue, runs a filtered similarity search (`repo_id` match, optionally `state=open` weighted higher), and assembles the top-k results into context for the LLM.
- Constructs a prompt asking the LLM to produce: a duplicate verdict + confidence, suggested labels, and any related PR/discussion links found in the retrieved context.

### 3.6 LLM Serving Layer
- Self-hosted via Ollama or vLLM to start — this keeps cost at zero during development and gives you full control over latency, which matters because a triage comment posted 10 minutes late is useless.

### 3.7 Comment Poster
- Formats the LLM's structured output into a readable GitHub comment and posts it via the GitHub API, authenticated as the installation (see §7).

### 3.8 Config & State Store (new in this pass — needed once you think through reliability)
A lightweight relational store (SQLite to start, Postgres if you want the more production-realistic version) holding:
- which repos have the app installed and their settings
- rate-limit token bucket state per repo
- a record of processed webhook delivery IDs (idempotency — see §6)

## 4. Data Model

**Qdrant point payload (one per issue/PR):**
```
id            UUID
repo_id       string   ("owner/repo")
issue_number  int
type          "issue" | "pull_request"
state         "open" | "closed"
title         string
body_excerpt  string    (trimmed, for display in the posted comment)
created_at    timestamp
updated_at    timestamp
vector        embedding
```

**Relational store tables:**
```
installations(repo_id PK, installed_at, is_active, settings_json)
rate_limit_state(repo_id PK, tokens_remaining, last_refill_at)
processed_deliveries(delivery_id PK, repo_id, processed_at)   -- idempotency
backtest_results(repo_id, run_at, precision, recall, f1, notes)
```

## 5. Event Flows

**New issue opened (live):** webhook fires → signature verified → event pushed to queue → ingestion worker embeds + indexes it → triage worker (in parallel) embeds it, searches the index for *pre-existing* similar issues, calls the LLM, posts the comment.

**Issue edited or closed:** webhook fires → ingestion worker re-embeds (edited) or updates state (closed) → no new triage comment is triggered, this only keeps the index accurate for future issues.

**Offline backtest (Phase 1, no webhooks needed):**
```
 GitHub REST/GraphQL API
          |
          |  paginated fetch, respects rate limits (ETags / conditional requests)
          v
 Historical issue dataset, sorted by created_at
          |
          v
 Replay engine (for each issue, in chronological order):
   1. Only search issues with created_at < this issue's created_at (no future leakage)
   2. Run the SAME retrieval logic the live Triage Worker uses
   3. Compare predicted duplicates against actual "duplicate of #N" labels/links
          |
          v
 Metrics: precision / recall / F1 per repo --> stored in backtest_results, charted
```
This is the headline result for the whole project and requires zero live installs — it's what actually proves the retrieval approach works.

## 6. Reliability & Idempotency

- **GitHub retries webhook deliveries** on non-2xx responses or timeouts. Without protection, this means the same issue could get triaged (and commented on) twice. Fix: every webhook payload includes an `X-GitHub-Delivery` header with a unique ID — check it against `processed_deliveries` before processing, and skip if already seen.
- **Dead-letter handling:** if an event fails processing repeatedly (LLM timeout, transient API error), move it to a separate dead-letter stream after N retries rather than looping forever or silently dropping it — surface these for manual review.
- **Retry with backoff** for transient failures calling the LLM or the GitHub API.

## 7. Security

- **GitHub App auth:** the app has an App ID and a private key (RSA). To act on an installation, you sign a short-lived JWT (~10 min) with the private key, then exchange it for an installation access token (~1 hour) scoped to that specific repo. Never use a long-lived personal access token.
- **Webhook verification:** compute HMAC-SHA256 of the raw payload using the app's webhook secret, compare against `X-Hub-Signature-256`, reject on mismatch — do this before parsing or trusting anything in the payload.
- **Secrets management:** private key and webhook secret live in Kubernetes Secrets, never in code or committed config.
- **Least privilege:** request only the permissions actually needed (`issues: write`, `pull_requests: read`, `contents: read`) — this is also what a maintainer checks before installing anything on their repo.

## 8. Observability

**Metrics (Prometheus):**
- `triage_stage_duration_seconds{stage="embed|retrieve|generate|post"}` (histogram) — per-stage latency
- `queue_depth` (gauge) — what KEDA scales on
- `webhook_events_received_total`, `webhook_events_deduped_total`
- `triage_comments_posted_total`, `llm_call_errors_total`

**Tracing (OpenTelemetry):** one trace per issue event, with a span per stage, so you can pull up a single issue and see exactly how long each hop took — useful for debugging and genuinely good material for an interview walkthrough.

**Logging:** structured JSON logs correlated by `repo_id`, `issue_number`, and `delivery_id`.

## 9. Deployment Architecture

```
+-------------------------------- Kubernetes cluster ---------------------------------+
|                                                                                       |
|   Ingress (TLS) ---> webhook-receiver (Deployment, 2+ replicas)                      |
|                                                                                       |
|   redis (queue)        qdrant (vector store)        postgres (installs + state)      |
|                                                                                       |
|   ingestion-worker (Deployment)     triage-worker (Deployment, KEDA ScaledObject:     |
|                                        scales replicas on redis stream length)        |
|                                                                                       |
|   prometheus + grafana (metrics)        otel-collector (traces)                      |
|                                                                                       |
+---------------------------------------------------------------------------------------+
```
k3s or kind for local development — no cloud bill needed until/unless you want a public demo instance.

## 10. Multi-Tenancy & Rate Limiting

- Isolation: every Qdrant query is filtered by `repo_id` — one collection, not one database per tenant.
- Fairness: a token-bucket rate limiter per `repo_id` in the config store caps how many LLM calls a single repo can trigger per minute, so one very active repo can't starve everyone else sharing your LLM budget. When a repo exceeds its bucket, queue the event for later processing rather than dropping it outright.

## 11. Tech Stack Summary

| Layer | Choice | Status |
|---|---|---|
| Webhook receiver | Octokit/Probot (Node) or FastAPI (Python) | open decision |
| Queue | Redis Streams (Kafka as a later stretch) | decided |
| Vector store | Qdrant | decided |
| Embedding model | — | open decision |
| LLM serving | Ollama or vLLM, self-hosted | decided |
| Config/state store | SQLite → Postgres | decided (SQLite first) |
| Orchestration | Kubernetes (k3s/kind for dev) | decided |
| Autoscaling | KEDA, scaling on queue depth | decided |
| Observability | Prometheus + Grafana + OpenTelemetry | decided |

## 12. Phase-to-Component Mapping

1. **Backtest harness + single-repo MVP** — §5 offline flow, §3.4/3.5 retrieval logic, no webhook receiver or queue yet
2. **Live webhooks + queue** — §3.1, §3.2, §6 idempotency
3. **Kubernetes + autoscaling + observability** — §9, §3.8, §8
4. **Multi-repo isolation + rate limiting** — §10
5. **(Stretch) MLOps layer** — experiment tracking + eval-gated CI on top of the backtest harness from Phase 1

## 13. Open Decisions to Work Through With a Mentor

- Webhook receiver language/framework (Node vs. Python)
- Embedding model choice (latency/cost/quality tradeoff)
- Whether v1 indexes PR bodies and comments, or issues only, to keep initial scope tight
