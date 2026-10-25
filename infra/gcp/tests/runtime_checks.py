import asyncio
import base64
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from google.api_core.exceptions import PermissionDenied
from google.cloud import kms_v1
from starlette.responses import JSONResponse

from app.core.storage_encryption import StorageEncryption
from app.schemas.traffic import TrafficLogRequest
from app.schemas.workspace import StoragePolicy
from infra.gcp.runtime import configuration, openbao, provision_keyset, spool


class FakeKMS:
    def __init__(self):
        self.master = AESGCM(AESGCM.generate_key(bit_length=256))
        self.bad_checksum = False

    def get_crypto_key(self, **kwargs):
        return kms_v1.CryptoKey(purpose="ENCRYPT_DECRYPT")

    def encrypt(self, *, request, **kwargs):
        assert request["plaintext_crc32c"] == openbao.checksum(request["plaintext"])
        aad = request["additional_authenticated_data"]
        assert request["additional_authenticated_data_crc32c"] == openbao.checksum(aad)
        nonce = os.urandom(12)
        ciphertext = nonce + self.master.encrypt(nonce, request["plaintext"], aad)
        return kms_v1.EncryptResponse(
            ciphertext=ciphertext,
            ciphertext_crc32c=0 if self.bad_checksum else openbao.checksum(ciphertext),
            verified_plaintext_crc32c=True,
            verified_additional_authenticated_data_crc32c=True,
        )

    def decrypt(self, *, request, **kwargs):
        ciphertext = request["ciphertext"]
        assert request["ciphertext_crc32c"] == openbao.checksum(ciphertext)
        value = self.master.decrypt(
            ciphertext[:12], ciphertext[12:], request["additional_authenticated_data"]
        )
        return kms_v1.DecryptResponse(plaintext=value, plaintext_crc32c=openbao.checksum(value))


class AsyncFakeKMS:
    def __init__(self, sync):
        self.sync = sync

    async def get_crypto_key(self, **kwargs):
        return self.sync.get_crypto_key(**kwargs)

    async def encrypt(self, **kwargs):
        await asyncio.sleep(0)
        return self.sync.encrypt(**kwargs)


@pytest.fixture
def cloud_storage(monkeypatch):
    monkeypatch.setenv("GCP_STORAGE_KEY", "projects/p/locations/r/keyRings/k/cryptoKeys/storage")
    monkeypatch.setattr(openbao.settings, "openbao_transit_mount", "transit")
    monkeypatch.setattr(openbao.settings, "openbao_storage_key", "log-storage")
    fake = FakeKMS()
    sync = openbao.OpenBaoClient()
    sync.kms = fake
    asynchronous = openbao.AsyncOpenBaoClient()
    asynchronous.kms = AsyncFakeKMS(fake)

    @contextmanager
    def client():
        yield sync

    storage = StorageEncryption(asynchronous)
    monkeypatch.setattr(storage, "client", client)
    return storage, fake


def protected_document():
    return {
        "_id": ObjectId(),
        "metadata": {"device_id": "device"},
        "ingestion": {"batch_id": "batch"},
        "logdata": {"request_headers": {}, "response_headers": {}},
    }


def test_cloud_envelope_interoperates_with_existing_storage_for_large_bodies(cloud_storage):
    storage, _ = cloud_storage
    document = protected_document()
    body = "sensitive" * 100_000  # Above KMS's direct encryption size limit.
    payload = TrafficLogRequest(
        timestamp=datetime.now(UTC),
        method="POST",
        url="https://example.test/",
        status_code=200,
        body=body,
    )
    asyncio.run(storage.protect([document], [payload], StoragePolicy()))
    assert "body" not in document["logdata"]
    assert body not in document["storage_encryption"]["ciphertext"]
    assert storage.decrypt(document)["logdata"]["body"] == body

    second = protected_document()
    asyncio.run(storage.protect([second], [payload], StoragePolicy()))
    assert (
        second["storage_encryption"]["ciphertext"] != document["storage_encryption"]["ciphertext"]
    )


