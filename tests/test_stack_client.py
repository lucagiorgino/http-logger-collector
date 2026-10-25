import gzip
import json
import os
from hashlib import sha256
from threading import Lock, get_ident
from uuid import uuid4

import httpx2 as httpx
import pytest
from joserfc import jwe
from joserfc.jwk import ECKey
from pydantic import ValidationError

from integration_tests.stack_client import (
    LOGS_PER_DEVICE,
    StackSettings,
    StackTestFailure,
    load_settings,
    run_stack,
)


@pytest.fixture(autouse=True)
def isolated_stack_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("STACK_TEST_"):
            monkeypatch.delenv(name)


@pytest.fixture
def stack_settings():
    return StackSettings(
        token_url="https://auth.example.test/oauth/token",
        audience="https://test-api",
        plaintext={
            "client_id": "plain-client",
            "client_secret": "plain-secret",
            "device_id": "configured-plaintext",
            "encrypted": False,
        },
        encrypted={
            "client_id": "jwe-client",
            "client_secret": "jwe-secret",
            "device_id": "configured-encrypted",
            "encrypted": True,
        },
        visibility_timeout=0.5,
    )


def fake_stack(
    settings,
    *,
    algorithm="ECDH-ES+A256KW",
    encryption="A256GCM",
    failure=None,
    encrypted_mode="required",
    plaintext_mode="tls_only",
    pagination=False,
):
    devices = {
        "plain-client": settings.plaintext.device_id or "plain-client",
        "jwe-client": settings.encrypted.device_id or "jwe-client",
    }
    key = ECKey.generate_key(
        "P-256", parameters={"kid": "server-key", "alg": algorithm, "use": "enc"}
    )
    registry = jwe.JWERegistry(algorithms=[algorithm, encryption])
    configurations = {
        devices["plain-client"]: {
            "version": 2,
            "encryption_mode": plaintext_mode,
            "encryption": None,
        },
        devices["jwe-client"]: {
            "version": 4,
            "encryption_mode": encrypted_mode,
            "encryption": {
                "algorithm": algorithm,
                "encryption": encryption,
                "curve": "P-256",
                "public_key": key.as_dict(private=False),
            },
        },
    }
    records = {}
    state = {
        "oauth_clients": {},
        "uploads": set(),
        "upload_count": 0,
        "rejections": 0,
        "reads": {},
        "configuration_methods": set(),
        "paged_reads": 0,
        "status_reads": {},
    }
    lock = Lock()

    def respond(request):
        with lock:
            if request.url.host == "auth.example.test":
                payload = json.loads(request.content)
                identity = payload["client_id"]
                credential = (
                    settings.plaintext if identity == "plain-client" else settings.encrypted
                )
                assert payload["client_secret"] == credential.client_secret.get_secret_value()
                assert payload["grant_type"] == "client_credentials"
                assert payload["audience"] == settings.audience
                assert "scope" not in payload
                assert "Authorization" not in request.headers
                state["oauth_clients"][identity] = get_ident()
                if failure == "token":
                    return httpx.Response(401, json={"error": "sensitive-response"})
                return httpx.Response(200, json={"access_token": identity, "token_type": "Bearer"})
            assert request.url.scheme == "http"
            assert request.url.host == "localhost"
            assert request.url.port == 8080
            client_id = request.headers["Authorization"].removeprefix("Bearer ")
            path = request.url.path
            if path == "/api/v1/me":
                return httpx.Response(
                    200,
                    json={
                        "device_id": "mismatched-device"
                        if failure == "identity"
                        else devices[client_id],
                        "client_id": client_id,
                    },
                )
            if path.startswith("/api/v1/devices/"):
                device = path.split("/")[4]
                if device != devices[client_id]:
                    return httpx.Response(403, json={"detail": "Device access denied"})
                configuration = configurations[device]
                assert request.method in {"GET", "PUT"}, "Agents must not change device policies"
                state["configuration_methods"].add(request.method)
                if request.method == "PUT":
                    report = json.loads(request.content)
                    assert report["version"] == configuration["version"]
                    assert report["supports_encryption"] is (client_id == "jwe-client")
                    assert report["key_id"] == ("server-key" if client_id == "jwe-client" else None)
                return httpx.Response(200, json=configuration)
            if path.startswith("/api/v1/traffic-log-uploads/"):
                attempt = state["status_reads"].get(path, 0)
                state["status_reads"][path] = attempt + 1
                return httpx.Response(
                    200,
                    json={
                        "status": failure
                        if failure in {"parse_failed", "load_failed"}
                        else ("received" if failure == "pending" or not attempt else "stored"),
                        "accepted_count": LOGS_PER_DEVICE,
                        "error_code": "sensitive-response",
                    },
                )
            if path == "/api/v1/traffic-log-uploads":
                encrypted = request.headers["Content-Type"] == "application/jose"
                if client_id == "jwe-client" and encrypted_mode == "required" and not encrypted:
                    state["rejections"] += 1
                    return httpx.Response(403, json={"detail": "Encryption required"})
                if failure == "storage":
                    return httpx.Response(503, json={"detail": "sensitive-response"})
                if encrypted:
                    assert "Content-Encoding" not in request.headers
                    compressed = jwe.decrypt_compact(
                        request.content, key, registry=registry
                    ).plaintext
                else:
                    assert request.headers["Content-Encoding"] == "gzip"
                    compressed = request.content
                logs = json.loads(gzip.decompress(compressed))["logs"]
                batch_id = uuid4().hex
                for log in logs:
                    assert (
                        not {"user_id", "device_id", "client_id", "source_ip", "metadata"}
                        & log.keys()
                    )
                    body = log.pop("body").encode()
                    log.update(
                        device_id=devices[client_id],
                        client_id=client_id,
                        source_ip="203.0.113.10",
                        id=uuid4().hex[:24],
                        batch_id=batch_id,
                        method="GET",
                        headers={"Authorization": "[REDACTED]", "Cookie": "[REDACTED]"},
                        body_length=len(body),
                        body_sha256=sha256(body).hexdigest(),
                    )
                    if failure == "redaction":
                        log["headers"]["Authorization"] = "unredacted-placeholder"
                    records.setdefault(log["device_id"], []).append(log)
                state["uploads"].add((client_id, encrypted))
                state["upload_count"] += 1
                return httpx.Response(
                    202,
                    json={"upload_id": batch_id, "duplicate": False, "status": "received"},
                )
            if path == "/api/v1/traffic-logs":
                device = request.url.params["device_id"]
                if device != devices[client_id] and failure != "isolation":
                    return httpx.Response(403, json={"detail": "Device access denied"})
                lookup = device
                attempt = state["reads"].get(lookup, 0)
                state["reads"][lookup] = attempt + 1
                visible = records.get(lookup, [])
                if failure == "isolation":
                    visible = [
                        record
                        for candidate, documents in records.items()
                        if candidate == device
                        for record in documents
                    ]
                if (lookup in records and not attempt) or failure == "visibility":
                    visible = []
                if pagination and lookup in records:
                    if "cursor" not in request.url.params:
                        old_record = {**records[lookup][0], "batch_id": "previous-batch"}
                        return httpx.Response(
                            200, json={"items": [old_record], "next_cursor": "own-batch-page"}
                        )
                    assert request.url.params["cursor"] == "own-batch-page"
                    state["paged_reads"] += 1
                return httpx.Response(200, json={"items": visible, "next_cursor": None})
            if path.startswith("/api/v1/traffic-logs/"):
                return httpx.Response(404, json={"detail": "Not found"})
            raise AssertionError("Unexpected endpoint")

    return httpx.MockTransport(respond), state


