import asyncio
import gzip
import json
import os
from time import monotonic
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.monitoring import CommandListener
from pymongo.read_preferences import Secondary

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.core.encryption import get_encryption_keys
from app.db.mongo import MongoRepository, PersistenceError
from app.deps import get_mongo_repository
from app.main import app
from app.schemas.traffic import TrafficLogRequest
from app.services.ingestion import MongoIngestionPublisher
from app.services.traffic_service import ingest_traffic_logs


class RoutingListener(CommandListener):
    def __init__(self):
        self.operations = []

    def started(self, event):
        if event.command_name in {"insert", "find"}:
            self.operations.append((event.command_name, event.connection_id))

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


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
def test_time_series_ingestion_and_analysis_routing(monkeypatch, allow_primary, uri):
    monkeypatch.setattr(settings, "mongo_server_selection_timeout_ms", 1000)
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", allow_primary)
    monkeypatch.setattr(settings, "app_env", "development" if allow_primary else "production")

    async def scenario():
        database_name = "traffic_test_" + uuid4().hex
        repository = MongoRepository(uri=uri, db_name=database_name)
        listener = RoutingListener()
        monitored = AsyncIOMotorClient(
            uri, event_listeners=[listener], serverSelectionTimeoutMS=1000, tz_aware=True
        )
        repository.client.close()
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
        try:
            await repository.ensure_indexes()
            await repository.ensure_indexes()
            collection_cursor = await repository.database.list_collections(
                filter={"name": repository.collection_name}
            )
            collection = (await collection_cursor.to_list(length=1))[0]
            assert collection["type"] == "timeseries"
            payload = TrafficLogRequest(
                user_id="owner",
                device_id="device",
                timestamp="2026-01-15T10:00:00Z",
                method="GET",
                url="https://example.com",
                status_code=200,
                headers={"Authorization": "secret"},
                body="private body",
            )
            receipt = await ingest_traffic_logs(
                [payload, payload], "owner", MongoIngestionPublisher(repository)
            )
            assert receipt.status == "stored"
            deadline = monotonic() + 15
            while True:
                documents = await repository.list_traffic_logs(
                    "owner",
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
            assert all(document["batch_id"] == receipt.batch_id for document in documents)
            assert documents[0]["headers"]["Authorization"] == "[REDACTED]"
            assert "body" not in documents[0]
            assert await repository.get_traffic_log("other-owner", documents[0]["_id"]) is None
            assert await repository.get_traffic_log("owner", documents[0]["_id"]) is not None
            next_page = await repository.list_traffic_logs(
                "owner",
                device_id=None,
                start_time=None,
                end_time=None,
                before=(documents[0]["timestamp"], documents[0]["_id"]),
                limit=10,
            )
            assert [document["_id"] for document in next_page] == [documents[1]["_id"]]
            for operation, (host, port) in set(listener.operations):
                node = AsyncIOMotorClient(
                    f"mongodb://{host}:{port}/?directConnection=true",
                    serverSelectionTimeoutMS=1000,
                )
                try:
                    hello = await node.admin.command("hello")
                    assert hello["isWritablePrimary"] is (operation == "insert" or allow_primary)
                    if operation == "find" and not allow_primary:
                        assert hello["secondary"] is True
                finally:
                    node.close()
            repository.analysis_logs = repository.analysis_logs.with_options(
                read_preference=Secondary(tag_sets=[{"unavailable": "true"}])
            )
            with pytest.raises(PersistenceError, match="replica"):
                await repository.get_traffic_log("owner", documents[0]["_id"])
            await ingest_traffic_logs([payload], "owner", MongoIngestionPublisher(repository))
        finally:
            await repository.client.drop_database(database_name)
            repository.close()

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
    monkeypatch, encryption_keys, traffic_payload, key_source
):
    from joserfc import jwe
    from joserfc.jwk import ECKey

    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")
    if key_source == "openbao":
        monkeypatch.setattr(settings, "openbao_addr", os.environ["OPENBAO_TEST_ADDR"])
        monkeypatch.setattr(settings, "openbao_token_file", os.environ["OPENBAO_TEST_TOKEN_FILE"])
        get_encryption_keys.cache_clear()
        encryption_keys = get_encryption_keys()

    async def scenario():
        repository = MongoRepository(
            uri=os.environ["MONGO_TEST_URI"], db_name="traffic_test_" + uuid4().hex
        )
        claims = {"sub": "user-123", "permissions": ["manage:devices"]}
        app.dependency_overrides[require_bearer_token] = lambda: claims
        app.dependency_overrides[get_mongo_repository] = lambda: repository
        app.dependency_overrides[get_encryption_keys] = lambda: encryption_keys
        config_url = "/api/v1/devices/device-456/configuration"
        upload_url = "/api/v1/traffic-log-batches"
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
                claims["sub"] = "another-owner"
                response = await client.get(config_url)
                assert response.json()["version"] == 0
                response = await client.post(
                    upload_url, content=envelope, headers={"Content-Type": "application/jose"}
                )
                assert response.status_code == 403
                documents = await repository.list_traffic_logs(
                    "user-123",
                    device_id="device-456",
                    start_time=None,
                    end_time=None,
                    before=None,
                    limit=10,
                )
                assert len(documents) == 2
                assert all(
                    document["headers"]["Authorization"] == "[REDACTED]" for document in documents
                )
        finally:
            app.dependency_overrides.clear()
            get_encryption_keys.cache_clear()
            await repository.client.drop_database(repository.database.name)
            repository.close()

    asyncio.run(scenario())