@pytest.mark.parametrize("tamper", ["context", "ciphertext"])
def test_cloud_envelope_rejects_context_or_ciphertext_tampering(cloud_storage, tamper):
    storage, _ = cloud_storage
    entry = {
        "plaintext": base64.b64encode(b'{"body":"private"}').decode(),
        "associated_data": base64.b64encode(b"record-one").decode(),
    }
    ciphertext = asyncio.run(storage.encrypt([entry]))[0]
    payload = {"ciphertext": ciphertext, "associated_data": entry["associated_data"]}
    if tamper == "context":
        payload["associated_data"] = base64.b64encode(b"record-two").decode()
    else:
        document, _ = openbao.parse_ciphertext(payload)
        document["ciphertext"] = base64.b64encode(b"corrupted").decode()
        payload["ciphertext"] = (
            "vault:v1:" + base64.b64encode(json.dumps(document).encode()).decode()
        )
    with storage.client() as client, pytest.raises(openbao.KMSUnavailable):
        client.request("POST", "transit/decrypt/log-storage", payload)


def test_cloud_kms_checksum_failure_is_not_accepted(cloud_storage):
    storage, fake = cloud_storage
    fake.bad_checksum = True
    with pytest.raises(openbao.KMSUnavailable, match="integrity"):
        asyncio.run(storage.encrypt([{"plaintext": "eA==", "associated_data": "eQ=="}]))


def test_cloud_keyset_checks_secret_checksum_and_pinned_version(monkeypatch):
    monkeypatch.setenv("GCP_STORAGE_KEY", "key")
    monkeypatch.setenv("GCP_EC_KEYSET", "projects/p/secrets/ec/versions/1")
    result = SimpleNamespace(payload=SimpleNamespace(data=b'{"keys":[]}', data_crc32c=0))
    secret = MagicMock()
    secret.__enter__.return_value.access_secret_version.return_value = result
    monkeypatch.setattr(openbao.secretmanager, "SecretManagerServiceClient", lambda: secret)
    with pytest.raises(openbao.KMSUnavailable):
        openbao.OpenBaoClient().load_keyset()
    result.payload.data_crc32c = openbao.checksum(result.payload.data)
    assert openbao.OpenBaoClient().load_keyset() == {"keys": []}
    monkeypatch.setenv("GCP_EC_KEYSET", "projects/p/secrets/ec/versions/latest")
    with pytest.raises(openbao.KMSUnavailable, match="pinned"):
        openbao.OpenBaoClient().load_keyset()


def test_cloud_rpc_errors_are_sanitized(cloud_storage):
    storage, fake = cloud_storage
    fake.get_crypto_key = MagicMock(side_effect=PermissionDenied("private diagnostic"))
    with storage.client() as client, pytest.raises(openbao.KMSUnavailable) as error:
        client.request("GET", "transit/keys/log-storage")
    assert str(error.value) == "Cloud KMS operation failed"


@pytest.mark.parametrize(
    "mongo_uri,valid",
    [
        ("mongodb://localhost/?tls=true", True),
        ("mongodb://localhost/?tls=true&tlsInsecure=true", False),
        ("mongodb://localhost/?tls=true&tlsAllowInvalidHostnames=true", False),
        ("mongodb://localhost/", False),
    ],
)
def test_cloud_connections_require_verified_tls(monkeypatch, mongo_uri, valid):
    monkeypatch.setattr(configuration.settings, "mongo_uri", mongo_uri)
    if valid:
        configuration.validate_cloud_connections()
    else:
        with pytest.raises(ValueError):
            configuration.validate_cloud_connections()


