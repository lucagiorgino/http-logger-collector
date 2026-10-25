import asyncio
import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pymongo import AsyncMongoClient
from pymongo.monitoring import CommandListener
from pymongo.read_preferences import Secondary

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.core.encryption import get_encryption_keys
from app.db.mongo import MongoRepository, PersistenceError
from app.deps import get_mongo_repository, get_request_encryption_keys
from app.main import app
from app.schemas.traffic import TrafficLogRequest
from app.services.traffic_service import (
    DEVICE_ID_CLAIM,
    ClientIdentity,
    IngestionContext,
)


class RoutingListener(CommandListener):
    def __init__(self):
        self.operations = []

    def started(self, event):
        if (
            event.command_name in {"insert", "find"}
            and event.command[event.command_name] == "traffic_logs"
        ):
            self.operations.append((event.command_name, event.connection_id))

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


@pytest.mark.skipif(
    not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for device registration"
)
def test_device_registration_uniqueness_policy_enforcement_and_device_isolation(
    monkeypatch, encryption_keys, traffic_payload, storage_encryption, upload_harness
):
    from app.core.storage_encryption import get_storage_encryption

    traffic_payload["timestamp"] = datetime.now(UTC).isoformat()
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")

    async def scenario():
        database_name = "device_registration_test_" + uuid4().hex
        repository = MongoRepository(uri=os.environ["MONGO_TEST_URI"], db_name=database_name)
        claims = {
            "sub": "registration-operator",
            DEVICE_ID_CLAIM: "device-456",
            "permissions": ["manage:devices"],
        }
        original_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[require_bearer_token] = lambda: claims.copy()
        app.dependency_overrides[get_mongo_repository] = lambda: repository
        upload_harness.loader.repository = repository
        upload_harness.parser.keys = encryption_keys
        app.dependency_overrides[get_request_encryption_keys] = lambda: encryption_keys
        app.dependency_overrides[get_storage_encryption] = lambda: storage_encryption
        try:
            await repository.ensure_indexes()
            async with AsyncClient(
                transport=ASGITransport(app), base_url="https://testserver"
            ) as client:
                url = "/api/v1/devices"
                payload = {
                    "device_id": "device-456",
                    "name": "Registered laptop",
                    "encryption_mode": "required",
                }
                response = await client.post(url, json=payload)
                assert response.status_code == 201
                assert (await client.get(response.headers["location"])).json()["name"] == payload[
                    "name"
                ]
                document = await repository.get_device_configuration("device-456")
                assert document["name"] == payload["name"]
                assert document["version"] == 1
                claims["sub"] = "another-operator"
                assert (await client.get(url + "/device-456")).json()["version"] == 1
                assert (
                    await client.post(url, json=payload | {"name": "Different name"})
                ).status_code == 400
                assert (
                    await client.post(url, json=payload | {"device_id": "different-id"})
                ).status_code == 400
                assert await repository.get_device_configuration("device-456") == document
                claims["permissions"] = []
                gzip_batch = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
                upload_headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
                assert (
                    await client.post(
                        "/api/v1/traffic-log-uploads", content=gzip_batch, headers=upload_headers
                    )
                ).status_code == 403
                claims["permissions"] = ["manage:devices"]
                response = await client.patch(
                    url + "/device-456/configuration",
                    json={"expected_version": 1, "encryption_mode": "tls_only"},
                )
                assert response.status_code == 200
                assert response.json()["name"] == payload["name"]
                assert (
                    await client.post(
                        "/api/v1/traffic-log-uploads", content=gzip_batch, headers=upload_headers
                    )
                ).status_code == 202
                await upload_harness.drain()
                assert (
                    await client.post(
                        url, json={"device_id": "device-789", "name": "Second device"}
                    )
                ).status_code == 201
                first = (await client.get(url, params={"limit": 1})).json()
                assert first["items"][0]["device_id"] == "device-456"
                second = (await client.get(url, params={"after": first["next_after"]})).json()
                assert [item["device_id"] for item in second["items"]] == ["device-789"]
                assert second["next_after"] is None

                await repository.update_device_configuration("legacy-device", 0, "optional")
                legacy = {"device_id": "legacy-device", "name": "Existing agent"}
                assert (await client.post(url, json=legacy)).status_code == 400
                response = await client.post(
                    url, json=legacy | {"expected_version": 1, "encryption_mode": "optional"}
                )
                assert response.status_code == 422
                assert (await repository.get_device_configuration("legacy-device"))["version"] == 1
                assert await repository.report_device_configuration(
                    "agent-discovered@clients",
                    {
                        "version": 0,
                        "status": "applied",
                        "supports_encryption": False,
                        "reported_at": datetime.now(UTC),
                    },
                )
                inventory = (await client.get(url)).json()
                assert [item["device_id"] for item in inventory["items"]] == [
                    "agent-discovered@clients",
                    "device-456",
                    "device-789",
                    "legacy-device",
                ]
                assert inventory["items"][0]["name"] is None
                assert inventory["items"][0]["registered_at"] is None
                detail = await client.get(url + "/agent-discovered%40clients")
                assert detail.status_code == 200
                assert detail.json()["version"] == 0
                assert detail.json()["configuration_status"]["status"] == "applied"
                assert (await client.get(url + "/legacy-device")).status_code == 200
                assert (await client.get(url + "/missing-device")).status_code == 404
                first = (await client.get(url, params={"limit": 1})).json()
                assert first["next_after"] == "agent-discovered@clients"
                remaining = (await client.get(url, params={"after": first["next_after"]})).json()
                assert [item["device_id"] for item in remaining["items"]] == [
                    "device-456",
                    "device-789",
                    "legacy-device",
                ]

                for candidates in (
                    [
                        {"device_id": "race-id", "name": "First race device"},
                        {"device_id": "race-id", "name": "Second race device"},
                    ],
                    [
                        {"device_id": "race-name-a", "name": "Shared race name"},
                        {"device_id": "race-name-b", "name": "Shared race name"},
                    ],
                ):
                    responses = await asyncio.gather(
                        *(client.post(url, json=candidate) for candidate in candidates)
                    )
                    assert sorted(response.status_code for response in responses) == [201, 400]
                assert (
                    await repository.device_configurations.count_documents(
                        {"name": "Shared race name"}
                    )
                    == 1
                )
                claims["permissions"] = []
                assert [item["device_id"] for item in (await client.get(url)).json()["items"]] == [
                    "device-456"
                ]
                assert (await client.get(url + "/device-789")).status_code == 403
                assert (await client.get(url + "/device-789/configuration")).status_code == 403
                claims["sub"] = "yet-another-subject"
                assert (await client.get(url + "/device-456")).json()["version"] == 2
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(original_overrides)
            await repository.client.drop_database(database_name)
            await repository.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("allow_primary", "uri"),
    [
        pytest.param(
            True,
            os.getenv("MONGO_TEST_URI"),
            marks=pytest.mark.skipif(
                not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for local MongoDB"
            ),
            id="development-primary",
        ),
        pytest.param(
            False,
            os.getenv("MONGO_REPLICA_TEST_URI"),
            marks=pytest.mark.skipif(
                not os.getenv("MONGO_REPLICA_TEST_URI"),
                reason="Set MONGO_REPLICA_TEST_URI for secondary-only analysis",
            ),
            id="strict-secondary",
        ),
    ],
)
def test_time_series_ingestion_and_analysis_routing(
    monkeypatch, allow_primary, uri, storage_encryption, upload_harness
):
    from app.schemas.workspace import StoragePolicy

    monkeypatch.setattr(settings, "mongo_server_selection_timeout_ms", 1000)
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", allow_primary)
    monkeypatch.setattr(settings, "app_env", "development" if allow_primary else "production")

    async def scenario():
        database_name = "traffic_test_" + uuid4().hex
        repository = MongoRepository(uri=uri, db_name=database_name)
        listener = RoutingListener()
        monitored = AsyncMongoClient(
            uri, event_listeners=[listener], serverSelectionTimeoutMS=1000, tz_aware=True
        )
        await repository.client.close()
        repository.client = monitored
        repository.database = monitored[database_name]
        repository.traffic_logs = repository.database.get_collection(
            repository.collection_name,
            read_preference=repository.traffic_logs.read_preference,
            write_concern=repository.traffic_logs.write_concern,
        )
        repository.analysis_logs = repository.database.get_collection(
            repository.collection_name,
            read_preference=repository.analysis_logs.read_preference,
            read_concern=repository.analysis_logs.read_concern,
        )
        repository.device_configurations = repository.database.get_collection(
            "device_configurations",
            read_preference=repository.device_configurations.read_preference,
            read_concern=repository.device_configurations.read_concern,
            write_concern=repository.device_configurations.write_concern,
        )
        repository.workspace_configurations = repository.database.get_collection(
            "workspace_configurations",
            read_preference=repository.workspace_configurations.read_preference,
            read_concern=repository.workspace_configurations.read_concern,
            write_concern=repository.workspace_configurations.write_concern,
        )
        try:
            await repository.ensure_indexes()
            await repository.ensure_indexes()
            collection_cursor = await repository.database.list_collections(
                filter={"name": repository.collection_name}
            )
            collection = (await collection_cursor.to_list(length=1))[0]
            assert collection["type"] == "timeseries"
            payload = TrafficLogRequest(
                timestamp=datetime.now(UTC),
                method="GET",
                url="https://example.com",
                status_code=200,
                headers={"Authorization": "secret"},
                captured_source_ip="192.0.2.10",
                destination_ip="203.0.113.50",
                response_size_bytes=1024,
                process_name="chrome.exe",
                body="private body",
            )
            context = IngestionContext(ClientIdentity("device", "oauth-client"), "127.0.0.1")
            upload_harness.loader.repository = repository
            upload_id = await upload_harness.enqueue(
                [payload, payload],
                context,
                StoragePolicy(),
            )
            await upload_harness.drain()
            assert upload_harness.store.records[upload_id]["status"] == "stored"
            deadline = monotonic() + 15
            while True:
                documents = await repository.list_traffic_logs(
                    device_id="device",
                    start_time=None,
                    end_time=None,
                    before=None,
                    limit=10,
                )
                if len(documents) == 2:
                    break
                if monotonic() >= deadline:
                    pytest.fail("Acknowledged logs did not reach the configured analysis node")
                await asyncio.sleep(0.1)
            assert len({document["_id"] for document in documents}) == 2
            assert all(document["ingestion"]["batch_id"] == upload_id for document in documents)
            assert documents[0]["metadata"] == {"device_id": "device", "suspicious": False}
            assert documents[0]["ingestion"]["client_id"] == "oauth-client"
            assert documents[0]["ingestion"]["source_ip"] == "127.0.0.1"
            assert documents[0]["logdata"]["captured_source_ip"] == "192.0.2.10"
            assert documents[0]["logdata"]["destination_ip"] == "203.0.113.50"
            assert documents[0]["logdata"]["response_size_bytes"] == 1024
            assert documents[0]["logdata"]["process_name"] == "chrome.exe"
            assert documents[0]["logdata"]["request_headers"]["Authorization"] == "[ENCRYPTED]"
            assert "body" not in documents[0]["logdata"]
            batch_records = (
                await repository.traffic_logs.find({"ingestion.batch_id": upload_id})
                .hint("ingestion_batch_record")
                .to_list(length=None)
            )
            assert {record["_id"] for record in batch_records} == {
                document["_id"] for document in documents
            }
            assert (
                await repository.get_traffic_log(documents[0]["_id"], device_id="other-device")
                is None
            )
            assert (
                await repository.get_traffic_log(documents[0]["_id"], device_id="device")
                is not None
            )
            assert await repository.get_traffic_log(documents[0]["_id"], device_id=None) is not None
            next_page = await repository.list_traffic_logs(
                device_id=None,
                start_time=None,
                end_time=None,
                before=(documents[0]["timestamp"], documents[0]["_id"]),
                limit=10,
            )
            assert [document["_id"] for document in next_page] == [documents[1]["_id"]]
            for operation, (host, port) in set(listener.operations):
                node = AsyncMongoClient(
                    f"mongodb://{host}:{port}/?directConnection=true",
                    serverSelectionTimeoutMS=1000,
                )
                try:
                    hello = await node.admin.command("hello")
                    assert hello["isWritablePrimary"] is (operation == "insert" or allow_primary)
                    if operation == "find" and not allow_primary:
                        assert hello["secondary"] is True
                finally:
                    await node.close()
            repository.analysis_logs = repository.analysis_logs.with_options(
                read_preference=Secondary(tag_sets=[{"unavailable": "true"}])
            )
            with pytest.raises(PersistenceError, match="replica"):
                await repository.get_traffic_log(documents[0]["_id"], device_id="device")
            upload_id = await upload_harness.enqueue(
                [payload],
                context,
                StoragePolicy(),
            )
            await upload_harness.drain()
            assert upload_harness.store.records[upload_id]["status"] == "stored"
        finally:
            await repository.client.drop_database(database_name)
            await repository.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for local MongoDB")
