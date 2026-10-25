import asyncio
import gzip
import itertools
import json
from copy import deepcopy
from hashlib import sha256

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from app.core.auth import require_bearer_token
from app.core.openbao import KMSUnavailable
from app.db.mongo import PersistenceError
from app.main import app
from app.schemas.traffic import TrafficLogRequest
from app.schemas.workspace import RetentionPolicy, StoragePolicy, WorkspaceConfiguration
from app.services.traffic_service import normalize_traffic_log

URL = "/api/v1/workspace/configuration"
FLAGS = ("encrypt_request_body", "encrypt_authorization_header", "encrypt_cookies")
RETAIN_FLAGS = ("retain_request_body", "retain_authorization_header", "retain_cookies")


def operator():
    return {"sub": "operator", "permissions": ["manage:devices"]}


def test_workspace_configuration_requires_authentication():
    client = TestClient(app)
    assert client.get(URL).status_code == 401
    assert (
        client.patch(URL, json={"expected_version": 0, **dict.fromkeys(FLAGS, False)}).status_code
        == 401
    )


def test_workspace_configuration_requires_management_and_is_not_cached(
    authorized_client, repository
):
    assert authorized_client.get(URL).status_code == 403
    assert (
        authorized_client.patch(
            URL, json={"expected_version": 0, **dict.fromkeys(FLAGS, True)}
        ).status_code
        == 403
    )
    repository.get_workspace_configuration.assert_not_awaited()
    repository.update_workspace_configuration.assert_not_awaited()
    app.dependency_overrides[require_bearer_token] = operator
    response = authorized_client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == WorkspaceConfiguration().model_dump(mode="json")


@pytest.mark.parametrize(
    "invalid",
    [
        {"retention_enabled": "false"},
        {"suspicious_retention_enabled": 1},
        {"retention_days": 0},
        {"retention_days": True},
        {"retention_days": "7"},
        {"retention_days": 1.5},
        {"suspicious_retention_days": 24856},
        {"suspicious_retention_days": None},
    ],
)
def test_retention_settings_reject_invalid_values(authorized_client, repository, invalid):
    app.dependency_overrides[require_bearer_token] = operator
    response = authorized_client.patch(
        URL, json={"expected_version": 0, **dict.fromkeys(FLAGS, True), **invalid}
    )
    assert response.status_code == 422
    repository.update_workspace_configuration.assert_not_awaited()


def test_retention_settings_are_versioned_and_independent(authorized_client, repository):
    app.dependency_overrides[require_bearer_token] = operator
    policy = {
        **dict.fromkeys(FLAGS, True),
        **RetentionPolicy(
            retention_enabled=False,
            retention_days=14,
            suspicious_retention_enabled=True,
            suspicious_retention_days=365,
        ).model_dump(),
    }
    repository.update_workspace_configuration.return_value = {**policy, "version": 1}
    response = authorized_client.patch(URL, json={"expected_version": 0, **policy})
    assert response.status_code == 200
    assert response.json()["suspicious_retention_days"] == 365
    assert response.json()["retention_enabled"] is False
    repository.update_workspace_configuration.assert_awaited_once_with(0, policy)
    repository.update_workspace_configuration.side_effect = PersistenceError("private DDL details")
    failed = authorized_client.patch(URL, json={"expected_version": 1, **policy})
    assert failed.status_code == 503
    assert "private DDL" not in failed.text


def test_workspace_updates_are_versioned_strict_and_fail_closed(
    authorized_client, repository, storage_encryption, monkeypatch
):
    app.dependency_overrides[require_bearer_token] = operator
    policy = dict.fromkeys(FLAGS, True)
    repository.update_workspace_configuration.return_value = {**policy, "version": 1}
    response = authorized_client.patch(URL, json={**policy, "expected_version": 0})
    assert response.status_code == 200
    repository.update_workspace_configuration.assert_awaited_once_with(0, policy)
    repository.update_workspace_configuration.return_value = None
    assert authorized_client.patch(URL, json={**policy, "expected_version": 0}).status_code == 409
    for invalid in (
        {**policy, "encrypt_cookies": "false"},
        {**policy, "retain_request_body": "false"},
        {**policy, "retain_authorization_header": 0},
        {**policy, "retain_cookies": None},
        {**policy, "extra": True},
        {"encrypt_cookies": True},
    ):
        assert (
            authorized_client.patch(URL, json={**invalid, "expected_version": 1}).status_code == 422
        )

    def unavailable():
        raise KMSUnavailable("private storage details")

    monkeypatch.setattr(storage_encryption, "validate", unavailable)
    repository.update_workspace_configuration.reset_mock()
    failed = authorized_client.patch(URL, json={**policy, "expected_version": 1})
    assert failed.status_code == 503
    assert "private storage" not in failed.text
    repository.update_workspace_configuration.assert_not_awaited()
    repository.update_workspace_configuration.return_value = {
        **dict.fromkeys(FLAGS, False),
        "version": 2,
    }
    assert (
        authorized_client.patch(
            URL, json={**dict.fromkeys(FLAGS, False), "expected_version": 1}
        ).status_code
        == 200
    )