@pytest.mark.parametrize(
    "forwarded,protocols,address",
    [
        ([b"203.0.113.42"], [b"https"], "203.0.113.42"),
        ([b"spoofed, 203.0.113.42"], [b"https"], "203.0.113.42"),
        ([b"198.51.100.1, 2001:db8::42"], [b"https"], "2001:db8::42"),
        ([], [b"https"], None),
        ([b"not-an-ip"], [b"https"], None),
        ([b"203.0.113.42, "], [b"https"], None),
        ([b"203.0.113.42", b"198.51.100.1"], [b"https"], None),
        ([b"203.0.113.42"], [b"http"], None),
        ([b"203.0.113.42"], [], None),
        ([b"203.0.113.42"], [b"https", b"http"], None),
    ],
)
def test_cloud_run_headers_reject_spoofing_and_insecure_transport(
    monkeypatch, forwarded, protocols, address
):
    monkeypatch.setattr(configuration.settings, "auth0_domain", "tenant.example.com")
    monkeypatch.setattr(configuration.settings, "auth0_audience", "https://logs.example.com/api")
    from infra.gcp.runtime import api

    observed = {}
    messages = []

    async def application(scope, receive, send):
        observed.update(scope)
        await JSONResponse({"ok": True})(scope, receive, send)

    headers = [
        (b"forwarded", b"for=198.51.100.99;proto=https"),
        (b"x-ermes-client-ip", b"198.51.100.99"),
        (b"x-ermes-edge-ip", b"198.51.100.1"),
    ]
    headers.extend((b"x-forwarded-for", value) for value in forwarded)
    headers.extend((b"x-forwarded-proto", value) for value in protocols)

    async def run():
        async def send(message):
            messages.append(message)

        await api.CloudRunHeaders(application)(
            {
                "type": "http",
                "path": "/api/v1/me",
                "headers": headers,
                "scheme": "http",
                "client": ("127.0.0.1", 1234),
            },
            None,
            send,
        )

    asyncio.run(run())
    assert messages[0]["status"] == (200 if address else 400)
    if address:
        assert observed["client"] == (address, 0)
        assert observed["scheme"] == "https"
        assert (b"strict-transport-security", b"max-age=31536000") in messages[0]["headers"]
    else:
        assert not observed


def test_cloud_run_liveness_does_not_require_forwarding_headers():
    from infra.gcp.runtime import api

    messages = []

    async def application(scope, receive, send):
        await JSONResponse({"status": "ok"})(scope, receive, send)

    async def run():
        async def send(message):
            messages.append(message)

        await api.CloudRunHeaders(application)(
            {"type": "http", "path": "/health", "headers": []}, None, send
        )

    asyncio.run(run())
    assert messages[0]["status"] == 200


def test_cloud_run_https_keeps_api_authentication(monkeypatch):
    monkeypatch.setattr(configuration.settings, "auth0_domain", "tenant.example.com")
    monkeypatch.setattr(configuration.settings, "auth0_audience", "https://logs.example.com/api")
    from httpx import ASGITransport, AsyncClient

    from infra.gcp.runtime import api

    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=api.app), base_url="https://api.example.com"
        ) as client:
            response = await client.get(
                "/api/v1/me",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.42"},
            )
            assert response.status_code == 401
            assert response.headers["strict-transport-security"] == "max-age=31536000"

    asyncio.run(run())


def test_worker_liveness_detects_dead_consumer():
    async def run():
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        spool.app.state.consumer = task
        with pytest.raises(HTTPException) as error:
            await spool.health()
        assert error.value.status_code == 503

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["parse", "load"])
def test_worker_supervisor_starts_consuming_and_cancels_on_shutdown(monkeypatch, stage):
    calls = []

    async def run():
        started = asyncio.Event()

        async def operate(action, upload_id, *, manage_signals):
            assert action == stage and upload_id is None and manage_signals is False
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                calls.append("cancelled")

        monkeypatch.setenv("SPOOL_WORKER_STAGE", stage)
        monkeypatch.setattr(spool, "validate_cloud_connections", lambda: None)
        monkeypatch.setattr(spool, "operate", operate)
        async with spool.lifespan(spool.app):
            await asyncio.wait_for(started.wait(), timeout=1)
            assert await spool.health() == {"status": "ok"}
        assert calls == ["cancelled"]

    asyncio.run(run())


def test_provisioner_sends_valid_private_keyset_only_to_secret_manager(monkeypatch, capsys):
    secret = MagicMock()
    secret.__enter__.return_value.add_secret_version.return_value.name = "created/version/1"
    monkeypatch.setattr(
        provision_keyset.secretmanager, "SecretManagerServiceClient", lambda: secret
    )
    monkeypatch.setattr(
        "sys.argv",
        ["provision_keyset.py", "--secret", "projects/p/secrets/ec"],
    )
    provision_keyset.main()
    request = secret.__enter__.return_value.add_secret_version.call_args.kwargs["request"]
    document = json.loads(request["payload"]["data"])
    from app.core.encryption import parse_keyset

    assert parse_keyset(document).active_key_id == document["active_key_id"]
    output = capsys.readouterr().out
    assert output == "Created EC key-set version: created/version/1\n"
    assert document["keys"][0]["d"] not in output
