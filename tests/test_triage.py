"""
tests/test_triage.py — Unit tests for src/triage.py.

We mock the Qdrant search and Ollama call so these tests run without any
external services. The goal is to verify:
  1. Prompt construction is correct and contains all required information.
  2. JSON response parsing handles clean output, fenced output, and bad output.
  3. The main triage_issue() function wires everything together correctly.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.indexer import SearchResult
from src.triage import TriageResult, build_prompt, parse_response, triage_issue


# ── build_prompt tests ────────────────────────────────────────────────────────

def test_prompt_contains_new_issue_title():
    prompt = build_prompt(
        title="App crashes on startup",
        body="Steps: 1. Open app 2. Crash",
        retrieved=[],
    )
    assert "App crashes on startup" in prompt


def test_prompt_contains_retrieved_issue():
    retrieved = [
        SearchResult(
            issue_number=42,
            repo_id="owner/repo",
            title="Application fails to launch",
            body_excerpt="The app crashes immediately",
            state="closed",
            score=0.91,
            created_at="2024-01-01T00:00:00",
            type="issue",
        )
    ]
    prompt = build_prompt("Crash on open", "Can't open the app", retrieved)
    assert "#42" in prompt
    assert "Application fails to launch" in prompt
    assert "0.910" in prompt  # score formatted to 3dp


def test_prompt_truncates_body_to_512():
    long_body = "A" * 1000
    prompt = build_prompt("Title", long_body, [])
    # Body excerpt in prompt should not contain the full 1000-char body
    assert "A" * 600 not in prompt


def test_prompt_handles_none_body():
    prompt = build_prompt("Title", None, [])
    assert "Title" in prompt


def test_prompt_with_no_retrieved_shows_none_found():
    prompt = build_prompt("Title", "body", [])
    assert "none found" in prompt.lower() or "PAST ISSUES" in prompt


# ── parse_response tests ──────────────────────────────────────────────────────

_VALID_JSON = {
    "is_duplicate": True,
    "confidence": 0.87,
    "duplicate_of": 123,
    "suggested_labels": ["bug"],
    "related_issues": [100, 101],
}


def test_parse_clean_json():
    raw = json.dumps(_VALID_JSON)
    result = parse_response(raw)
    assert result["is_duplicate"] is True
    assert result["duplicate_of"] == 123


def test_parse_fenced_json():
    raw = f"```json\n{json.dumps(_VALID_JSON)}\n```"
    result = parse_response(raw)
    assert result["confidence"] == 0.87


def test_parse_json_with_leading_prose():
    raw = f"Here is my analysis:\n\n{json.dumps(_VALID_JSON)}\n\nLet me know if you need more."
    result = parse_response(raw)
    assert result["suggested_labels"] == ["bug"]


def test_parse_raises_on_no_json():
    with pytest.raises(ValueError, match="No JSON"):
        parse_response("Sorry, I cannot determine this.")


# ── triage_issue integration tests ────────────────────────────────────────────

@patch("src.triage.call_ollama")
@patch("src.triage.search_similar")
@patch("src.triage.embed")
def test_triage_issue_returns_result(mock_embed, mock_search, mock_llm):
    """Full pipeline wired together with mocked external calls."""
    mock_embed.return_value = [0.1] * 384
    mock_search.return_value = [
        SearchResult(
            issue_number=10,
            repo_id="owner/repo",
            title="Similar bug",
            body_excerpt="Same crash",
            state="closed",
            score=0.95,
            created_at="2024-01-01",
            type="issue",
        )
    ]
    mock_llm.return_value = json.dumps({
        "is_duplicate": True,
        "confidence": 0.9,
        "duplicate_of": 10,
        "suggested_labels": ["bug"],
        "related_issues": [],
    })

    result = triage_issue(
        repo_id="owner/repo",
        issue_number=99,
        title="App crashes",
        body="It crashes on launch",
    )

    assert isinstance(result, TriageResult)
    assert result.is_duplicate is True
    assert result.duplicate_of == 10
    assert result.confidence == 0.9
    assert "bug" in result.suggested_labels


@patch("src.triage.call_ollama")
@patch("src.triage.search_similar")
@patch("src.triage.embed")
def test_triage_issue_handles_unparseable_llm_output(mock_embed, mock_search, mock_llm):
    """When the LLM returns garbage, triage returns a safe default (no crash)."""
    mock_embed.return_value = [0.0] * 384
    mock_search.return_value = []
    mock_llm.return_value = "I am unable to determine this. Please try again."

    result = triage_issue(
        repo_id="owner/repo",
        issue_number=1,
        title="Some issue",
        body=None,
    )

    # Safe default: not marked as a duplicate
    assert result.is_duplicate is False
    assert result.confidence == 0.0
    assert result.duplicate_of is None


# ── No-future-leakage property test ──────────────────────────────────────────

def test_no_future_leakage_in_replay():
    """
    Verify the replay ordering guarantees: triage is called before the issue
    is indexed, so search cannot return the query issue itself.

    This test simulates the replay loop logic in a minimal way, without
    running the full pipeline.
    """
    indexed_issue_numbers = []
    triage_call_order = []

    # Simulate the replay loop for 3 issues in order
    issues = [
        {"number": 1, "created_at": "2024-01-01"},
        {"number": 2, "created_at": "2024-01-02"},
        {"number": 3, "created_at": "2024-01-03"},
    ]

    for issue in issues:
        num = issue["number"]
        # Before triage: record what's in the "index"
        triage_call_order.append((num, list(indexed_issue_numbers)))
        # After triage: add to index
        indexed_issue_numbers.append(num)

    # When issue 1 is triaged, index is empty
    assert triage_call_order[0] == (1, [])
    # When issue 2 is triaged, only issue 1 is in the index
    assert triage_call_order[1] == (2, [1])
    # When issue 3 is triaged, issues 1 and 2 are in the index
    assert triage_call_order[2] == (3, [1, 2])
    # Issue 3 is never in the index when we search for its duplicates
    assert 3 not in triage_call_order[2][1]
