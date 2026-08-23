"""
src/triage.py — Core triage logic: retrieve similar issues, call the LLM,
parse structured output.

This module is the same logic used by both:
    - The offline backtest replay (Phase 1)
    - The live Triage Worker (Phase 2+)

Keeping it here rather than in backtest/ means Phase 2 just imports it.

Flow:
    1. embed(query_issue) → query_vector
    2. search_similar(query_vector, repo_id, top_k) → retrieved_issues
    3. build_prompt(query_issue, retrieved_issues) → prompt string
    4. call_llm(prompt) → raw text response
    5. parse_response(raw_text) → TriageResult dataclass

LLM: Ollama HTTP API (POST /api/generate)
    Model configured via OLLAMA_MODEL in .env (default: llama3.1:8b)
    Self-hosted, zero API cost, full latency control.
    Retry with exponential backoff on transient errors.
"""

import json
import re
import time
from dataclasses import dataclass, field

import httpx

from src import config
from src.embedder import embed, prepare_text
from src.indexer import SearchResult, search_similar
from src.logging import get_logger
from src.metrics import triage_stage_duration, llm_call_errors
from src.tracing import tracer

log = get_logger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class TriageResult:
    """Structured output from one triage run."""
    is_duplicate: bool
    confidence: float                   # 0.0–1.0; LLM's stated confidence
    duplicate_of: int | None            # issue number of the canonical issue
    suggested_labels: list[str]
    related_issues: list[int]           # issue numbers of related (non-duplicate) issues
    raw_response: str = ""              # full LLM text, preserved for debugging
    retrieval_scores: list[float] = field(default_factory=list)  # top-k cosine scores


# ── Prompt construction ───────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert GitHub issue triage assistant. You will be given a new issue
and a list of potentially related past issues. Your job is to:
1. Determine if the new issue is a duplicate of any past issue.
2. Suggest appropriate labels.
3. Identify related (but not duplicate) issues.

Respond ONLY with valid JSON matching this schema, no prose, no markdown fences:
{
  "is_duplicate": true | false,
  "confidence": 0.0–1.0,
  "duplicate_of": <issue_number> | null,
  "suggested_labels": ["label1", "label2"],
  "related_issues": [<issue_number>, ...]
}

