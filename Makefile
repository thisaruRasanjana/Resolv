.PHONY: help install up down fetch backtest test lint

# Use python3 explicitly — macOS does not symlink 'python' by default
PYTHON := python3

help:
	@echo ""
	@echo "  Resolv — GitHub Issue Triage Bot"
	@echo ""
	@echo "  Setup:"
	@echo "    make install          Install Python dependencies"
	@echo "    make up               Start Qdrant via Docker Compose"
	@echo "    make down             Stop Docker services"
	@echo ""
	@echo "  Phase 1 — Backtest:"
	@echo "    make fetch            Fetch issues from the target repo"
	@echo "    make fetch REPO=owner/repo   Fetch from a specific repo"
	@echo "    make backtest         Run the full backtest (requires fetched data)"
	@echo "    make backtest REPO=owner/repo LIMIT=2000"
	@echo ""
	@echo "  Development:"
	@echo "    make test             Run unit tests"
	@echo "    make lint             Run ruff linter"
	@echo ""

install:
	$(PYTHON) -m pip install -e ".[dev]"

up:
	docker compose up -d
	@echo "Qdrant dashboard: http://localhost:6333/dashboard"

down:
	docker compose down

# Fetch issues from GitHub. REPO defaults to microsoft/vscode.
# LIMIT caps the number of issues fetched (default: no limit).
# Example: make fetch REPO=microsoft/vscode LIMIT=5000
REPO ?= microsoft/vscode
LIMIT ?=

fetch:
	@mkdir -p backtest/data backtest/results
	$(PYTHON) -m backtest.fetch \
		--repo $(REPO) \
		$(if $(LIMIT),--limit $(LIMIT),)

# Run the full backtest replay.
# Uses the same REPO and optional LIMIT variables.
backtest:
	$(PYTHON) -m backtest.replay \
		--repo $(REPO) \
		$(if $(LIMIT),--limit $(LIMIT),)

test:
	$(PYTHON) -m pytest tests/ -v

lint:
	ruff check src/ backtest/ tests/

# Phase 2: Webhook receiver + workers
webhook-server:
	$(PYTHON) -m uvicorn src.webhook:app --host 0.0.0.0 --port 8000 --reload

SMEE_URL ?=
smee:
	smee --url $(SMEE_URL) --path /webhook --port 8000

ingestion-worker:
	$(PYTHON) -m src.workers.ingestion

triage-worker:
	$(PYTHON) -m src.workers.triage
