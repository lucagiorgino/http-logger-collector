from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_legacy_auth_endpoints_removed() -> None:
    authorize_response = client.post(
        "/auth/authorize",
        json={"user_id": "user-123", "device_id": "device-456"},
    )
    token_response = client.post(
        "/auth/token",
        json={"user_id": "user-123"},
    )

    assert authorize_response.status_code == 404
    assert token_response.status_code == 404
