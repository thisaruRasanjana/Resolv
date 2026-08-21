"""
backtest/replay.py — Chronological replay engine.

The core correctness requirement (ARCHITECTURE.md §5):
    At the point we triage issue N, the Qdrant index must contain ONLY
    issues with created_at < issue_N.created_at. No future data leaks in.

How we enforce this:
    Issues are sorted ascending by created_at before replay begins.
    For each issue in that sorted order:
        1. We first triage it (search the index in its current state).
        2. Then we upsert it into the index.
    This means when we run the search for issue N, the index contains
    only issues 1…N-1 — exactly what the live system would see.

Evaluation (duplicate detection only):
    We evaluate only on issues that appear in ground_truth with a non-null,
    non-(-1) value (i.e. we know exactly which issue it duplicates).

    A prediction is:
        TP: is_duplicate=True and duplicate_of matches ground truth
        FP: is_duplicate=True but duplicate_of is wrong or no ground truth
        FN: is_duplicate=False but ground truth says it's a duplicate

    Precision = TP / (TP + FP)
    Recall    = TP / (TP + FN)
    F1        = 2 * P * R / (P + R)

Usage:
    python -m backtest.replay --repo microsoft/vscode
    python -m backtest.replay --repo microsoft/vscode --limit 2000

Output:
    Printed report + backtest/results/{owner}_{repo}_report.md
    Metrics stored in SQLite backtest_results table.
"""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
from rich.table import Table

from src import config
from src.db import BacktestResult, get_connection, save_backtest_result
from src.embedder import embed_batch, prepare_text
from src.indexer import IssuePoint, ensure_collection, upsert_issue, upsert_issues_batch
from src.logging import configure_logging, get_logger
from src.triage import TriageResult, triage_issue

configure_logging()
log = get_logger(__name__)
console = Console()


# ── Prediction record ─────────────────────────────────────────────────────────

@dataclass
class Prediction:
    issue_number: int
    ground_truth_duplicate_of: int      # canonical issue number
    predicted_duplicate_of: int | None  # what the model said
    is_duplicate_predicted: bool
    confidence: float
    retrieval_top_score: float


# ── Data loading ──────────────────────────────────────────────────────────────

def load_issues(data_dir: Path) -> list[dict]:
    """
    Load issues from issues.jsonl, sorted ascending by created_at.

    Sorting is critical: it's what guarantees the replay order matches the
    real-world chronological order in which issues were filed.
    """
    jsonl_path = data_dir / "issues.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(
            f"{jsonl_path} not found. Run `make fetch REPO=<owner/repo>` first."
        )

    issues = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                issues.append(json.loads(line))

    # Sort by created_at ascending — this is the replay order
    issues.sort(key=lambda i: i["created_at"])
    log.info("issues loaded", count=len(issues), sorted_by="created_at asc")
    return issues


def load_ground_truth(data_dir: Path) -> dict[str, int | None]:
    """Load ground_truth.json produced by fetch.py."""
    gt_path = data_dir / "ground_truth.json"
    if not gt_path.exists():
        raise FileNotFoundError(f"{gt_path} not found. Run fetch first.")

    with gt_path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ── Issue → IssuePoint conversion ────────────────────────────────────────────

def _parse_dt(s: str | None) -> datetime:
    """Parse an ISO-8601 string from the GitHub API to a timezone-aware datetime."""
    if not s:
        return datetime.now(timezone.utc)
    # GitHub timestamps are always UTC in "YYYY-MM-DDTHH:MM:SSZ" format
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def issue_to_point(issue: dict, repo_id: str, vector: list[float]) -> IssuePoint:
    """Convert a raw GitHub API issue dict + pre-computed vector to an IssuePoint."""
    return IssuePoint(
        repo_id=repo_id,
        issue_number=issue["number"],
        type="issue",
        state=issue.get("state", "open"),
        title=issue.get("title", ""),
        body_excerpt=(issue.get("body") or "")[:512],
        created_at=_parse_dt(issue.get("created_at")),
        updated_at=_parse_dt(issue.get("updated_at")),
        vector=vector,
    )


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(predictions: list[Prediction]) -> tuple[float, float, float]:
    """
    Compute precision, recall, F1 for duplicate detection.

    Only evaluates predictions where we have a known ground-truth canonical
    issue (i.e. ground_truth_duplicate_of > 0). Issues with the duplicate
    label but no identifiable target (-1) are excluded from evaluation
    to avoid false negatives penalizing correct detections.

    Returns:
        (precision, recall, f1)  — all in [0.0, 1.0]
    """
    tp = fp = fn = 0

    for p in predictions:
        gt = p.ground_truth_duplicate_of
        pred_is_dup = p.is_duplicate_predicted
        pred_target = p.predicted_duplicate_of

        if pred_is_dup and pred_target == gt:
            tp += 1
        elif pred_is_dup and pred_target != gt:
            fp += 1
        elif not pred_is_dup:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return precision, recall, f1


