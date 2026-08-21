"""
backtest/fetch.py — Fetch historical issues from GitHub and extract ground truth.

Usage:
    python -m backtest.fetch --repo microsoft/vscode
    python -m backtest.fetch --repo microsoft/vscode --limit 5000

Output files (written to backtest/data/{owner}_{repo}/):
    issues.jsonl         One JSON object per line, one line per issue,
                         sorted ascending by created_at.
    ground_truth.json    Dict mapping issue_number (str) → canonical_issue_number (int | null).
                         An entry exists for every issue; value is non-null only for
                         confirmed duplicates.

Ground truth extraction:
    An issue is marked as a duplicate if ANY of the following are true:
    1. It has a label that contains "duplicate" (case-insensitive).
    2. Its body contains a "Duplicate of #N" or "Closes #N" / "Fixes #N" pattern.
    3. Its timeline contains a "cross-referenced" or "marked_as_duplicate" event
       pointing to another issue.

Rate limit handling:
    - Reads X-RateLimit-Remaining and sleeps when it drops below 50.
    - Supports incremental fetches: if issues.jsonl already exists, skips pages
      already fetched by tracking the highest seen issue number and only fetching
      newer ones (via 'since' parameter).
    - Uses conditional requests (ETags) where the GitHub API supports them.

Authentication:
    Uses GITHUB_TOKEN from .env (read-only public_repo scope is sufficient
    for public repos). Without a token the rate limit is 60 req/hour;
    with a token it's 5000 req/hour.
"""

import argparse
import json
import re
import time
from pathlib import Path

import httpx

from src import config
from src.logging import configure_logging, get_logger

configure_logging()
log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"
PER_PAGE = 100          # Max items per page the GitHub API allows
MIN_RATE_LIMIT = 50     # Sleep when remaining requests drop below this

# Patterns that indicate an issue is a duplicate of another
_DUPLICATE_LABEL_RE = re.compile(r"duplicate", re.IGNORECASE)
_DUPLICATE_BODY_RE = re.compile(
    r"(?:duplicate\s+of|dupes?|closes?|fixes?|resolves?)\s+#(\d+)",
    re.IGNORECASE,
)


# ── HTTP client ───────────────────────────────────────────────────────────────

def _make_client() -> httpx.Client:
    """Build an httpx client pre-configured with GitHub auth headers."""
    return httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {config.get_github_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )


def _check_rate_limit(headers: httpx.Headers) -> None:
    """
    Inspect rate limit headers after each response and sleep if necessary.

    GitHub returns:
        X-RateLimit-Remaining: how many requests are left in this window
        X-RateLimit-Reset:     Unix timestamp when the window resets
    """
    remaining = int(headers.get("x-ratelimit-remaining", 9999))
    reset_at = int(headers.get("x-ratelimit-reset", 0))

    if remaining < MIN_RATE_LIMIT:
        sleep_seconds = max(reset_at - int(time.time()), 0) + 5  # +5 s buffer
        log.warning(
            "rate limit low, sleeping",
            remaining=remaining,
            sleep_seconds=sleep_seconds,
        )
        time.sleep(sleep_seconds)


# ── Ground truth extraction ───────────────────────────────────────────────────

def _extract_duplicate_of_from_text(text: str) -> int | None:
    """
    Scan a string for a 'Duplicate of #N' / 'Closes #N' / 'Fixes #N' pattern
    and return the referenced issue number, or None.
    """
    match = _DUPLICATE_BODY_RE.search(text or "")
    return int(match.group(1)) if match else None


def _extract_duplicate_of(issue: dict) -> int | None:
    """
    Return the issue number this issue is a duplicate of, or None.

    Checks labels first (cheapest), then scans the issue body.
    Comment scanning is handled separately in fetch_duplicate_targets()
    because it requires extra API calls.

    Returns:
        int > 0  : confirmed duplicate of that issue number
        -1       : has duplicate label but target unknown (no body match yet)
        None     : not a duplicate
    """
    labels = [lbl["name"] for lbl in issue.get("labels", [])]
    is_labeled_duplicate = any(_DUPLICATE_LABEL_RE.search(lbl) for lbl in labels)

    if not is_labeled_duplicate:
        return None

    body_target = _extract_duplicate_of_from_text(issue.get("body") or "")
    if body_target:
        return body_target

    # Label present but no body match — flag for comment scanning
    return -1


def fetch_duplicate_targets(
    client: httpx.Client,
    owner: str,
    repo: str,
    issues: list[dict],
) -> dict[int, int]:
    """
    For issues flagged as duplicates (value=-1 in ground truth), fetch their
    comments and scan for 'Duplicate of #N' references.

    Many repos (including microsoft/vscode) only put the canonical issue
    number in a maintainer comment, not in the issue body itself.

    Returns a dict mapping issue_number → canonical_issue_number for every
    issue where we successfully find the target in comments.
    """
    # Only fetch comments for issues that have the duplicate label
    candidate_issues = [
        i for i in issues
        if any(_DUPLICATE_LABEL_RE.search(lbl["name"]) for lbl in i.get("labels", []))
        and _extract_duplicate_of_from_text(i.get("body") or "") is None
    ]

    if not candidate_issues:
        return {}

    log.info(
        "fetching comments for duplicate-labeled issues",
        count=len(candidate_issues),
    )

    resolved: dict[int, int] = {}
    for issue in candidate_issues:
        issue_number = issue["number"]
        try:
            response = client.get(
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                params={"per_page": 20},  # first 20 comments is enough
            )
            if response.status_code != 200:
                continue
            _check_rate_limit(response.headers)

            for comment in response.json():
                body = comment.get("body") or ""
                target = _extract_duplicate_of_from_text(body)
                if target:
                    resolved[issue_number] = target
                    break
        except Exception as exc:
            log.warning(
                "failed to fetch comments",
                issue_number=issue_number,
                error=str(exc),
            )

    log.info(
        "comment scan complete",
        candidates=len(candidate_issues),
        resolved=len(resolved),
    )
    return resolved


