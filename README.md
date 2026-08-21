# Resolv — GitHub Issue Triage Bot

An AI bot that triages new GitHub issues: detecting duplicates, suggesting labels, and linking related PRs, by retrieving semantically similar past issues and having an LLM synthesize a comment.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full technical spec.

---

## Current status: Phase 1 — Backtest Harness

The offline backtest harness is fully implemented. You can run it against any public GitHub repo to measure how well the retrieval + LLM pipeline detects duplicate issues against real historical data — no live installs or webhooks required.

---

## Tech stack

| Layer | Choice |
|---|---|
| Embeddings | `all-MiniLM-L6-v2` (sentence-transformers, 384-dim, runs on Apple MPS) |
| Vector store | Qdrant |
| LLM | Ollama (`llama3.1:8b`, self-hosted, zero cost) |
| State store | SQLite (→ Postgres in Phase 3) |
| Language | Python 3.11+ |

---

## Quickstart (Phase 1 — Backtest)

### Prerequisites
- Python 3.11+
- Docker Desktop running
- Ollama running with `llama3.1:8b` pulled (`ollama pull llama3.1:8b`)
- A GitHub Personal Access Token with `public_repo` read scope

### 1. Clone and install

```bash
git clone https://github.com/yourname/resolv
cd resolv
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set GITHUB_TOKEN=ghp_your_token_here
```

### 3. Start Qdrant

```bash
make up
# Qdrant dashboard: http://localhost:6333/dashboard
```

### 4. Fetch issues

Fetches issues from `microsoft/vscode` (the default repo). Use `--limit` to cap
the number of issues for a quick test:

```bash
make fetch                          # all issues (slow — 170k issues)
make fetch LIMIT=2000               # fast test run (~2000 issues)
make fetch REPO=facebook/react LIMIT=3000
```

Data is saved to `backtest/data/{owner}_{repo}/`:
- `issues.jsonl` — raw issue data, one JSON object per line
- `ground_truth.json` — maps each issue number to its duplicate target (or null)

### 5. Run the backtest

```bash
make backtest LIMIT=2000            # use same LIMIT as your fetch
make backtest REPO=facebook/react LIMIT=3000
```

The replay engine processes issues in chronological order. For each issue that is
a known duplicate (based on labels and body patterns), it:
1. Runs the full retrieval + LLM pipeline against the index as it existed at that point in time.
2. Records whether it correctly identified the duplicate.

Outputs:
- A metrics table in the terminal (Precision / Recall / F1)
- A markdown report at `backtest/results/{owner}_{repo}_report.md`
- Metrics stored in `backtest/data/resolv.db` (SQLite)

### 6. Run unit tests

```bash
make test
```

> **Note:** `tests/test_indexer.py` requires Qdrant to be running (`make up`). The other tests run without any services.

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
│   ├── db.py             SQLite state store
│   └── logging.py        Structured JSON logging (structlog)
├── backtest/
│   ├── fetch.py          GitHub API fetcher + ground-truth extractor
│   └── replay.py         Chronological replay + P/R/F1 evaluation
└── tests/
    ├── test_embedder.py  Embedding correctness + similarity ranking
    ├── test_triage.py    Prompt construction + response parsing + mocked pipeline
    └── test_indexer.py   Qdrant upsert / search / multi-tenant isolation
```
