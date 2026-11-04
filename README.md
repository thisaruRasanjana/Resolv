# Resolv — AI-Powered GitHub Issue Triage Bot

An AI bot that installs on GitHub repositories and automatically triages new issues — detecting duplicates, suggesting labels, and linking related issues — by retrieving semantically similar past issues and having an LLM synthesize a structured comment.

**Evaluated against 3,000 real [`microsoft/vscode`](https://github.com/microsoft/vscode) issues: 50% Recall@10 on duplicate detection using semantic vector search alone, measured with a chronological no-leakage backtest.**

Fully self-hosted. Zero API costs. Runs on a single machine with [Ollama](https://ollama.com).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full technical specification.

---

## How it works

```
  GitHub Issue Opened
         │
         ▼
  ┌──────────────┐    HMAC verify   ┌─────────────────┐
  │   Webhook    │ ───────────────▶ │  Redis Streams  │
  │   Receiver   │                  │  (event queue)  │
  └──────────────┘                  └────────┬────────┘
                                             │
                          ┌──────────────────┤
                          ▼                  ▼
                  ┌──────────────┐   ┌──────────────┐
                  │  Ingestion   │   │   Triage     │
                  │  Worker      │   │   Worker     │
                  │  embed →     │   │  embed →     │
                  │  upsert      │   │  search →    │
                  │  Qdrant      │   │  LLM →       │
                  └──────────────┘   │  comment     │
                                     └──────────────┘
```

1. **Webhook Receiver** (FastAPI) verifies the GitHub HMAC signature and pushes the event to Redis Streams.
2. **Ingestion Worker** embeds the issue text via Ollama and upserts it into Qdrant (vector store).
3. **Triage Worker** embeds the new issue, retrieves the top-k most similar past issues from Qdrant, builds a prompt with that context, calls the LLM, parses the structured JSON response, and posts a comment back to GitHub.

All three components run as independent processes, connected only through Redis. This lets them scale independently via Kubernetes (KEDA autoscaling based on Redis stream lag).

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Webhook Receiver | FastAPI + Uvicorn |
| Event Queue | Redis Streams (async `redis-py`) |
| Embeddings + LLM | Ollama (`llama3.1:8b`, self-hosted, zero cost) |
| Vector Store | Qdrant (cosine similarity, multi-tenant filtering) |
| State Store | SQLite (idempotency, rate limiting, installations) |
| Rate Limiting | Token bucket algorithm (per-repo, SQLite-backed) |
| Deployment | Docker, Kubernetes (kind), KEDA autoscaling |
| Observability | Prometheus metrics, OpenTelemetry tracing, Grafana |
| Experiment Tracking | MLflow (local file backend) |
| CI / Eval Gate | GitHub Actions (backtest-gated PRs) |

---

## Quickstart

### Prerequisites

- Python 3.11+
- Docker Desktop running
- Ollama running locally with `llama3.1:8b` pulled (`ollama pull llama3.1:8b`)
- A GitHub App configured (see [ARCHITECTURE.md](ARCHITECTURE.md) for setup)

### 1. Install

```bash
git clone https://github.com/thisaruRasanjana/Resolv.git
cd Resolv
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env: set GITHUB_TOKEN, GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH, GITHUB_WEBHOOK_SECRET
```

### 3. Start infrastructure

```bash
docker compose up -d   # Qdrant + Redis
```

### 4. Run the bot (native)

```bash
# Terminal 1: Webhook receiver
make webhook-server

# Terminal 2: Ingestion worker
make ingestion-worker

# Terminal 3: Triage worker
make triage-worker

# Terminal 4: Tunnel GitHub webhooks to localhost
make smee SMEE_URL=https://smee.io/YOUR_URL
```

### 5. Run the bot (Kubernetes)

```bash
# One-click: builds Docker image, creates kind cluster, deploys everything
./scripts/deploy-local.sh
```

---

## Offline backtest

The backtest harness evaluates the retrieval pipeline against real historical GitHub data before deploying live. Issues are replayed in strict chronological order — when evaluating issue N, the index contains only issues 1…N-1, guaranteeing no future data leakage.

```bash
# Fetch 3000 issues from microsoft/vscode (extracts ground-truth duplicates)
make fetch REPO=microsoft/vscode LIMIT=3000

# Fast retrieval eval: Recall@10 — no LLM needed, ~2 min for 3000 issues
make backtest-retrieval REPO=microsoft/vscode LIMIT=3000

# Full pipeline eval: Precision / Recall / F1 — requires Ollama LLM running
make backtest REPO=microsoft/vscode LIMIT=3000
```

**Measured result on `microsoft/vscode` (3,000 issues, 56 ground-truth duplicate pairs):**

| Metric | Value |
|---|---|
| Recall@10 | **50.0%** |
| Embedding model | `nomic-embed-text` (768-dim, via Ollama) |
| Evaluation method | Chronological replay — no future leakage |

Recall@10 = the fraction of known duplicate pairs where the original issue appears in the top-10 vector search results.

Results are tracked in MLflow. To browse experiments:

```bash
mlflow ui   # opens http://localhost:5000
```

---

## Testing

```bash
make test    # 35 unit + integration tests
make lint    # ruff linter
```

---

## Project structure

```
Resolv/
├── ARCHITECTURE.md           Full technical specification
├── Dockerfile                Lightweight production image (~105 MB)
├── docker-compose.yml        Local dev: Qdrant + Redis
├── Makefile                  Developer commands
│
├── src/
│   ├── config.py             Centralised config (env vars, single source of truth)
│   ├── embedder.py           Ollama embedding API wrapper
│   ├── indexer.py            Qdrant client (upsert, search, multi-tenant isolation)
│   ├── triage.py             Core pipeline: embed → retrieve → LLM → parse
│   ├── webhook.py            FastAPI receiver (HMAC-SHA256 verification)
│   ├── queue.py              Redis Streams async producer/consumer
│   ├── github_client.py      GitHub App JWT auth & comment posting
│   ├── db.py                 SQLite schema & connection management
│   ├── idempotency.py        Webhook delivery deduplication
│   ├── rate_limiter.py       Per-repo token bucket rate limiter
│   ├── metrics.py            Prometheus counters & histograms
│   ├── tracing.py            OpenTelemetry OTLP configuration
│   └── workers/
│       ├── ingestion.py      Consume events → embed → upsert Qdrant + sync installations
│       └── triage.py         Consume events → triage pipeline → post GitHub comment
│
├── backtest/
│   ├── fetch.py              GitHub API fetcher + ground-truth extractor
│   └── replay.py             Chronological replay engine + MLflow tracking
│
├── k8s/                      Kubernetes manifests
│   ├── qdrant.yaml           Qdrant StatefulSet
│   ├── redis.yaml            Redis Deployment
│   ├── webhook.yaml          Webhook receiver Deployment + Service
│   ├── workers.yaml          Ingestion + Triage worker Deployments
│   ├── keda.yaml             KEDA ScaledObjects (autoscale on Redis lag)
│   └── monitoring/           Prometheus, Grafana, OTel Collector
│
├── scripts/
│   └── deploy-local.sh       One-click kind cluster deployment
│
├── .github/workflows/
│   └── eval-gate.yml         CI: evaluation-gated PR checks
│
└── tests/
    ├── test_embedder.py      Embedding function tests
    ├── test_triage.py        Prompt construction, response parsing, mocked pipeline
    ├── test_indexer.py       Qdrant upsert / search / multi-tenant isolation
    ├── test_webhook.py       FastAPI routes & HMAC signature verification
    ├── test_queue.py         Redis Streams operations
    ├── test_rate_limiter.py  Token bucket math, exhaustion, refill, cap
    └── test_idempotency.py   Delivery deduplication logic
```

---

## Key design decisions

- **Single Ollama model for both embeddings and generation** — eliminates the need for PyTorch/sentence-transformers, keeping the Docker image at ~105 MB and RAM usage low.
- **Redis Streams (not Kafka/RabbitMQ)** — lightweight, already battle-tested, and consumer groups give us exactly-once processing semantics with manual acknowledgement.
- **SQLite (not Postgres)** — perfectly sufficient for idempotency tracking, rate limiting state, and installation records at single-instance scale. `BEGIN IMMEDIATE` transactions provide safe concurrency between workers.
- **Deterministic UUIDs for Qdrant points** — `uuid5(repo_id/issue_number)` means upserts are naturally idempotent without any coordination.
- **Multi-tenant via filter, not separate collections** — all repos share one Qdrant collection; `repo_id` filter on every query prevents cross-tenant data leakage.
- **Token bucket rate limiter** — prevents a noisy repository from starving other tenants. Deferred events are re-published to the back of the queue rather than dropped.
- **MLflow for experiment tracking** — every backtest run logs hyperparameters and metrics, making it easy to compare model/prompt changes quantitatively.
- **Eval-gated CI** — PRs that modify the AI pipeline are automatically backtested; the pipeline fails if recall drops below the baseline threshold.

---

## License

See [LICENSE](LICENSE).