@pytest.mark.parametrize(
    "key_source",
    [
        "memory",
        pytest.param(
            "openbao",
            marks=pytest.mark.skipif(
                not os.getenv("OPENBAO_TEST_ADDR") or not os.getenv("OPENBAO_TEST_TOKEN_FILE"),
                reason="Set OPENBAO_TEST_ADDR and OPENBAO_TEST_TOKEN_FILE for full KMS ingestion",
            ),
        ),
    ],
)
def test_device_policy_and_encrypted_ingestion_persist_end_to_end(
    monkeypatch, encryption_keys, traffic_payload, key_source, storage_encryption, upload_harness
):
    from joserfc import jwe
    from joserfc.jwk import ECKey

    from app.core.storage_encryption import get_storage_encryption

    traffic_payload["timestamp"] = datetime.now(UTC).isoformat()
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")
    if key_source == "openbao":
        from app.core.storage_encryption import StorageEncryption

        monkeypatch.setattr(settings, "openbao_addr", os.environ["OPENBAO_TEST_ADDR"])
        monkeypatch.setattr(settings, "openbao_token_file", os.environ["OPENBAO_TEST_TOKEN_FILE"])
        get_encryption_keys.cache_clear()
        encryption_keys = get_encryption_keys()
        storage_encryption = StorageEncryption()

    async def scenario():
        repository = MongoRepository(
            uri=os.environ["MONGO_TEST_URI"], db_name="traffic_test_" + uuid4().hex
        )
        claims = {
            "sub": "user-123",
            DEVICE_ID_CLAIM: "device-456",
            "permissions": ["manage:devices"],
        }
        app.dependency_overrides[require_bearer_token] = lambda: claims
        app.dependency_overrides[get_mongo_repository] = lambda: repository
        upload_harness.loader.repository = repository
        upload_harness.parser.keys = encryption_keys
        upload_harness.parser.storage = storage_encryption
        app.dependency_overrides[get_request_encryption_keys] = lambda: encryption_keys
        app.dependency_overrides[get_storage_encryption] = lambda: storage_encryption
        config_url = "/api/v1/devices/device-456/configuration"
        upload_url = "/api/v1/traffic-log-uploads"
        try:
            await repository.ensure_indexes()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://testserver"
            ) as client:
                response = await client.get(config_url)
                assert response.json()["version"] == 0
                response = await client.put(
                    config_url + "-status",
                    json={"version": 0, "status": "applied", "supports_encryption": False},
                )
                assert response.status_code == 200
                response = await client.patch(
                    config_url, json={"expected_version": 0, "encryption_mode": "optional"}
                )
                assert response.status_code == 200
                assert response.json()["version"] == 1
                response = await client.put(
                    config_url + "-status",
                    json={"version": 1, "status": "unsupported", "supports_encryption": False},
                )
                assert response.status_code == 200
                response = await client.get(config_url)
                assert response.json()["configuration_status"]["status"] == "unsupported"
                body = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
                response = await client.post(
                    upload_url,
                    content=body,
                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                )
                assert response.status_code == 202
                await upload_harness.drain()
                results = await asyncio.gather(
                    *(
                        client.patch(
                            config_url, json={"expected_version": 1, "encryption_mode": "required"}
                        )
                        for _ in range(2)
                    )
                )
                assert sorted(response.status_code for response in results) == [200, 409]
                response = await client.post(
                    upload_url,
                    content=body,
                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                )
                assert response.status_code == 403
                response = await client.put(
                    config_url + "-status",
                    json={"version": 1, "status": "applied", "supports_encryption": False},
                )
                assert response.status_code == 409
                response = await client.put(
                    config_url + "-status",
                    json={
                        "version": 2,
                        "status": "applied",
                        "supports_encryption": True,
                        "key_id": encryption_keys.active_key_id,
                    },
                )
                assert response.status_code == 200
                response = await client.get(config_url)
                public = response.json()["encryption"]["public_key"]
                envelope = jwe.encrypt_compact(
                    {
                        "alg": response.json()["encryption"]["algorithm"],
                        "enc": response.json()["encryption"]["encryption"],
                        "kid": encryption_keys.active_key_id,
                        "cty": "application/gzip",
                    },
                    body,
                    ECKey.import_key(public),
                    registry=encryption_keys.registry,
                )
                response = await client.post(
                    upload_url, content=envelope, headers={"Content-Type": "application/jose"}
                )
                assert response.status_code == 202
                await upload_harness.drain()
                creations = await asyncio.gather(
                    *(
                        client.patch(
                            "/api/v1/devices/new-device/configuration",
                            json={"expected_version": 0, "encryption_mode": "optional"},
                        )
                        for _ in range(2)
                    )
                )
                assert sorted(response.status_code for response in creations) == [200, 409]
                claims["sub"] = "another-subject"
                response = await client.get(config_url)
                assert response.json()["version"] == 2
                claims[DEVICE_ID_CLAIM] = "another-device"
                claims["permissions"] = []
                assert (await client.get(config_url)).status_code == 403
                response = await client.post(
                    upload_url, content=envelope, headers={"Content-Type": "application/jose"}
                )
                assert response.status_code == 403
                documents = await repository.list_traffic_logs(
                    device_id="device-456",
                    start_time=None,
                    end_time=None,
                    before=None,
                    limit=10,
                )
                assert len(documents) == 2
                assert all(
                    document["logdata"]["request_headers"]["Authorization"] == "[ENCRYPTED]"
                    for document in documents
                )
        finally:
            app.dependency_overrides.clear()
            get_encryption_keys.cache_clear()
            await repository.client.drop_database(repository.database.name)
            await repository.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for local MongoDB")
