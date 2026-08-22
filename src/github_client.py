"""
src/github_client.py — GitHub App authentication and API client.

Auth flow (ARCHITECTURE.md §7):
1. Sign a short-lived JWT (~10 min) with the App's private key.
2. Exchange the JWT for an installation access token (~1 hour) scoped to the repo.
3. Use the installation token for all API calls.

The installation token is cached and refreshed when it has <5 min remaining.
"""

import time

import httpx
import jwt  # PyJWT

from src import config
from src.logging import get_logger

log = get_logger(__name__)

# Cache: {installation_id: (token, expires_at_timestamp)}
_token_cache: dict[str, tuple[str, float]] = {}


def _create_jwt() -> str:
    """
    Create a short-lived JWT signed with the App's private key.

    The JWT is used to authenticate AS the App itself (not as an installation).
    It's only valid for 10 minutes and is exchanged for an installation token.
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued at time (60s in the past to handle clock drift)
        "exp": now + (10 * 60),  # Expires in 10 minutes
        "iss": str(config.get_github_app_id()),  # App ID must be a string for PyJWT
    }
    private_key = config.get_github_private_key()
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token(installation_id: str) -> str:
    """
    Get a valid installation access token, using cache when possible.

    If cached token has >5 min remaining, return it.
    Otherwise, exchange the JWT for a new one.
    """
    cached = _token_cache.get(installation_id)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at - 300:  # 5 min buffer
            return token

    # Exchange JWT for installation token
    app_jwt = _create_jwt()
    with httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30.0,
    ) as client:
        response = client.post(f"/app/installations/{installation_id}/access_tokens")
        response.raise_for_status()
        data = response.json()

    token = data["token"]
    # GitHub returns expires_at as ISO 8601 — parse to timestamp
    from datetime import datetime
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()

    _token_cache[installation_id] = (token, expires_at)
    log.info("installation token refreshed", installation_id=installation_id)
    return token


def post_comment(
    installation_id: str,
    repo_id: str,
    issue_number: int,
    body: str,
    retries: int = 3,
) -> None:
    """
    Post a comment on a GitHub issue.

    Retries with exponential backoff on 5xx errors and rate limits (403).
    """
    owner, repo = repo_id.split("/", 1)
    token = get_installation_token(installation_id)

    for attempt in range(retries):
        try:
            with httpx.Client(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=30.0,
            ) as client:
                response = client.post(
                    f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                    json={"body": body},
                )

                if response.status_code == 403:
                    # Rate limited — refresh token and retry
                    log.warning("rate limited by GitHub, retrying", attempt=attempt + 1)
                    token = get_installation_token(installation_id)
                    time.sleep(2 ** attempt)
                    continue

                response.raise_for_status()
                log.info(
                    "comment posted",
                    repo_id=repo_id,
                    issue_number=issue_number,
                )
                return

        except httpx.HTTPError as exc:
            wait = 2 ** attempt
            log.warning(
                "github API call failed, retrying",
                attempt=attempt + 1,
                error=str(exc),
                wait_seconds=wait,
            )
            if attempt < retries - 1:
                time.sleep(wait)

    raise RuntimeError(f"Failed to post comment after {retries} attempts")


def format_triage_comment(result) -> str:
    """
    Format a TriageResult into a Markdown comment for GitHub.

    Uses a clean, professional, non-intrusive format.
    """
    lines = ["**Resolv Triage Report**\n"]

    # Duplicate verdict
    if result.is_duplicate and result.duplicate_of:
        lines.append(
            f"**Status:** Possible duplicate of #{result.duplicate_of} "
            f"(confidence: {result.confidence:.0%})\n"
        )
    elif result.is_duplicate:
        lines.append(
            f"**Status:** Possible duplicate (confidence: {result.confidence:.0%}), "
            f"but unable to identify the exact original issue.\n"
        )
    else:
        lines.append("**Status:** No duplicate detected.\n")

    # Suggested labels
    if result.suggested_labels:
        labels_str = ", ".join(f"`{l}`" for l in result.suggested_labels)
        lines.append(f"**Suggested labels:** {labels_str}\n")

    # Related issues
    if result.related_issues:
        related_str = ", ".join(f"#{n}" for n in result.related_issues)
        lines.append(f"**Related issues:** {related_str}\n")

    lines.append("---")
    lines.append("*Automated triage provided by Resolv*")

    return "\n".join(lines)
