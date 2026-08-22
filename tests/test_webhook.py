import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.webhook import app

client = TestClient(app)


def test_missing_signature():
    response = client.post("/webhook", json={"action": "opened"})
    assert response.status_code == 401
    assert response.text == "Invalid signature"


@pytest.fixture
def mock_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    return "test-secret"


def generate_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_invalid_signature(mock_secret):
    body = json.dumps({"action": "opened"}).encode()
    response = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=invalid"},
    )
    assert response.status_code == 401


def test_non_issue_event(mock_secret):
    body = json.dumps({"action": "opened"}).encode()
    sig = generate_signature(mock_secret, body)
    
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
        },
    )
    assert response.status_code == 200
    assert "ignored" in response.text


def test_ignored_issue_action(mock_secret):
    body = json.dumps({"action": "assigned"}).encode()
    sig = generate_signature(mock_secret, body)
    
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "issues",
        },
    )
    assert response.status_code == 200
    assert "ignored" in response.text


@patch("src.webhook.publish_event")
def test_valid_issue_opened(mock_publish, mock_secret):
    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Test Issue",
            "body": "Test Body",
            "state": "open",
            "created_at": "2023-01-01T00:00:00Z",
            "updated_at": "2023-01-01T00:00:00Z",
        },
        "repository": {
            "full_name": "owner/repo"
        },
        "installation": {
            "id": 12345
        }
    }
    body = json.dumps(payload).encode()
    sig = generate_signature(mock_secret, body)
    
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "test-delivery-id",
        },
    )
    
    assert response.status_code == 200
    assert response.text == "OK"
    
    mock_publish.assert_called_once()
    args, _ = mock_publish.call_args
    event_data = args[0]
    assert event_data["action"] == "opened"
    assert event_data["repo_id"] == "owner/repo"
    assert event_data["issue_number"] == 42
    assert event_data["delivery_id"] == "test-delivery-id"
    assert event_data["installation_id"] == "12345"