def test_workspace_settings_control_storage_and_survive_concurrent_edits(
    monkeypatch,
    storage_encryption,
    traffic_payload,
    upload_harness,
):
    from app.core.encryption import EncryptionKeys
    from app.core.storage_encryption import get_storage_encryption

    traffic_payload["timestamp"] = datetime.now(UTC).isoformat()
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")

    async def scenario():
        repository = MongoRepository(
            uri=os.environ["MONGO_TEST_URI"], db_name="workspace_test_" + uuid4().hex
        )
        app.dependency_overrides[require_bearer_token] = lambda: {
            "sub": "test-device",
            "permissions": ["manage:devices"],
        }
        app.dependency_overrides[get_mongo_repository] = lambda: repository
        upload_harness.loader.repository = repository
        app.dependency_overrides[get_request_encryption_keys] = lambda: EncryptionKeys({})
        app.dependency_overrides[get_storage_encryption] = lambda: storage_encryption
        url = "/api/v1/workspace/configuration"
        flags = ("encrypt_request_body", "encrypt_authorization_header", "encrypt_cookies")
        wire = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
        headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
        try:
            await repository.ensure_indexes()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://testserver"
            ) as client:
                assert (await client.get(url)).json()["version"] == 0
                assert (
                    await client.post("/api/v1/traffic-log-uploads", content=wire, headers=headers)
                ).status_code == 202
                await upload_harness.drain()
                encrypted = await repository.traffic_logs.find_one({})
                assert traffic_payload["body"] not in repr(encrypted)
                assert encrypted["logdata"]["request_headers"]["Authorization"] == "[ENCRYPTED]"
                changes = await asyncio.gather(
                    *(
                        client.patch(
                            url, json={"expected_version": 0, **dict.fromkeys(flags, False)}
                        )
                        for _ in range(2)
                    )
                )
                assert sorted(response.status_code for response in changes) == [200, 409]
                assert (await client.get(url)).json()["version"] == 1
                assert (
                    await client.post("/api/v1/traffic-log-uploads", content=wire, headers=headers)
                ).status_code == 202
                await upload_harness.drain()
                plain = await repository.traffic_logs.find_one(
                    {"storage_encryption": {"$exists": False}}
                )
                assert plain["logdata"]["body"] == traffic_payload["body"]
                assert plain["logdata"]["request_headers"]["Authorization"] == "secret"
                assert plain["logdata"]["request_headers"]["Cookie"] == "session=secret"
                for document in (encrypted, plain):
                    detail = await client.get(f"/api/v1/traffic-logs/{document['_id']}")
                    assert detail.status_code == 200
                    assert detail.json()["body"] == traffic_payload["body"]
                    assert detail.json()["request_headers"]["Authorization"] == "[REDACTED]"
                summaries = await repository.list_traffic_logs(
                    device_id="test-device", start_time=None, end_time=None, before=None, limit=10
                )
                assert len(summaries) == 2
                assert all("body" not in document["logdata"] for document in summaries)
                assert all(
                    "ciphertext" not in document.get("storage_encryption", {})
                    for document in summaries
                )
                await client.patch(url, json={"expected_version": 1, **dict.fromkeys(flags, True)})
                stored = await repository.workspace_configurations.find_one({"_id": "workspace"})
                assert stored["version"] == 2
                assert all(stored[flag] for flag in flags)
                assert await repository.traffic_logs.count_documents({}) == 2
                discard = {
                    "retain_request_body": False,
                    "retain_authorization_header": False,
                    "retain_cookies": False,
                }
                saved = await client.patch(
                    url, json={"expected_version": 2, **dict.fromkeys(flags, True), **discard}
                )
                assert saved.status_code == 200
                assert all(saved.json()[field] is False for field in discard)
                uploaded = await client.post(
                    "/api/v1/traffic-log-uploads", content=wire, headers=headers
                )
                assert uploaded.status_code == 202
                await upload_harness.drain()
                discarded = await repository.traffic_logs.find_one(
                    {"ingestion.batch_id": uploaded.json()["upload_id"]}
                )
                assert "body" not in discarded["logdata"]
                assert "storage_encryption" not in discarded
                body = traffic_payload["body"].encode("utf-8")
                assert discarded["logdata"]["body_length"] == len(body)
                assert discarded["logdata"]["body_sha256"] == sha256(body).hexdigest()
                assert not discarded["logdata"]["request_headers"]
                detail = await client.get(f"/api/v1/traffic-logs/{discarded['_id']}")
                assert detail.status_code == 200
                assert detail.json()["body"] is None
                assert detail.json()["body_length"] == len(body)
                assert detail.json()["body_sha256"] == sha256(body).hexdigest()
                assert detail.json()["request_headers"] == {}
                historical = await client.get(f"/api/v1/traffic-logs/{encrypted['_id']}")
                assert historical.json()["body"] == traffic_payload["body"]
        finally:
            app.dependency_overrides.clear()
            await repository.client.drop_database(repository.database.name)
            await repository.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for retention TTL")
