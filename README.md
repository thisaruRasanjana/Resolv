# Resolv — AI-Powered GitHub Issue Triage Bot

An AI bot that installs on GitHub repositories and automatically triages new issues — detecting duplicates, suggesting labels, and linking related issues — by retrieving semantically similar past issues and having an LLM synthesize a structured comment.

**Evaluated against 3,000 real [`microsoft/vscode`](https://github.com/microsoft/vscode) issues: 50% Recall@10 on duplicate detection using semantic vector search alone, measured with a chronological no-leakage backtest.**

Fully self-hosted or cloud-deployed. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full technical specification.

---

## How it works

```
  GitHub Issue Opened
         │
         ▼
  ┌──────────────┐    HMAC verify    ┌─────────────────┐
  │   Webhook     │ ───────────────▶ │  Redis Streams   │
  │   Receiver    │                  │  (event queue)   │
  └──────────────┘                  └────────┬─────────┘
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

| Layer | Local | Cloud |
|---|---|---|
| Language | Python 3.11+ | Python 3.11+ |
| Webhook Receiver | FastAPI + Uvicorn | FastAPI + Uvicorn (Azure Container Apps) |
| Event Queue | Redis Streams (Docker) | Upstash Redis Streams (TLS) |
| Embeddings | Ollama `nomic-embed-text` | Gemini `gemini-embedding-001` (768-dim) |
| LLM | Ollama `llama3.1:8b` | Groq `openai/gpt-oss-20b` |
| Vector Store | Qdrant (Docker) | Qdrant Cloud Free Tier |
| State Store | SQLite | SQLite (ephemeral container volume) |
| Rate Limiting | Token bucket (SQLite-backed) | Token bucket (SQLite-backed) |
| Deployment | Docker Compose / Kubernetes (kind) | Azure Container Apps (`minReplicas: 0`) |
| Observability | Prometheus, OpenTelemetry, Grafana | Azure Container Apps log streaming |
| Experiment Tracking | MLflow (local) | MLflow (local) |
| CI / Eval Gate | GitHub Actions | GitHub Actions |

---

## Live Cloud Deployment (Azure Container Apps)

The bot is deployed to production on Azure Container Apps in the **Malaysia West** region, verified against the [`thisaruRasanjana/bot-testing-sandbox`](https://github.com/thisaruRasanjana/bot-testing-sandbox) repository.

**GitHub App:** `resolv-triage-bot-thisaru` (App ID: `4679516`)

| Endpoint | URL |
|---|---|
| Webhook | `https://resolv-webhook.yellowsand-62e3a857.malaysiawest.azurecontainerapps.io/webhook` |
| Health check | `https://resolv-webhook.yellowsand-62e3a857.malaysiawest.azurecontainerapps.io/health` |

### Cloud architecture

Three Azure Container Apps (`resolv-webhook`, `resolv-triage`, `resolv-ingestion`) with `minReplicas: 0` — the entire deployment scales to zero when idle, running at **\$0.00/mo** in steady state.

```
  GitHub Webhook
       │
       ▼
  resolv-webhook  ──► Upstash Redis Streams (TLS)  ──► resolv-ingestion
  (always-warm)   │                                        (scale-to-zero)
                  │   WAKE_URLS POST /wake (port 8080)
                  └──────────────────────────────────► resolv-triage
                                                            (scale-to-zero)
```

**Scale-from-zero wake-up:** After enqueueing an event to Redis, the webhook container sends a non-blocking `POST` to each worker's internal `/wake` endpoint (configured via `WAKE_URLS`). This pulls workers from 0 replicas before the first Redis `XREADGROUP` poll, eliminating cold-start latency for the job that triggered the wake.

### Cloud infrastructure

| Component | Service |
|---|---|
| Compute | Azure Container Apps — Malaysia West, `minReplicas: 0` |
| Message queue | Upstash Redis Streams (`rediss://` TLS), consumer groups `ingestion-group` / `triage-group` |
| Vector store | Qdrant Cloud Free Tier — collection `resolv_issues`, keyword payload index on `repo_id` |
| Embeddings | Google Gemini `gemini-embedding-001` (768-dim) via `src/embedder.py` |
| LLM | Groq (`openai/gpt-oss-20b`) via `src/triage.py` |

### Cloud environment variables

The following env vars are added on top of the local defaults. No secrets are stored in the repository.

| Variable | Module | Purpose |
|---|---|---|
| `GITHUB_PRIVATE_KEY` | `src/config.py` | Inline PEM string (replaces file path for containerised deploys) |
| `QDRANT_API_KEY` | `src/indexer.py` | Qdrant Cloud API key |
| `GEMINI_API_KEY` | `src/embedder.py` | Google Gemini embeddings API key |
| `GROQ_API_KEY` | `src/triage.py` | Groq LLM API key |
| `GROQ_MODEL` | `src/triage.py` | Model override (defaults to `openai/gpt-oss-20b`) |
| `WAKE_URLS` | `src/webhook.py` | Comma-separated internal `/wake` URLs for each worker container |

---

## Quickstart (local)

### Prerequisites

- Python 3.11+
- Docker Desktop running
- Ollama running locally with `llama3.1:8b` pulled (`ollama pull llama3.1:8b`)
- A GitHub App configured (see [ARCHITECTURE.md](ARCHITECTURE.md) §7 for setup)

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

- **Single Ollama model for both embeddings and generation** — eliminates the need for PyTorch/sentence-transformers, keeping the Docker image at ~105 MB and RAM usage low enough for an 8 GB MacBook Air.
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