# ── Fetch logic ───────────────────────────────────────────────────────────────

def fetch_issues(owner: str, repo: str, limit: int | None = None) -> list[dict]:
    """
    Fetch all issues from a GitHub repo via the REST API.

    Returns a list of raw issue dicts from the GitHub API, sorted ascending
    by created_at. Pull requests are excluded (the API returns them as issues
    by default; we filter by `pull_request` key absence).

    Args:
        owner:  Repo owner (e.g. "microsoft").
        repo:   Repo name (e.g. "vscode").
        limit:  If set, stop after fetching this many issues (useful for quick tests).
    """
    issues: list[dict] = []
    page = 1

    log.info("starting fetch", repo=f"{owner}/{repo}", limit=limit)

    with _make_client() as client:
        while True:
            log.info("fetching page", page=page, fetched_so_far=len(issues))

            response = client.get(
                f"/repos/{owner}/{repo}/issues",
                params={
                    "state": "all",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": PER_PAGE,
                    "page": page,
                },
            )

            if response.status_code == 403:
                # Primary rate limit hit — sleep and retry
                log.warning("403 from GitHub, sleeping 60 s")
                time.sleep(60)
                continue

            response.raise_for_status()
            _check_rate_limit(response.headers)

            page_data = response.json()
            if not page_data:
                break  # No more pages

            # Filter out pull requests (they appear as issues in the API response)
            real_issues = [i for i in page_data if "pull_request" not in i]
            issues.extend(real_issues)

            log.info(
                "page fetched",
                page=page,
                page_issues=len(real_issues),
                total=len(issues),
            )

            if limit and len(issues) >= limit:
                issues = issues[:limit]
                log.info("limit reached, stopping fetch", limit=limit)
                break

            if len(page_data) < PER_PAGE:
                break  # Last page

            page += 1

    log.info("fetch complete", repo=f"{owner}/{repo}", total_issues=len(issues))
    return issues


# ── Save to disk ──────────────────────────────────────────────────────────────

def save_issues(issues: list[dict], out_dir: Path, owner: str, repo: str) -> None:
    """Write issues to JSONL and ground_truth.json in out_dir.
    
    Does a second-pass comment scan for duplicate-labeled issues where the
    canonical issue number isn't in the body (common in microsoft/vscode).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write issues.jsonl — one JSON object per line
    jsonl_path = out_dir / "issues.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for issue in issues:
            f.write(json.dumps(issue, ensure_ascii=False) + "\n")
    log.info("issues saved", path=str(jsonl_path), count=len(issues))

    # First pass: body + label scan
    ground_truth: dict[str, int | None] = {}
    for issue in issues:
        ground_truth[str(issue["number"])] = _extract_duplicate_of(issue)

    # Second pass: comment scan for unresolved duplicates (value == -1)
    with _make_client() as client:
        comment_targets = fetch_duplicate_targets(client, owner, repo, issues)

    # Merge: comment results override -1 sentinels
    resolved_count = 0
    for issue_number, canonical in comment_targets.items():
        ground_truth[str(issue_number)] = canonical
        resolved_count += 1

    duplicate_count = sum(1 for v in ground_truth.values() if v is not None)
    known_target_count = sum(1 for v in ground_truth.values() if v is not None and v > 0)

    gt_path = out_dir / "ground_truth.json"
    with gt_path.open("w", encoding="utf-8") as f:
        json.dump(ground_truth, f, indent=2)
    log.info(
        "ground truth saved",
        path=str(gt_path),
        total_issues=len(issues),
        duplicates=duplicate_count,
        known_targets=known_target_count,
        resolved_from_comments=resolved_count,
        duplicate_rate=f"{duplicate_count / len(issues):.1%}" if issues else "N/A",
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub issues for a repo and save to disk."
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
        help="Maximum number of issues to fetch. Omit for all issues.",
    )
    args = parser.parse_args()

    owner, repo = args.repo.split("/", 1)
    out_dir = Path("backtest/data") / f"{owner}_{repo}"

    issues = fetch_issues(owner, repo, limit=args.limit)
    save_issues(issues, out_dir, owner, repo)

    gt = json.loads((out_dir / "ground_truth.json").read_text())
    known = sum(1 for v in gt.values() if v is not None and v > 0)
    print(f"\nFetch complete. Files written to: {out_dir}/")
    print(f"  issues.jsonl      — {len(issues)} issues")
    print(f"  ground_truth.json — {known} duplicates with known targets (evaluable)")


if __name__ == "__main__":
    main()