@pytest.mark.parametrize(
    ("algorithm", "encryption"),
    [("ECDH-ES+A256KW", "A256GCM"), ("ECDH-ES+A128KW", "A128GCM")],
)
def test_two_threaded_clients_use_real_gzip_and_advertised_jwe(
    stack_settings, algorithm, encryption
):
    transport, state = fake_stack(stack_settings, algorithm=algorithm, encryption=encryption)
    run_stack(stack_settings, transport=transport)
    assert set(state["oauth_clients"]) == {"plain-client", "jwe-client"}
    assert len(set(state["oauth_clients"].values())) == 2
    assert state["uploads"] == {("plain-client", False), ("jwe-client", True)}
    assert state["rejections"] == 1
    assert state["configuration_methods"] == {"GET", "PUT"}
    assert sorted(state["status_reads"].values()) == [2, 2]


def test_optional_policy_is_respected_without_changing_it(stack_settings):
    transport, state = fake_stack(stack_settings, encrypted_mode="optional")
    run_stack(stack_settings, transport=transport)
    assert state["uploads"] == {("plain-client", False), ("jwe-client", True)}
    assert state["rejections"] == 0


@pytest.mark.parametrize(
    ("plaintext_mode", "encrypted_mode"),
    [("required", "required"), ("tls_only", "tls_only")],
)
def test_incompatible_policy_requires_external_configuration(
    stack_settings, plaintext_mode, encrypted_mode
):
    transport, state = fake_stack(
        stack_settings, plaintext_mode=plaintext_mode, encrypted_mode=encrypted_mode
    )
    with pytest.raises(StackTestFailure, match="test never changes device policies"):
        run_stack(stack_settings, transport=transport)
    assert "PATCH" not in state["configuration_methods"]