# ── Report generation ─────────────────────────────────────────────────────────

def write_report(
    repo_id: str,
    predictions: list[Prediction],
    precision: float,
    recall: float,
    f1: float,
    out_dir: Path,
    total_issues_replayed: int,
) -> Path:
    """Write a markdown report to backtest/results/ and print a Rich table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = repo_id.replace("/", "_")
    report_path = out_dir / f"{safe_name}_report.md"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_gt_dupes = len(predictions)

    # Rich table for terminal output
    table = Table(title=f"Backtest Results — {repo_id}", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold green")
    table.add_row("Issues replayed", str(total_issues_replayed))
    table.add_row("Known duplicates evaluated", str(total_gt_dupes))
    table.add_row("Precision", f"{precision:.4f}")
    table.add_row("Recall", f"{recall:.4f}")
    table.add_row("F1", f"{f1:.4f}")
    table.add_row("Embedding model", config.EMBEDDING_MODEL)
    table.add_row("LLM model", config.OLLAMA_MODEL)
    table.add_row("Top-k", str(config.TOP_K))
    console.print(table)

    # Sample of correct and incorrect predictions for qualitative analysis
    correct = [p for p in predictions if p.is_duplicate_predicted and p.predicted_duplicate_of == p.ground_truth_duplicate_of]
    incorrect = [p for p in predictions if not (p.is_duplicate_predicted and p.predicted_duplicate_of == p.ground_truth_duplicate_of)]

    # Markdown report content
    lines = [
        f"# Backtest Report — {repo_id}",
        f"",
        f"Generated: {now}",
        f"",
        f"## Configuration",
        f"| Setting | Value |",
        f"|---|---|",
        f"| Embedding model | `{config.EMBEDDING_MODEL}` |",
        f"| LLM model | `{config.OLLAMA_MODEL}` |",
        f"| Top-k | {config.TOP_K} |",
        f"| Issues replayed | {total_issues_replayed} |",
        f"| Known duplicates evaluated | {total_gt_dupes} |",
        f"",
        f"## Results",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Precision | **{precision:.4f}** |",
        f"| Recall | **{recall:.4f}** |",
        f"| F1 | **{f1:.4f}** |",
        f"",
        f"## Sample Correct Predictions (first 5)",
    ]

    for p in correct[:5]:
        lines.append(
            f"- Issue #{p.issue_number}: correctly identified as duplicate of "
            f"#{p.ground_truth_duplicate_of} (confidence={p.confidence:.2f}, "
            f"top_score={p.retrieval_top_score:.3f})"
        )

    lines += [f"", f"## Sample Incorrect Predictions (first 5)"]
    for p in incorrect[:5]:
        lines.append(
            f"- Issue #{p.issue_number}: ground truth=#{p.ground_truth_duplicate_of}, "
            f"predicted={'#' + str(p.predicted_duplicate_of) if p.predicted_duplicate_of else 'not a duplicate'} "
            f"(confidence={p.confidence:.2f})"
        )

    with report_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info("report written", path=str(report_path))
    return report_path


# ── Main replay loop ──────────────────────────────────────────────────────────

def run_replay(repo_id: str, limit: int | None = None) -> tuple[float, float, float]:
    """
    Run the full chronological replay and return (precision, recall, f1).

    The replay loop:
        For each issue in ascending created_at order:
            1. If it has a known duplicate target in ground truth →
               run triage (searching the current index state) and record prediction.
            2. Embed the issue and upsert it into the index.
               (Order matters: triage BEFORE index so we don't search against ourself.)

    No future leakage: step 2 happens after step 1, so when we search for
    duplicates of issue N, the index only contains issues 0…N-1.
    """
    owner, repo = repo_id.split("/", 1)
    data_dir = Path("backtest/data") / f"{owner}_{repo}"

    # Load data
    all_issues = load_issues(data_dir)
    ground_truth = load_ground_truth(data_dir)

    if limit:
        all_issues = all_issues[:limit]
        log.info("limit applied", limit=limit)

    # Initialise Qdrant collection (creates it if missing, no-op if exists)
    # Clear the collection to start fresh — important for reproducible results
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams
    from src.embedder import embedding_dim

    client = QdrantClient(
        host=config.QDRANT_HOST,
        port=config.QDRANT_PORT,
        check_compatibility=False,
    )
    # Drop and recreate the collection to ensure a clean replay state
    try:
        client.delete_collection(config.QDRANT_COLLECTION)
        log.info("cleared existing collection", collection=config.QDRANT_COLLECTION)
    except Exception:
        pass  # Collection didn't exist yet — fine
    ensure_collection()

    # Open SQLite connection
    conn = get_connection(config.SQLITE_PATH)

    predictions: list[Prediction] = []
    # Issues that definitely need triage evaluation (known duplicate target)
    eval_issue_numbers = {
        int(k) for k, v in ground_truth.items()
        if v is not None and v > 0  # exclude -1 (duplicate label, unknown target)
    }

    console.print(
        f"\n[bold]Starting replay[/bold]: {len(all_issues)} issues, "
        f"{len(eval_issue_numbers)} with known duplicate targets\n"
    )

    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task(f"Replaying {repo_id}…", total=len(all_issues))

        for i, issue in enumerate(all_issues):
            issue_number = issue["number"]
            title = issue.get("title", "")
            body = issue.get("body")

            # ── Step 1: Triage (only for issues we can evaluate) ──────────
            if issue_number in eval_issue_numbers:
                try:
                    result: TriageResult = triage_issue(
                        repo_id=repo_id,
                        issue_number=issue_number,
                        title=title,
                        body=body,
                        exclude_self=True,  # exclude self from search results
                    )
                    predictions.append(
                        Prediction(
                            issue_number=issue_number,
                            ground_truth_duplicate_of=ground_truth[str(issue_number)],
                            predicted_duplicate_of=result.duplicate_of,
                            is_duplicate_predicted=result.is_duplicate,
                            confidence=result.confidence,
                            retrieval_top_score=result.retrieval_scores[0] if result.retrieval_scores else 0.0,
                        )
                    )
                except Exception as exc:
                    log.error(
                        "triage failed for issue",
                        issue_number=issue_number,
                        error=str(exc),
                    )

            # ── Step 2: Index this issue (AFTER triage — no future leakage) ──
            text = prepare_text(title, body)
            vector = embed_batch([text])[0]
            point = issue_to_point(issue, repo_id, vector)
            upsert_issue(point)

            progress.advance(task)

            # Periodic progress log every 100 issues
            if (i + 1) % 100 == 0:
                evaluated = len(predictions)
                log.info(
                    "replay progress",
                    processed=i + 1,
                    total=len(all_issues),
                    evaluated=evaluated,
                )

    # ── Compute and store metrics ─────────────────────────────────────────
    precision, recall, f1 = compute_metrics(predictions)

    save_backtest_result(
        conn,
        BacktestResult(
            repo_id=repo_id,
            run_at=datetime.now(timezone.utc),
            precision=precision,
            recall=recall,
            f1=f1,
            notes=f"model={config.OLLAMA_MODEL}, embedding={config.EMBEDDING_MODEL}, top_k={config.TOP_K}",
        ),
    )

    out_dir = Path("backtest/results")
    report_path = write_report(
        repo_id=repo_id,
        predictions=predictions,
        precision=precision,
        recall=recall,
        f1=f1,
        out_dir=out_dir,
        total_issues_replayed=len(all_issues),
    )
    console.print(f"\nReport written to: [bold]{report_path}[/bold]")

    return precision, recall, f1


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the chronological replay backtest for duplicate detection."
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository in owner/repo format (e.g. microsoft/vscode)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of issues to replay (for quick test runs).",
    )
    args = parser.parse_args()

    precision, recall, f1 = run_replay(repo_id=args.repo, limit=args.limit)
    print(f"\nPrecision: {precision:.4f}  Recall: {recall:.4f}  F1: {f1:.4f}")


if __name__ == "__main__":
    main()