def test_retention_ttl_deletes_expired_logs_and_preserves_suspicious_and_disabled_logs(
    monkeypatch,
):
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")

    async def scenario():
        repository = MongoRepository(
            uri=os.environ["MONGO_TEST_URI"], db_name="retention_test_" + uuid4().hex
        )
        try:
            await repository.ensure_indexes()
            await repository.update_workspace_configuration(
                0, {"retention_enabled": False, "suspicious_retention_enabled": False}
            )
            now = datetime.now(UTC)
            documents = [
                {
                    "timestamp": now - timedelta(days=days),
                    "metadata": {"device_id": label, **classification},
                }
                for label, days, classification in (
                    ("ordinary-expired", 2, {"suspicious": False}),
                    ("legacy-expired", 2, {}),
                    ("suspicious-retained", 2, {"suspicious": True}),
                    ("suspicious-expired", 4, {"suspicious": True}),
                    ("ordinary-recent", 0, {"suspicious": False}),
                )
            ]
            await repository.insert_traffic_logs(documents)
            await repository.update_workspace_configuration(
                1,
                {
                    "retention_enabled": True,
                    "retention_days": 1,
                    "suspicious_retention_enabled": True,
                    "suspicious_retention_days": 3,
                },
            )

            async def wait_for_devices(expected):
                deadline = monotonic() + 100
                while monotonic() < deadline:
                    remaining = {
                        document["metadata"]["device_id"]
                        for document in await repository.traffic_logs.find({}).to_list(None)
                    }
                    if remaining == expected:
                        return
                    await asyncio.sleep(1)
                pytest.fail(f"TTL did not converge: {remaining!r} != {expected!r}")

            await wait_for_devices({"suspicious-retained", "ordinary-recent"})
            await repository.update_workspace_configuration(
                2, {"retention_days": 2, "suspicious_retention_days": 4}
            )
            indexes = await repository.traffic_logs.index_information()
            assert indexes["retention_ordinary"]["expireAfterSeconds"] == 2 * 86400
            assert indexes["retention_suspicious"]["expireAfterSeconds"] == 4 * 86400
            await repository.update_workspace_configuration(
                3, {"retention_enabled": False, "suspicious_retention_days": 1}
            )
            await repository.insert_traffic_logs(
                [
                    {
                        "timestamp": now - timedelta(days=10),
                        "metadata": {"device_id": "ordinary-disabled", "suspicious": False},
                    }
                ]
            )
            await wait_for_devices({"ordinary-disabled", "ordinary-recent"})
            await repository.update_workspace_configuration(
                4, {"suspicious_retention_enabled": False}
            )
            indexes = await repository.traffic_logs.index_information()
            assert "retention_ordinary" not in indexes
            assert "retention_suspicious" not in indexes
            await repository.ensure_indexes()
            indexes = await repository.traffic_logs.index_information()
            assert "retention_ordinary" not in indexes
            assert "retention_suspicious" not in indexes
        finally:
            await repository.client.drop_database(repository.database.name)
            await repository.close()

    asyncio.run(scenario())
