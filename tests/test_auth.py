import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer invalid-token"])
def test_identity_requires_a_bearer_jwt(authorization) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    response = client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_api_exposes_only_current_resources() -> None:
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/health",
        "/api/v1/me",
        "/api/v1/devices/{device_id}/configuration",
        "/api/v1/devices/{device_id}/configuration-status",
        "/api/v1/traffic-log-batches",
        "/api/v1/traffic-logs",
        "/api/v1/traffic-logs/{record_id}",
    }