def test_stable_devices_can_be_reused_without_counting_previous_batches(stack_settings):
    transport, state = fake_stack(stack_settings)
    run_stack(stack_settings, transport=transport)
    run_stack(stack_settings, transport=transport)
    assert state["upload_count"] == 4


def test_readback_follows_cursors_to_find_its_own_batch(stack_settings):
    transport, state = fake_stack(stack_settings, pagination=True)
    run_stack(stack_settings, transport=transport)
    assert state["paged_reads"] == 2


def test_device_identity_is_discovered_from_the_server(stack_settings):
    stack_settings.plaintext.device_id = None
    stack_settings.encrypted.device_id = None
    transport, state = fake_stack(stack_settings)
    run_stack(stack_settings, transport=transport)
    assert state["upload_count"] == 2


def test_configured_device_cannot_override_the_authenticated_identity(stack_settings):
    transport, state = fake_stack(stack_settings, failure="identity")
    with pytest.raises(StackTestFailure, match="does not match the JWT-bound identity"):
        run_stack(stack_settings, transport=transport)
    assert state["upload_count"] == 0
    assert state["configuration_methods"] == set()


@pytest.mark.parametrize(
    "failure", ["token", "storage", "visibility", "pending", "parse_failed", "load_failed"]
)
def test_stack_errors_fail_without_echoing_credentials(stack_settings, failure):
    transport, _ = fake_stack(stack_settings, failure=failure)
    with pytest.raises(StackTestFailure) as error:
        run_stack(stack_settings, transport=transport)
    for secret in ("plain-secret", "jwe-secret", "sensitive-response"):
        assert secret not in str(error.value)
    if failure == "pending":
        assert "parser and loader workers are running" in str(error.value)


@pytest.mark.parametrize("failure", ["redaction", "isolation"])
def test_successful_receipts_do_not_hide_persisted_data_failures(stack_settings, failure):
    transport, _ = fake_stack(stack_settings, failure=failure)
    with pytest.raises(AssertionError):
        run_stack(stack_settings, transport=transport)


def test_profile_requires_distinct_clients_and_hides_secrets(tmp_path, monkeypatch):
    profile = tmp_path / "stack.env"
    profile.write_text(
        "STACK_TEST_ENABLED=true\n"
        "STACK_TEST_TOKEN_URL=https://auth.example.test/oauth/token\nSTACK_TEST_AUDIENCE=api\n"
        "STACK_TEST_PLAINTEXT_CLIENT_ID=first\nSTACK_TEST_PLAINTEXT_CLIENT_SECRET=private-one\n"
        "STACK_TEST_ENCRYPTED_CLIENT_ID=second\nSTACK_TEST_ENCRYPTED_CLIENT_SECRET=private-two\n"
    )
    monkeypatch.setenv("STACK_TEST_ENV_FILE", str(profile))
    loaded = load_settings()
    assert loaded.base_url == "http://localhost:8080"
    monkeypatch.setenv("STACK_TEST_BASE_URL", "https://staging.example.test")
    assert load_settings().base_url == "https://staging.example.test"
    monkeypatch.delenv("STACK_TEST_BASE_URL")
    assert "private-one" not in repr(loaded)
    assert "private-two" not in repr(loaded)
    assert loaded.plaintext.device_id is None
    assert loaded.encrypted.device_id is None
    monkeypatch.setenv("STACK_TEST_PLAINTEXT_DEVICE_ID", "stack-test-plaintext")
    monkeypatch.setenv("STACK_TEST_ENCRYPTED_DEVICE_ID", "stack-test-plaintext")
    with pytest.raises(ValueError, match="different device ID"):
        load_settings()
    monkeypatch.setenv("STACK_TEST_ENCRYPTED_DEVICE_ID", "configured-device")
    assert load_settings().encrypted.device_id == "configured-device"
    monkeypatch.setenv("STACK_TEST_ENCRYPTED_CLIENT_ID", "first")
    with pytest.raises(ValueError, match="different client ID"):
        load_settings()
    monkeypatch.setenv("STACK_TEST_ENABLED", "false")
    assert load_settings() is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("token_url", "http://auth.example.test/oauth/token"),
        ("token_url", "https://user:private-secret@auth.example.test/oauth/token"),
        ("base_url", "http://remote.example.test"),
    ],
)
def test_oauth_credentials_require_safe_urls(stack_settings, name, value):
    with pytest.raises(ValidationError) as error:
        StackSettings.model_validate({**stack_settings.model_dump(), name: value})
    assert "private-secret" not in str(error.value)


def test_explicit_missing_profile_fails_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setenv("STACK_TEST_ENV_FILE", str(tmp_path / "missing.env"))
    with pytest.raises(ValueError, match="STACK_TEST_ENV_FILE"):
        load_settings()