Rules:
- "is_duplicate" is true only if the new issue describes the SAME bug or request as an existing one.
- "confidence" reflects your certainty (0.9+ = very sure, 0.5 = unsure).
- "duplicate_of" must be null if "is_duplicate" is false.
- "suggested_labels" should use labels commonly used in GitHub issues (e.g. "bug", "enhancement", "question", "documentation").
- "related_issues" lists issues that share context but are not the same problem.
"""


def build_prompt(
    title: str,
    body: str | None,
    retrieved: list[SearchResult],
) -> str:
    """
    Build the full prompt string sent to the LLM.

    Keeps the context window bounded: each retrieved issue contributes at most
    one short paragraph. With top_k=10 and 512-char body excerpts this stays
    well under 4096 tokens for 7–8B models.
    """
    body_excerpt = (body or "").strip()[:512]
    new_issue_block = f"NEW ISSUE:\nTitle: {title}\nBody: {body_excerpt}\n"

    if not retrieved:
        context_block = "PAST ISSUES: (none found — this may be the first issue in this repo)\n"
    else:
        lines = ["PAST ISSUES (sorted by similarity, highest first):"]
        for r in retrieved:
            lines.append(
                f"\n#{r.issue_number} [score={r.score:.3f}] [{r.state}] {r.title}\n"
                f"  {r.body_excerpt[:200]}"
            )
        context_block = "\n".join(lines)

    return f"{_SYSTEM_PROMPT}\n\n{context_block}\n\n{new_issue_block}\nJSON response:"


# ── LLM call ─────────────────────────────────────────────────────────────────

def call_ollama(prompt: str, retries: int = 3) -> str:
    """
    Send a prompt to the Ollama API and return the response text.

    Uses the non-streaming /api/generate endpoint for simplicity.
    Retries up to `retries` times with exponential backoff on transient errors.

    Raises RuntimeError after all retries are exhausted.
    """
    url = f"{config.OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            # Low temperature for deterministic structured output
            "temperature": 0.1,
            "top_p": 0.9,
        },
    }

    for attempt in range(retries):
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                return response.json()["response"]
        except (httpx.HTTPError, KeyError) as exc:
            wait = 2 ** attempt
            log.warning(
                "ollama call failed, retrying",
                attempt=attempt + 1,
                retries=retries,
                wait_seconds=wait,
                error=str(exc),
            )
            if attempt < retries - 1:
                time.sleep(wait)
    
    llm_call_errors.inc()
    raise RuntimeError(f"Ollama call failed after {retries} attempts")


# ── Response parsing ──────────────────────────────────────────────────────────

def parse_response(raw: str) -> dict:
    """
    Extract the JSON object from the LLM response.

    LLMs sometimes wrap JSON in markdown fences or add trailing prose.
    This function strips fences and finds the first {...} block.
    """
    # Strip markdown code fences if present
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = raw.replace("```", "")

    # Find the first JSON object in the text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in LLM response: {raw!r}")

    return json.loads(match.group())


# ── Main triage function ──────────────────────────────────────────────────────

def triage_issue(
    repo_id: str,
    issue_number: int,
    title: str,
    body: str | None,
    top_k: int | None = None,
    exclude_self: bool = True,
) -> TriageResult:
    """
    Full triage pipeline for a single issue.

    Args:
        repo_id:        "owner/repo" string, used to filter Qdrant search.
        issue_number:   The issue's number in the repo.
        title:          Issue title.
        body:           Issue body (may be None for issues with no description).
        top_k:          How many similar issues to retrieve. Defaults to config.TOP_K.
        exclude_self:   If True, the query issue is excluded from results even if
                        it's already in the index. Set False in unit tests.

    Returns:
        TriageResult dataclass with the LLM's verdict.
    """
    k = top_k if top_k is not None else config.TOP_K

    # Step 1: Embed the incoming issue
    with triage_stage_duration.labels(stage="embed").time():
        with tracer.start_as_current_span("embed"):
            text = prepare_text(title, body)
            query_vector = embed(text)

    # Step 2: Retrieve similar past issues (filtered by repo_id — no cross-tenant leakage)
    with triage_stage_duration.labels(stage="retrieve").time():
        with tracer.start_as_current_span("retrieve"):
            retrieved = search_similar(
                query_vector=query_vector,
                repo_id=repo_id,
                top_k=k,
                exclude_issue_number=issue_number if exclude_self else None,
            )

    log.debug(
        "retrieval complete",
        repo_id=repo_id,
        issue_number=issue_number,
        retrieved_count=len(retrieved),
        top_score=retrieved[0].score if retrieved else None,
    )

    # Step 3: Build prompt and call LLM
    with triage_stage_duration.labels(stage="generate").time():
        with tracer.start_as_current_span("llm_generate"):
            prompt = build_prompt(title, body, retrieved)
            raw_response = call_ollama(prompt)

    # Step 4: Parse structured output
    with triage_stage_duration.labels(stage="parse").time():
        try:
            parsed = parse_response(raw_response)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "failed to parse LLM response, returning safe default",
                issue_number=issue_number,
                error=str(exc),
            )
            # Return a safe "no determination" result rather than crashing the pipeline
            return TriageResult(
                is_duplicate=False,
                confidence=0.0,
                duplicate_of=None,
                suggested_labels=[],
                related_issues=[],
                raw_response=raw_response,
                retrieval_scores=[r.score for r in retrieved],
            )

    return TriageResult(
        is_duplicate=bool(parsed.get("is_duplicate", False)),
        confidence=float(parsed.get("confidence", 0.0)),
        duplicate_of=parsed.get("duplicate_of"),
        suggested_labels=parsed.get("suggested_labels", []),
        related_issues=parsed.get("related_issues", []),
        raw_response=raw_response,
        retrieval_scores=[r.score for r in retrieved],
    )