def test_discarded_categories_do_not_require_encryption_and_omitted_choices_are_preserved(
    authorized_client, repository, storage_encryption, monkeypatch, traffic_payload
):
    app.dependency_overrides[require_bearer_token] = operator
    configuration = WorkspaceConfiguration(**dict.fromkeys(RETAIN_FLAGS, False)).model_dump()
    repository.get_workspace_configuration.return_value = configuration
    repository.update_workspace_configuration.return_value = {**configuration, "version": 1}

    def unavailable(*args):
        raise KMSUnavailable("KMS must not be called for discarded values")

    monkeypatch.setattr(storage_encryption, "validate", unavailable)
    monkeypatch.setattr(storage_encryption, "encrypt", unavailable)
    response = authorized_client.patch(
        URL, json={**dict.fromkeys(FLAGS, True), "expected_version": 0}
    )
    assert response.status_code == 200
    assert all(response.json()[field] is False for field in RETAIN_FLAGS)
    repository.update_workspace_configuration.assert_awaited_once_with(
        0, dict.fromkeys(FLAGS, True)
    )
    response = authorized_client.post(
        "/api/v1/traffic-log-batches",
        content=gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200
    document = repository.insert_traffic_logs.call_args.args[0][0]
    assert "body" not in document
    assert "storage_encryption" not in document
    body = traffic_payload["body"].encode("utf-8")
    assert document["body_length"] == len(body)
    assert document["body_sha256"] == sha256(body).hexdigest()
    assert not document["request_headers"]


@pytest.mark.parametrize(
    "retained",
    [(False, True, True), (True, False, True), (True, True, False), (False, False, False)],
)
def test_discard_removes_values_but_preserves_body_metadata_before_the_publisher(
    retained, rich_traffic_payload, trusted_context, storage_encryption
):
    rich_traffic_payload["request_headers"].update(
        {"cOoKiE": "request-cookie", "Proxy-Authorization": "proxy-secret"}
    )
    rich_traffic_payload["response_headers"]["aUtHoRiZaTiOn"] = "response-secret"
    payload = TrafficLogRequest.model_validate(rich_traffic_payload)
    document = normalize_traffic_log(payload, "test-storage", trusted_context)
    policy = StoragePolicy(**dict(zip(RETAIN_FLAGS, retained, strict=True)))
    asyncio.run(storage_encryption.protect([document], [payload], policy))
    restored = storage_encryption.decrypt(document)
    body_retained, auth_retained, cookies_retained = retained
    assert restored.get("body") == (payload.body if body_retained else None)
    body = payload.body.encode("utf-8")
    assert document["body_length"] == len(body)
    assert document["body_sha256"] == sha256(body).hexdigest()
    for name, headers in (
        ("request_headers", payload.request_headers),
        ("response_headers", payload.response_headers),
    ):
        for key, value in headers.items():
            if key.lower() in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
                retain = (
                    auth_retained
                    if key.lower() in {"authorization", "proxy-authorization"}
                    else cookies_retained
                )
                assert (key in document[name]) is retain
                if retain:
                    assert document[name][key] == "[ENCRYPTED]"
                    assert restored[name][key] == value
                else:
                    assert key not in restored[name]
    assert document.get("storage_encryption", {}).get("fields", []) == [
        name
        for name, retain in (
            ("authorization", auth_retained),
            ("body", body_retained),
            ("cookies", cookies_retained),
        )
        if retain
    ]


@pytest.mark.parametrize("enabled", list(itertools.product([False, True], repeat=3)))
def test_each_storage_toggle_controls_only_its_fields_and_round_trips(
    enabled,
    rich_traffic_payload,
    trusted_context,
    storage_encryption,
):
    rich_traffic_payload["request_headers"].update(
        {
            "cOoKiE": "request-cookie",
            "Proxy-Authorization": "proxy-secret",
            "X-Api-Key": "still-redacted",
        }
    )
    payload = TrafficLogRequest.model_validate(rich_traffic_payload)
    document = normalize_traffic_log(payload, "test-storage", trusted_context)
    policy = StoragePolicy(**dict(zip(FLAGS, enabled, strict=True)))
    asyncio.run(storage_encryption.protect([document], [payload], policy))
    body_enabled, auth_enabled, cookies_enabled = enabled
    assert document.get("body") == (None if body_enabled else payload.body)
    assert document["request_headers"]["Authorization"] == (
        "[ENCRYPTED]" if auth_enabled else "private-credential"
    )
    assert document["request_headers"]["Proxy-Authorization"] == (
        "[ENCRYPTED]" if auth_enabled else "proxy-secret"
    )
    assert document["request_headers"]["cOoKiE"] == (
        "[ENCRYPTED]" if cookies_enabled else "request-cookie"
    )
    assert document["response_headers"]["Set-Cookie"] == (
        "[ENCRYPTED]" if cookies_enabled else "private-cookie"
    )
    assert document["request_headers"]["X-Api-Key"] == "[REDACTED]"
    assert ("storage_encryption" in document) is any(enabled)
    restored = storage_encryption.decrypt(document)
    assert restored["body"] == payload.body
    assert restored["request_headers"]["Authorization"] == "private-credential"
    assert restored["response_headers"]["Set-Cookie"] == "private-cookie"


@pytest.mark.parametrize("tampering", ["ciphertext", "device", "record", "batch", "fields"])
def test_ciphertext_is_authenticated_and_bound_to_its_record(
    tampering, traffic_payload, trusted_context, storage_encryption
):
    payload = TrafficLogRequest.model_validate(traffic_payload)
    document = normalize_traffic_log(payload, "test-storage", trusted_context)
    asyncio.run(storage_encryption.protect([document], [payload], StoragePolicy()))
    document = deepcopy(document)
    if tampering == "ciphertext":
        value = document["storage_encryption"]["ciphertext"]
        document["storage_encryption"]["ciphertext"] = value[:-4] + "AAAA"
    elif tampering == "device":
        document["metadata"]["device_id"] = "other-device"
    elif tampering == "record":
        document["_id"] = "other-record"
    elif tampering == "batch":
        document["batch_id"] = "other-batch"
    else:
        document["storage_encryption"]["fields"] = ["body"]
    with pytest.raises(KMSUnavailable):
        storage_encryption.decrypt(document)


def test_ingestion_fails_without_accepting_plaintext_when_configuration_or_kms_fails(
    authorized_client,
    repository,
    traffic_payload,
    storage_encryption,
    monkeypatch,
):
    wire = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}

    async def unavailable(entries):
        raise KMSUnavailable("private storage details")

    monkeypatch.setattr(storage_encryption, "encrypt", unavailable)
    response = authorized_client.post("/api/v1/traffic-log-batches", content=wire, headers=headers)
    assert response.status_code == 503
    assert "private storage" not in response.text
    repository.insert_traffic_logs.assert_not_awaited()
    repository.get_workspace_configuration.side_effect = PersistenceError(
        "private configuration details"
    )
    assert (
        authorized_client.post(
            "/api/v1/traffic-log-batches", content=wire, headers=headers
        ).status_code
        == 503
    )
    repository.insert_traffic_logs.assert_not_awaited()


