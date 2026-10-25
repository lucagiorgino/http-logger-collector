from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.db.mongo import DuplicateDeviceError, PersistenceError
from app.main import app

URL = "/api/v1/devices/device-456/configuration"
REGISTRY = "/api/v1/devices"


def admin():
    return {"sub": "user-123", "permissions": ["manage:devices"]}


def test_default_policy_is_device_scoped_and_not_cached(authorized_client, repository):
    response = authorized_client.get(URL)
    assert response.status_code == 200
    assert response.json()["encryption_mode"] == "tls_only"
    assert response.json()["version"] == 0
    assert response.json()["encryption"] is None
    assert response.headers["cache-control"] == "no-store"
    repository.get_device_configuration.assert_awaited_once_with("device-456")


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
    repository.update_device_configuration.assert_awaited_once_with("device-456", 0, "required")
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


def test_registration_saves_initial_policy_and_returns_resource(encrypted_client, repository):
    app.dependency_overrides[require_bearer_token] = admin
    registered_at = datetime.now(UTC)
    repository.register_device.return_value = {
        "name": "Operator laptop",
        "registered_at": registered_at,
        "version": 1,
        "encryption_mode": "required",
    }
    response = encrypted_client.post(
        REGISTRY,
        json={"device_id": "device-456", "name": "Operator laptop", "encryption_mode": "required"},
    )
    assert response.status_code == 201
    assert response.headers["location"] == REGISTRY + "/device-456"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["name"] == "Operator laptop"
    assert response.json()["registered_at"] == registered_at.isoformat().replace("+00:00", "Z")
    assert response.json()["version"] == 1
    assert "d" not in response.json()["encryption"]["public_key"]
    repository.register_device.assert_awaited_once_with("device-456", "Operator laptop", "required")


def test_registration_requires_management_permission_and_keys(authorized_client, repository):
    payload = {"device_id": "device-456", "name": "Laptop", "encryption_mode": "required"}
    assert authorized_client.post(REGISTRY, json=payload).status_code == 403
    repository.register_device.assert_not_awaited()
    app.dependency_overrides[require_bearer_token] = admin
    assert authorized_client.post(REGISTRY, json=payload).status_code == 503
    repository.register_device.assert_not_awaited()


def test_duplicate_registration_is_a_bad_request(authorized_client, repository):
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "user-123",
        "scope": "openid manage:devices",
    }
    repository.register_device.side_effect = DuplicateDeviceError("private-driver-details")
    response = authorized_client.post(REGISTRY, json={"device_id": "device-456", "name": "Laptop"})
    assert response.status_code == 400
    assert "device_id or name already exists" in response.json()["detail"]
    assert "private-driver-details" not in response.text
    repository.register_device.assert_awaited_once_with("device-456", "Laptop", "tls_only")


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "x" * 129},
        {"name": "Laptop\nforged"},
        {"device_id": " "},
        {"device_id": "a/b"},
        {"device_id": ".."},
        {"device_id": "x" * 256},
        {"device_id": {"$ne": None}},
        {"expected_version": True},
        {"expected_version": -1},
        {"user_id": "forged-user"},
        {"registered_at": "2025-01-15T00:00:00Z"},
        {"encryption_mode": "off"},
    ],
)
def test_invalid_registration_never_reaches_storage(authorized_client, repository, changes):
    app.dependency_overrides[require_bearer_token] = admin
    response = authorized_client.post(
        REGISTRY, json={"device_id": "device-456", "name": "Laptop"} | changes
    )
    assert response.status_code == 422
    repository.register_device.assert_not_awaited()


