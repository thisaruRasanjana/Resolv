# Resolv — GitHub Issue Triage Bot

An AI bot that triages new GitHub issues: detecting duplicates, suggesting labels, and linking related PRs, by retrieving semantically similar past issues and having an LLM synthesize a comment.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full technical spec.

---

## Current status: Phase 3 — Kubernetes + Observability

Phase 1 (Offline Backtest), Phase 2 (Live Webhooks + Redis Queue), and Phase 3 (Containerisation + Kubernetes + Observability) are fully implemented. 

The bot is now a production-ready cloud-native service. It uses an ultra-lightweight Docker image (~105 MB) by leveraging Ollama for both LLM generation and vector embeddings, entirely eliminating the PyTorch dependency. It includes Kubernetes manifests for local `kind` deployment, KEDA autoscaling, OpenTelemetry distributed tracing, and Prometheus metrics.

---

## Tech stack

| Layer | Choice |
|---|---|
| Webhook Receiver | FastAPI |
| Queue / Broker | Redis Streams (`redis-py` async) |
| Embeddings | Ollama (`llama3.1:8b` via `/api/embed`, 4096-dim) |
| Vector store | Qdrant |
| LLM | Ollama (`llama3.1:8b`, self-hosted, zero cost) |
| State store | SQLite (Idempotency and rate limiting) |
| Deployment | Kubernetes (manifests + `kind`), Docker |
| Observability | Prometheus (`/metrics`), OpenTelemetry (OTLP) |
| Language | Python 3.11+ |

---

## Quickstart (Phase 3 — Kubernetes Deployment)

### Prerequisites
- Python 3.11+
- Docker Desktop running
- Ollama running natively on host with `llama3.1:8b` pulled (`ollama pull llama3.1:8b`)
- `kind`, `kubectl`, and `helm` installed
- A GitHub App configured

### 1. Clone and build

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

### 3. Deploy locally to Kind

```bash
# This builds the lightweight Docker image, spins up a local Kubernetes cluster using kind,
# and deploys Qdrant, Redis, the Webhook receiver, and the workers.
./scripts/deploy-local.sh
```

### 4. Connect Webhook
Use `smee.io` to tunnel GitHub webhooks to your local cluster:
```bash
smee --url https://smee.io/YOUR_URL --path /webhook --port 30080
```

When you open a new issue in a repository where the GitHub App is installed, the webhook is received, pushed to Redis, and processed asynchronously by the Kubernetes worker pods.

*(Note: You can still run the bot natively using `make webhook-server` and `docker compose up -d` if you do not want to use Kubernetes).*

---

## Project structure

```
Resolv/
├── ARCHITECTURE.md       Full technical spec
├── k8s/                  Kubernetes manifests (Deployments, StatefulSets, KEDA, Monitoring)
├── scripts/
│   └── deploy-local.sh   One-click kind cluster deployment script
├── src/
│   ├── config.py         Centralised config (env vars)
│   ├── embedder.py       Ollama HTTP API wrapper for embeddings
│   ├── indexer.py        Qdrant client (upsert, search, delete)
│   ├── triage.py         Embed → retrieve → LLM → parse
│   ├── webhook.py        FastAPI GitHub webhook receiver (HMAC verification)
│   ├── queue.py          Redis Streams async producer/consumer
│   ├── github_client.py  GitHub App JWT auth & comment API
│   ├── idempotency.py    Webhook delivery deduplication (SQLite)
│   ├── metrics.py        Prometheus counters and histograms
│   ├── tracing.py        OpenTelemetry tracing configuration
│   └── workers/          
│       ├── ingestion.py  Worker: consume events → index in Qdrant
│       └── triage.py     Worker: consume events → run LLM → post comment
├── backtest/
│   ├── fetch.py          GitHub API fetcher + ground-truth extractor
│   └── replay.py         Chronological replay + P/R/F1 evaluation
└── tests/
    ├── test_embedder.py  Embedding pure function tests
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
| 3 — Kubernetes + observability | **Done** | k8s/kind, KEDA autoscaling, Prometheus/Grafana/OTel |
| 4 — Multi-repo + rate limiting | 🔜 Next | Per-repo filtering, token-bucket rate limiter |
| 5 — MLOps | 🔜 Stretch | MLflow experiment tracking, eval-gated CI |