def test_encrypted_body_is_only_decrypted_for_authorized_details(
    authorized_client,
    repository,
    traffic_payload,
    storage_encryption,
    trusted_context,
    monkeypatch,
):
    payload = TrafficLogRequest.model_validate(traffic_payload)
    document = normalize_traffic_log(payload, "test-storage", trusted_context)
    asyncio.run(storage_encryption.protect([document], [payload], WorkspaceConfiguration()))
    repository.get_traffic_log.return_value = document
    repository.list_traffic_logs.return_value = [document]
    detail = authorized_client.get(f"/api/v1/traffic-logs/{document['_id']}")
    assert detail.status_code == 200
    assert detail.json()["body"] == payload.body
    assert detail.json()["request_headers"]["Authorization"] == "[REDACTED]"
    assert detail.headers["cache-control"] == "no-store"
    listed = authorized_client.get("/api/v1/traffic-logs")
    assert "body" not in listed.json()["items"][0]

    def unavailable(document):
        raise KMSUnavailable("private storage details")

    monkeypatch.setattr(storage_encryption, "decrypt", unavailable)
    assert authorized_client.get("/api/v1/traffic-logs").status_code == 200
    assert authorized_client.get(f"/api/v1/traffic-logs/{document['_id']}").status_code == 503
    repository.get_traffic_log.return_value = None
    assert authorized_client.get(f"/api/v1/traffic-logs/{document['_id']}").status_code == 404


def test_partial_transit_success_rejects_the_entire_batch(
    authorized_client,
    repository,
    traffic_payload,
    storage_encryption,
    monkeypatch,
):
    original = storage_encryption.respond

    def respond(request):
        response = original(request)
        if "/encrypt/" in request.url.path:
            payload = response.json()
            payload["data"]["batch_results"][-1] = {"error": "private encryption failure"}
            return httpx.Response(200, json=payload)
        return response

    monkeypatch.setattr(storage_encryption, "respond", respond)
    wire = gzip.compress(json.dumps({"logs": [traffic_payload, traffic_payload]}).encode())
    response = authorized_client.post(
        "/api/v1/traffic-log-batches",
        content=wire,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 503
    assert "private encryption" not in response.text
    repository.insert_traffic_logs.assert_not_awaited()
