from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_agent_logs_reject_missing_bearer_token() -> None:
    response = client.post(
        "/agent/traffic_logs",
        json={
            "user_id": "user-123",
            "device_id": "device-456",
            "timestamp": "2026-01-15T10:00:00Z",
            "method": "GET",
            "url": "https://example.com/path",
            "status_code": 200,
            "headers": {"User-Agent": "TestAgent"},
        },
    )
    assert response.status_code == 401
    assert "Missing or invalid bearer token" in response.json()["detail"]