def test_operator_inventory_is_global_paginated_and_not_cached(authorized_client, repository):
    app.dependency_overrides[require_bearer_token] = admin
    documents = [
        {
            "_id": device_id,
            "name": device_id,
            "registered_at": datetime.now(UTC),
            "version": 1,
            "encryption_mode": "tls_only",
        }
        for device_id in ("device-a", "device-b", "device-c")
    ]
    repository.list_devices.return_value = documents
    response = authorized_client.get(REGISTRY, params={"limit": 2})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [item["device_id"] for item in response.json()["items"]] == ["device-a", "device-b"]
    assert response.json()["next_after"] == "device-b"
    repository.list_devices.assert_awaited_once_with(device_id=None, after=None, limit=3)
    repository.list_devices.return_value = documents[2:]
    assert (
        authorized_client.get(REGISTRY, params={"after": "device-b"}).json()["next_after"] is None
    )
    assert repository.list_devices.call_args.kwargs["after"] == "device-b"


def test_device_lookup_distinguishes_persisted_records_from_default_policy(
    authorized_client, repository
):
    assert authorized_client.get(REGISTRY + "/device-456").status_code == 404
    repository.get_device_configuration.assert_awaited_once_with("device-456")
    repository.get_device_configuration.return_value = {
        "_id": "device-456",
        "name": "Laptop",
        "registered_at": datetime.now(UTC),
        "version": 1,
        "encryption_mode": "tls_only",
    }
    assert authorized_client.get(REGISTRY + "/device-456").json()["name"] == "Laptop"


def test_inventory_and_details_include_unnamed_version_zero_device(authorized_client, repository):
    document = {"_id": "device-456", "version": 0, "encryption_mode": "tls_only"}
    repository.list_devices.return_value = [document]
    repository.get_device_configuration.return_value = document
    page = authorized_client.get(REGISTRY).json()
    assert page["items"] == [
        {
            "device_id": "device-456",
            "name": None,
            "registered_at": None,
            "version": 0,
            "encryption_mode": "tls_only",
        }
    ]
    detail = authorized_client.get(REGISTRY + "/device-456")
    assert detail.status_code == 200
    assert detail.json()["name"] is None
    assert detail.json()["registered_at"] is None
    assert detail.json()["version"] == 0


def test_registration_and_inventory_errors_are_explicit(authorized_client, repository):
    app.dependency_overrides[require_bearer_token] = admin
    repository.list_devices.side_effect = PersistenceError("private-database-error")
    repository.register_device.side_effect = PersistenceError("private-database-error")
    for response in (
        authorized_client.get(REGISTRY),
        authorized_client.post(REGISTRY, json={"device_id": "device-456", "name": "Laptop"}),
    ):
        assert response.status_code == 503
        assert "private-database-error" not in response.text


def test_inventory_registration_and_lookup_require_authentication():
    client = TestClient(app)
    assert client.get(REGISTRY).status_code == 401
    assert client.get(REGISTRY + "/device-456").status_code == 401
    assert client.post(REGISTRY, json={"device_id": "device", "name": "Laptop"}).status_code == 401


def test_agent_cannot_read_or_acknowledge_another_device(authorized_client, repository):
    for suffix in ("", "/configuration"):
        assert authorized_client.get(REGISTRY + "/another-device" + suffix).status_code == 403
    report = {"version": 0, "status": "applied", "supports_encryption": False}
    assert (
        authorized_client.put(
            REGISTRY + "/another-device/configuration-status", json=report
        ).status_code
        == 403
    )
    repository.get_device_configuration.assert_not_awaited()
    repository.report_device_configuration.assert_not_awaited()
    assert authorized_client.get(REGISTRY).status_code == 200
    repository.list_devices.assert_awaited_once_with(device_id="device-456", after=None, limit=51)


def test_operator_can_read_any_device_but_cannot_impersonate_its_acknowledgement(
    authorized_client, repository
):
    app.dependency_overrides[require_bearer_token] = admin
    assert authorized_client.get(REGISTRY + "/another-device/configuration").status_code == 200
    repository.get_device_configuration.assert_awaited_once_with("another-device")
    report = {"version": 0, "status": "applied", "supports_encryption": False}
    assert (
        authorized_client.put(
            REGISTRY + "/another-device/configuration-status", json=report
        ).status_code
        == 403
    )
