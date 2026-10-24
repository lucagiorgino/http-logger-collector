from typing import Any

from fastapi.testclient import TestClient

from app.core.auth import require_bearer_token
from app.deps import get_mongo_repository
from app.main import app

client = TestClient(app)


class FakeMongoRepository:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    async def insert_traffic_log(self, document: dict[str, Any]) -> str:
        self.documents.append(document)
        return "mongo-record-id"


def test_agent_logs_reject_without_authentication() -> None:
    response = client.post(
        "/agent/traffic_logs",
        json={
            "user_id": "user-123",
            "device_id": "device-456",
            "timestamp": "2026-01-15T10:00:00Z",
            "method": "GET",
            "url": "https://example.com/path",
            "status_code": 200,
            "headers": {"Authorization": "******", "User-Agent": "TestAgent"},
            "body": "ok",
        },
    )
    assert response.status_code == 401


def test_agent_logs_require_valid_auth0_jwt() -> None:
    response = client.post(
        "/agent/traffic_logs",
        json={
            "user_id": "user-123",
            "device_id": "device-456",
            "timestamp": "2026-01-15T10:00:00Z",
            "method": "GET",
            "url": "https://example.com/path",
            "status_code": 200,
            "headers": {"Authorization": "******", "User-Agent": "TestAgent"},
            "body": "ok",
        },
        headers={"Authorization": "******"},
    )
    assert response.status_code in {401, 403}


def test_agent_logs_are_normalized_redacted_and_persisted() -> None:
    repository = FakeMongoRepository()
    app.dependency_overrides[require_bearer_token] = lambda: {"sub": "user-123"}
    app.dependency_overrides[get_mongo_repository] = lambda: repository
    try:
        response = client.post(
            "/agent/traffic_logs",
            json={
                "user_id": "user-123",
                "device_id": "device-456",
                "timestamp": "2026-01-15T10:00:00+02:00",
                "method": "get",
                "url": "https://example.com/path",
                "status_code": 200,
                "headers": {
                    "Authorization": "Bearer secret",
                    "Cookie": "session=secret",
                    "User-Agent": "TestAgent",
                },
                "body": "sensitive request content",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "record_id": "mongo-record-id",
        "status": "stored",
    }
    assert len(repository.documents) == 1
    document = repository.documents[0]
    assert document["method"] == "GET"
    assert document["timestamp"].isoformat() == "2026-01-15T08:00:00+00:00"
    assert document["headers"]["Authorization"] == "[REDACTED]"
    assert document["headers"]["Cookie"] == "[REDACTED]"
    assert document["headers"]["User-Agent"] == "TestAgent"
    assert "body" not in document
    assert document["body_length"] == len("sensitive request content")
    assert document["body_sha256"]


def test_agent_logs_reject_cross_user_submission() -> None:
    repository = FakeMongoRepository()
    app.dependency_overrides[require_bearer_token] = lambda: {"sub": "different-user"}
    app.dependency_overrides[get_mongo_repository] = lambda: repository
    try:
        response = client.post(
            "/agent/traffic_logs",
            json={
                "user_id": "user-123",
                "device_id": "device-456",
                "timestamp": "2026-01-15T10:00:00Z",
                "method": "GET",
                "url": "https://example.com/path",
                "status_code": 200,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert repository.documents == []
