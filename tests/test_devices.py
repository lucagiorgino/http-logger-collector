from fastapi.testclient import TestClient

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.db.mongo import PersistenceError
from app.main import app

URL = "/api/v1/devices/device-456/configuration"


def admin():
    return {"sub": "user-123", "permissions": ["manage:devices"]}


def test_default_policy_is_owner_scoped_and_not_cached(authorized_client, repository):
    response = authorized_client.get(URL)
    assert response.status_code == 200
    assert response.json()["encryption_mode"] == "tls_only"
    assert response.json()["version"] == 0
    assert response.json()["encryption"] is None
    assert response.headers["cache-control"] == "no-store"
    repository.get_device_configuration.assert_awaited_once_with("user-123", "device-456")


def test_policy_changes_require_permission(authorized_client, repository):
    response = authorized_client.patch(
        URL, json={"expected_version": 0, "encryption_mode": "required"}
    )
    assert response.status_code == 403
    repository.update_device_configuration.assert_not_awaited()


def test_invalid_policy_and_unrepresentable_version_are_rejected(encrypted_client, repository):
    app.dependency_overrides[require_bearer_token] = admin
    for payload in (
        {"expected_version": 2**64, "encryption_mode": "required"},
        {"expected_version": 0, "encryption_mode": "unknown"},
    ):
        assert encrypted_client.patch(URL, json=payload).status_code == 422
    repository.update_device_configuration.assert_not_awaited()


def test_policy_patch_exposes_only_public_key_and_uses_compare_and_swap(
    encrypted_client, repository
):
    app.dependency_overrides[require_bearer_token] = admin
    repository.update_device_configuration.return_value = {
        "version": 1,
        "encryption_mode": "required",
    }
    response = encrypted_client.patch(
        URL, json={"expected_version": 0, "encryption_mode": "required"}
    )
    assert response.status_code == 200
    key = response.json()["encryption"]["public_key"]
    assert set(key) == {"kty", "crv", "x", "y", "kid", "use", "alg"}
    assert key["kid"] == "current"
    repository.update_device_configuration.assert_awaited_once_with(
        "user-123", "device-456", 0, "required"
    )
    repository.update_device_configuration.return_value = None
    assert (
        encrypted_client.patch(
            URL, json={"expected_version": 0, "encryption_mode": "tls_only"}
        ).status_code
        == 409
    )


def test_encryption_cannot_be_enabled_without_keys(authorized_client, repository):
    app.dependency_overrides[require_bearer_token] = admin
    response = authorized_client.patch(
        URL, json={"expected_version": 0, "encryption_mode": "optional"}
    )
    assert response.status_code == 503
    repository.update_device_configuration.assert_not_awaited()


def test_configuration_reports_capabilities_and_checks_version_and_key(
    encrypted_client, repository
):
    repository.get_device_configuration.return_value = {"version": 2, "encryption_mode": "required"}
    repository.report_device_configuration.return_value = True
    report = {"version": 2, "status": "applied", "supports_encryption": True, "key_id": "current"}
    response = encrypted_client.put(URL + "-status", json=report)
    assert response.status_code == 200
    assert response.json()["reported_at"]
    repository.report_device_configuration.assert_awaited_once()
    for change, expected in [
        ({"version": 1}, 409),
        ({"key_id": "previous"}, 422),
        ({"supports_encryption": False}, 422),
    ]:
        response = encrypted_client.put(URL + "-status", json={**report, **change})
        assert response.status_code == expected
    response = encrypted_client.put(
        URL + "-status", json={**report, "status": "unsupported", "supports_encryption": False}
    )
    assert response.status_code == 200
    repository.report_device_configuration.return_value = False
    assert encrypted_client.put(URL + "-status", json=report).status_code == 409


def test_policy_read_failure_is_explicit(authorized_client, repository):
    repository.get_device_configuration.side_effect = PersistenceError("private driver details")
    response = authorized_client.get(URL)
    assert response.status_code == 503
    assert "private driver details" not in response.text


def test_device_management_requires_authentication():
    client = TestClient(app)
    assert client.get(URL).status_code == 401
    assert client.patch(URL, json={}).status_code == 401
    assert client.put(URL + "-status", json={}).status_code == 401


def test_https_is_required_and_untrusted_forwarded_header_cannot_bypass(
    authorized_client, monkeypatch
):
    monkeypatch.setattr(settings, "require_https", True)
    assert authorized_client.get(URL, headers={"X-Forwarded-Proto": "https"}).status_code == 400
    assert authorized_client.get("https://testserver" + URL).status_code == 200
    assert authorized_client.get("/health").status_code == 200
