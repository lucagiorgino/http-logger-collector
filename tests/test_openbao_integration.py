import asyncio
import gzip
import os
import sys
from uuid import uuid4

import pytest
from joserfc import jwe
from joserfc.jwk import ECKey

from app.core.config import settings
from app.core.encryption import InvalidEnvelope, parse_keyset
from app.core.openbao import AsyncOpenBaoClient, KMSUnavailable, OpenBaoClient, read_credential
from app.core.storage_encryption import StorageEncryption
from app.kms import configure_runtime_policy, initialize, main
from app.schemas.traffic import TrafficLogRequest
from app.schemas.workspace import StoragePolicy
from app.services.traffic_service import ClientIdentity, IngestionContext, normalize_traffic_log


@pytest.mark.skipif(
    not os.getenv("OPENBAO_TEST_ADDR") or not os.getenv("OPENBAO_TEST_ADMIN_TOKEN_FILE"),
    reason="Set OPENBAO_TEST_ADDR and OPENBAO_TEST_ADMIN_TOKEN_FILE for real OpenBao",
)
def test_real_transit_envelope_rotation_cas_and_runtime_permissions(monkeypatch):
    suffix = uuid4().hex
    key_name = "test-" + suffix
    policy_name = "test-" + suffix
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "openbao_addr", os.environ["OPENBAO_TEST_ADDR"])
    monkeypatch.setattr(settings, "openbao_transit_key", key_name)
    storage_key_name = "storage-" + suffix
    monkeypatch.setattr(settings, "openbao_storage_key", storage_key_name)
    monkeypatch.setattr(settings, "openbao_kv_path", key_name)
    root = read_credential(os.environ["OPENBAO_TEST_ADMIN_TOKEN_FILE"])
    runtime_token = None

    def command(action, *arguments):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "kms",
                action,
                "--admin-token-file",
                os.environ["OPENBAO_TEST_ADMIN_TOKEN_FILE"],
                *arguments,
            ],
        )
        main()

    with OpenBaoClient(root) as admin:
        try:
            initialize(admin, "first")
            configure_runtime_policy(admin, policy_name)
            record = admin.read_wrapped()
            assert set(record["data"]) == {"ciphertext"}
            assert record["data"]["ciphertext"].startswith("vault:v1:")
            metadata = admin.request("GET", f"{settings.openbao_transit_mount}/keys/{key_name}")[
                "data"
            ]
            assert metadata["exportable"] is False
            assert metadata["allow_plaintext_backup"] is False
            response = admin.request(
                "POST",
                "auth/token/create",
                {
                    "policies": [policy_name],
                    "no_default_policy": True,
                    "ttl": "1h",
                },
            )
            runtime_token = response["auth"]["client_token"]
            with OpenBaoClient(runtime_token) as runtime:
                storage = StorageEncryption()
                monkeypatch.setattr(storage, "client", lambda: OpenBaoClient(runtime_token))
                monkeypatch.setattr(
                    storage, "async_client", lambda: AsyncOpenBaoClient(runtime_token)
                )
                payload = TrafficLogRequest(
                    timestamp="2026-01-15T10:00:00Z",
                    method="POST",
                    url="https://example.test",
                    status_code=200,
                    request_headers={"Authorization": "test-secret", "Cookie": "test-cookie"},
                    response_headers={"Set-Cookie": "test-response-cookie"},
                    body="x" * 900_000,
                )
                context = IngestionContext(ClientIdentity("storage-test", None), "127.0.0.1")
                documents = [
                    normalize_traffic_log(payload, "storage-batch", context) for _ in range(2)
                ]
                asyncio.run(storage.protect(documents, [payload, payload], StoragePolicy()))
                first, second = [
                    document["storage_encryption"]["ciphertext"] for document in documents
                ]
                assert first.startswith("vault:v1:") and second.startswith("vault:v1:")
                assert first != second
                assert storage.decrypt(documents[0])["logdata"]["body"] == payload.body
                small_payload = payload.model_copy(update={"body": "x" * 1024})
                maximum_batch = [
                    normalize_traffic_log(small_payload, "maximum-batch", context)
                    for _ in range(500)
                ]
                asyncio.run(storage.protect(maximum_batch, [small_payload] * 500, StoragePolicy()))
                for index in (0, 99, 100, 499):
                    assert (
                        storage.decrypt(maximum_batch[index])["logdata"]["body"]
                        == small_payload.body
                    )
                tampered = {
                    **documents[0],
                    "ingestion": {**documents[0]["ingestion"], "batch_id": "different-batch"},
                }
                with pytest.raises(KMSUnavailable):
                    storage.decrypt(tampered)
                command("rotate-storage")
                assert storage.decrypt(documents[0])["logdata"]["body"] == payload.body
                current = normalize_traffic_log(payload, "storage-batch", context)
                asyncio.run(storage.protect([current], [payload], StoragePolicy()))
                assert current["storage_encryption"]["ciphertext"].startswith("vault:v2:")
                assert (
                    storage.decrypt(current)["logdata"]["request_headers"]["Authorization"]
                    == "test-secret"
                )
                keys = parse_keyset(runtime.load_keyset())
                compressed = gzip.compress(b'{"logs": []}')
                envelope = jwe.encrypt_compact(
                    {
                        "alg": keys.algorithm,
                        "enc": keys.encryption,
                        "kid": keys.active_key_id,
                        "cty": "application/gzip",
                    },
                    compressed,
                    ECKey.import_key(keys.public_key),
                    registry=keys.registry,
                )
                assert keys.decrypt(envelope.encode()) == compressed
                for method, path, payload in [
                    ("GET", "sys/mounts", None),
                    ("POST", runtime.kv_path, {"data": {"ciphertext": "invalid"}}),
                    ("POST", f"{settings.openbao_transit_mount}/keys/{key_name}/rotate", {}),
                    ("POST", f"{settings.openbao_transit_mount}/encrypt/{key_name}", {}),
                    (
                        "POST",
                        f"{settings.openbao_transit_mount}/keys/{storage_key_name}/rotate",
                        {},
                    ),
                    (
                        "GET",
                        f"{settings.openbao_transit_mount}/export/encryption-key/{storage_key_name}",
                        None,
                    ),
                ]:
                    with pytest.raises(KMSUnavailable):
                        runtime.request(method, path, payload)
                command("stage", "--key-id", "second")
                staged = parse_keyset(runtime.load_keyset())
                assert staged.active_key_id == "first"
                assert set(staged.keys) == {"first", "second"}
                command("activate", "--key-id", "second")
                with pytest.raises(KMSUnavailable):
                    admin.store_keyset(admin.unwrap(record), record["metadata"]["version"])
                rotated = parse_keyset(runtime.load_keyset())
                assert rotated.active_key_id == "second"
                assert rotated.decrypt(envelope.encode()) == compressed
                command("rotate-kek")
                assert admin.read_wrapped()["data"]["ciphertext"].startswith("vault:v2:")
                assert parse_keyset(runtime.load_keyset()).public_key == rotated.public_key
                with pytest.raises(ValueError, match="active key"):
                    command("retire", "--key-id", "second")
                command("retire", "--key-id", "first")
                with pytest.raises(InvalidEnvelope):
                    parse_keyset(runtime.load_keyset()).decrypt(envelope.encode())
                command("rotate", "--key-id", "third")
                assert parse_keyset(runtime.load_keyset()).active_key_id == "third"
        finally:
            if runtime_token:
                admin.request("POST", "auth/token/revoke", {"token": runtime_token})
            admin.request(
                "DELETE", f"{settings.openbao_kv_mount}/metadata/{key_name}", missing_ok=True
            )
            admin.request(
                "POST",
                f"{settings.openbao_transit_mount}/keys/{key_name}/config",
                {"deletion_allowed": True},
            )
            admin.request("DELETE", f"{settings.openbao_transit_mount}/keys/{key_name}")
            admin.request(
                "POST",
                f"{settings.openbao_transit_mount}/keys/{storage_key_name}/config",
                {"deletion_allowed": True},
            )
            admin.request("DELETE", f"{settings.openbao_transit_mount}/keys/{storage_key_name}")
            admin.request("DELETE", f"sys/policies/acl/{policy_name}")
