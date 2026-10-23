from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_authorize_endpoint() -> None:
    response = client.post(
        "/auth/authorize",
        json={"user_id": "user-123", "device_id": "device-456"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "authorized"


def test_token_endpoint() -> None:
    response = client.post(
        "/auth/token",
        json={"user_id": "user-123"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["token"].startswith("token.user-123.")
