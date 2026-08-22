# Resolv — GitHub Issue Triage Bot

An AI bot that triages new GitHub issues: detecting duplicates, suggesting labels, and linking related PRs, by retrieving semantically similar past issues and having an LLM synthesize a comment.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full technical spec.

---

## Current status: Phase 2 — Live Webhooks + Queue

Phase 1 (Offline Backtest) and Phase 2 (Live Webhooks + Redis Queue) are fully implemented. The bot can now run against live GitHub repositories, listening for webhook events via a FastAPI server, queuing them in Redis Streams, and using background workers (Ingestion & Triage) to asynchronously embed issues and post comments on GitHub.

---

## Tech stack

| Layer | Choice |
|---|---|
| Webhook Receiver | FastAPI |
| Queue / Broker | Redis Streams (`redis-py` async) |
| Embeddings | `all-MiniLM-L6-v2` (sentence-transformers, 384-dim, runs on Apple MPS) |
| Vector store | Qdrant |
| LLM | Ollama (`llama3.1:8b`, self-hosted, zero cost) |
| State store | SQLite (Idempotency and rate limiting) |
| Language | Python 3.11+ |

---

## Quickstart (Phase 2 — Live Bot)

### Prerequisites
- Python 3.11+
- Docker Desktop running
- Ollama running with `llama3.1:8b` pulled (`ollama pull llama3.1:8b`)
- A GitHub App configured (see `docs/github-app-setup.md` or Phase 2 docs)
- `smee-client` installed (`npm install -g smee-client`) for local webhook proxy

### 1. Clone and install

```bash
git clone https://github.com/yourname/resolv
cd resolv
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set all GitHub App credentials (GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH, GITHUB_WEBHOOK_SECRET)
```

### 3. Start Infrastructure (Qdrant & Redis)

```bash
docker compose up -d
```

### 4. Run the Pipeline (Requires 4 Terminals)

Terminal 1: Start the FastAPI webhook receiver
```bash
make webhook-server
```

Terminal 2: Start the smee.io webhook proxy (replace with your URL)
```bash
make smee SMEE_URL=https://smee.io/YOUR_URL
```

Terminal 3: Start the Ingestion worker (embeds and upserts new issues)
```bash
make ingestion-worker
```

Terminal 4: Start the Triage worker (retrieves duplicates and posts comments)
```bash
make triage-worker
```

When you open a new issue in a repository where the GitHub App is installed, the webhook is received, pushed to Redis, and processed asynchronously by the workers.

---

## Project structure

```
Resolv/
├── ARCHITECTURE.md       Full technical spec
├── src/
│   ├── config.py         Centralised config (env vars)
│   ├── embedder.py       Sentence-transformers wrapper (MPS-aware)
│   ├── indexer.py        Qdrant client (upsert, search, delete)
│   ├── triage.py         Embed → retrieve → LLM → parse
│   ├── webhook.py        FastAPI GitHub webhook receiver (HMAC verification)
│   ├── queue.py          Redis Streams async producer/consumer
│   ├── github_client.py  GitHub App JWT auth & comment API
│   ├── idempotency.py    Webhook delivery deduplication (SQLite)
│   ├── db.py             SQLite state store
│   ├── logging.py        Structured JSON logging (structlog)
│   └── workers/          
│       ├── ingestion.py  Worker: consume events → index in Qdrant
│       └── triage.py     Worker: consume events → run LLM → post comment
├── backtest/
│   ├── fetch.py          GitHub API fetcher + ground-truth extractor
│   └── replay.py         Chronological replay + P/R/F1 evaluation
└── tests/
    ├── test_embedder.py  Embedding correctness + similarity ranking
    ├── test_triage.py    Prompt construction + response parsing + mocked pipeline
    ├── test_indexer.py   Qdrant upsert / search / multi-tenant isolation
    ├── test_webhook.py   FastAPI routes & HMAC signature verification
    └── test_queue.py     Redis Streams operations
```

---

## Phases

| Phase | Status | Description |
|---|---|---|
| 1 — Backtest harness | **Done** | Offline P/R/F1 evaluation against historical data |
| 2 — Live webhooks + queue | **Done** | GitHub App + Redis Streams + comment poster |
| 3 — Kubernetes + observability | 🔜 Next | k3s/kind, KEDA autoscaling, Prometheus/Grafana/OTel |
| 4 — Multi-repo + rate limiting | 🔜 Stretch | Per-repo filtering, token-bucket rate limiter |
| 5 — MLOps | 🔜 Stretch | MLflow experiment tracking, eval-gated CI |
