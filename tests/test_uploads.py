import asyncio
import base64
import copy
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from bson import BSON, ObjectId
from google.api_core.exceptions import DeadlineExceeded, NotFound, PreconditionFailed
from httpx2 import ASGITransport, AsyncClient
from joserfc import jwe
from upload_harness import MemoryUploads, silver_queue

from app.api.uploads import get_upload_cloud, get_upload_store
from app.core.auth import require_bearer_token
from app.core.config import settings
from app.core.encryption import EncryptionKeys
from app.core.openbao import KMSUnavailable
from app.db.mongo import MongoRepository, PersistenceError
from app.main import app
from app.schemas.uploads import UploadDescriptor
from app.schemas.workspace import StoragePolicy
from app.services.queued_batch import InvalidQueuedBatch
from app.services.traffic_service import DEVICE_ID_CLAIM
from app.services.upload_cloud import UploadCloud
from app.services.upload_pipeline import (
    MongoWriteLimiter,
    UploadLoader,
    UploadParser,
    normalize_upload,
)
from app.services.upload_queue import (
    MAX_SILVER_BYTES,
    SilverPayloadTooLarge,
    UploadQueue,
    encode_silver,
)
from app.services.upload_state import UploadStateStore

HEADERS = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
URL = "/api/v1/traffic-log-uploads"


def test_worker_import_does_not_require_auth0_or_load_api_routes(tmp_path):
    env_file = tmp_path / "worker.env"
    env_file.write_text("")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AUTH0_")}
    env["ENV_FILE"] = str(env_file)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            ("import sys; import app.spool_worker; assert 'app.core.auth' not in sys.modules"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def upload_setup(authorized_client, monkeypatch):
    monkeypatch.setattr(settings, "spool_bucket", "test-raw")
    monkeypatch.setattr(settings, "spool_storage_backend", "minio")
    store = MemoryUploads()
    objects = {}
    s3 = Mock()

    def put(**kwargs):
        key = kwargs["Key"]
        if key in objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        objects[key] = kwargs["Body"]

    def get(**kwargs):
        body = objects[kwargs["Key"]]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}

    def delete(**kwargs):
        objects.pop(kwargs["Key"], None)

    s3.put_object.side_effect = put
    s3.get_object.side_effect = get
    s3.delete_object.side_effect = delete
    cloud = UploadCloud(s3, Mock(), Mock())
    cloud.dead_letter = AsyncMock()
    app.dependency_overrides[get_upload_cloud] = lambda: cloud
    app.dependency_overrides[get_upload_store] = lambda: store
    return authorized_client, store, cloud, objects


def upload(setup, payload, batch_id=None):
    client, _, _, _ = setup
    wire = gzip.compress(json.dumps({"logs": [payload]}).encode(), mtime=0)
    headers = HEADERS | ({"X-Batch-Id": batch_id} if batch_id else {})
    response = client.post(URL, content=wire, headers=headers)
    return response, wire


def test_upload_persists_exact_wire_and_defers_content_processing(
    upload_setup,
    repository,
    storage_encryption,
    monkeypatch,
):
    protect = AsyncMock(side_effect=AssertionError("Collector must not encrypt/decompress"))
    monkeypatch.setattr(storage_encryption, "protect", protect)
    client, store, cloud, objects = upload_setup
    # Content processing belongs exclusively to the asynchronous parser.
    response = client.post(URL, content=b"not gzip", headers=HEADERS)
    assert response.status_code == 202
    assert "accepted_count" not in response.json()
    record = store.records[response.json()["upload_id"]]
    descriptor = UploadDescriptor.model_validate(record["descriptor"])
    header, wire = objects[descriptor.object_key].split(b"\n", 1)
    assert wire == b"not gzip"
    assert UploadDescriptor.model_validate_json(header) == descriptor
    assert descriptor.source_ip == "203.0.113.10"
    assert descriptor.received_at.microsecond % 1000 == 0
    cloud.objects.put_object.assert_called_once()
    assert cloud.objects.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    repository.insert_queued_traffic_logs.assert_not_awaited()
    protect.assert_not_awaited()


def test_raw_storage_failure_is_not_success(upload_setup):
    client, _, cloud, _ = upload_setup
    cloud.objects.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "private detail"}}, "PutObject"
    )
    response = client.post(URL, content=b"x", headers=HEADERS)
    assert response.status_code == 503
    assert "private detail" not in response.text


def test_collector_retry_idempotency_and_content_conflict(upload_setup, traffic_payload):
    first, wire = upload(upload_setup, traffic_payload, "batch-1")
    client, store, cloud, _ = upload_setup
    second = client.post(URL, content=wire, headers=HEADERS | {"X-Batch-Id": "batch-1"})
    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["upload_id"] == second.json()["upload_id"]
    assert second.json()["duplicate"] is True
    assert len(store.records) == 1
    cloud.objects.put_object.assert_called_once()
    assert (
        client.post(
            URL, content=b"different", headers=HEADERS | {"X-Batch-Id": "batch-1"}
        ).status_code
        == 409
    )


def test_ambiguous_put_retry_reuses_object_and_policy_snapshot(
    upload_setup,
    traffic_payload,
    repository,
):
    client, store, cloud, _ = upload_setup
    response, wire = upload(upload_setup, traffic_payload, "retry")
    record = store.records[response.json()["upload_id"]]
    record["status"] = "uploading"  # Put succeeded, receipt acknowledgement was lost.
    repository.get_workspace_configuration.return_value["retain_request_body"] = False
    retry = client.post(URL, content=wire, headers=HEADERS | {"X-Batch-Id": "retry"})
    assert retry.status_code == 200
    assert record["descriptor"]["policy"]["retain_request_body"] is True
    assert cloud.objects.get_object.called


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ({}, b"x", 415),
        (HEADERS, b"", 400),
        (HEADERS, b"x" * 1_048_577, 413),
        (HEADERS | {"X-Batch-Id": "invalid key"}, b"x", 400),
    ],
)
def test_collector_keeps_cheap_validation(upload_setup, headers, body, code):
    client, store, _, _ = upload_setup
    assert client.post(URL, content=body, headers=headers).status_code == code
    assert not store.records


def test_upload_requires_auth_and_enforces_transfer_policy(upload_setup, repository):
    client, store, _, _ = upload_setup
    repository.get_device_configuration.return_value["encryption_mode"] = "required"
    assert client.post(URL, content=b"x", headers=HEADERS).status_code == 403
    app.dependency_overrides.pop(require_bearer_token)
    assert client.post(URL, content=b"x", headers=HEADERS).status_code == 401
    assert not store.records


def test_status_is_device_scoped_and_reveals_no_raw_content(upload_setup, traffic_payload):
    response, _ = upload(upload_setup, traffic_payload)
    client, _, _, _ = upload_setup
    path = f"{URL}/{response.json()['upload_id']}"
    status = client.get(path)
    assert status.json()["accepted_count"] is None
    assert status.headers["cache-control"] == "no-store"
    assert "descriptor" not in status.json()
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "other",
        DEVICE_ID_CLAIM: "other-device",
    }
    assert client.get(path).status_code == 404
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "manager",
        "permissions": ["manage:devices"],
    }
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("encrypted", [False, True])
def test_full_pipeline_reuses_protection_and_deletes_only_after_mongo(
    upload_setup,
    traffic_payload,
    repository,
    storage_encryption,
    encryption_keys,
    encrypted,
):
    client, store, cloud, objects = upload_setup
    repository.get_device_configuration.return_value["encryption_mode"] = "optional"
    wire = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
    headers = HEADERS
    if encrypted:
        wire = jwe.encrypt_compact(
            {
                "alg": encryption_keys.algorithm,
                "enc": encryption_keys.encryption,
                "kid": encryption_keys.active_key_id,
                "cty": "application/gzip",
            },
            wire,
            encryption_keys.keys[encryption_keys.active_key_id],
            registry=encryption_keys.registry,
        ).encode()
        headers = {"Content-Type": "application/jose"}
    response = client.post(URL, content=wire, headers=headers)
    assert response.status_code == 202
    record = store.records[response.json()["upload_id"]]
    key = record["descriptor"]["object_key"]
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, encryption_keys, storage_encryption)
    loader = UploadLoader(cloud, store, queue, repository)

    async def scenario():
        assert await parser.process(key)
        assert await parser.process(key)  # Duplicate S3 delivery never republishes.
        queue.publish.assert_awaited_once()
        documents = queue.publish.call_args.args[1]
        assert "body" not in documents[0]["logdata"]
        assert "storage_encryption" in documents[0]
        assert traffic_payload["body"].encode() not in BSON.encode(documents[0])
        assert key in objects

        async def persist(documents):
            assert key in objects
            assert record["status"] == "loading"

        repository.insert_queued_traffic_logs.side_effect = persist
        queue.receive.return_value = queue.messages[0]
        assert await loader.consume_once()
        assert record["status"] == "stored"
        assert record["raw_deleted"]
        assert key not in objects
        # Stream replay after success skips Mongo and object retrieval.
        assert await loader.consume_once()
        repository.insert_queued_traffic_logs.assert_awaited_once()

    asyncio.run(scenario())


def test_parser_honors_snapshot_discard_and_deterministic_replay(
    upload_setup,
    traffic_payload,
    repository,
    storage_encryption,
):
    repository.get_workspace_configuration.return_value.update(
        retain_request_body=False,
        retain_authorization_header=False,
        retain_cookies=False,
    )
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, objects = upload_setup
    record = store.records[response.json()["upload_id"]]
    key = record["descriptor"]["object_key"]
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)

    async def scenario():
        assert await parser.process(key)
        first = copy.deepcopy(queue.publish.call_args.args[1])
        record["status"] = "received"
        assert await parser.process(key)
        second = queue.publish.call_args.args[1]
        assert second == first
        log = first[0]
        assert "body" not in log["logdata"]
        assert "authorization" not in log["logdata"]["request_headers"]
        assert "cookie" not in log["logdata"]["request_headers"]
        assert "storage_encryption" not in log
        assert key in objects

    asyncio.run(scenario())


@pytest.mark.parametrize("wire", [b"invalid gzip", gzip.compress(b'{"logs":[]}')])
def test_poison_raw_goes_to_dlq_and_keeps_replay_source(upload_setup, storage_encryption, wire):
    client, store, cloud, objects = upload_setup
    response = client.post(URL, content=wire, headers=HEADERS)
    record = store.records[response.json()["upload_id"]]
    key = record["descriptor"]["object_key"]
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)
    assert asyncio.run(parser.process(key))
    assert record["status"] == "parse_failed"
    cloud.dead_letter.assert_awaited_once_with("parse", record["_id"], "invalid_content")
    queue.publish.assert_not_awaited()
    assert key in objects


def test_transient_parser_failures_retry_then_dlq(upload_setup, traffic_payload, monkeypatch):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, objects = upload_setup
    record = store.records[response.json()["upload_id"]]
    key = record["descriptor"]["object_key"]
    storage = AsyncMock()
    storage.protect.side_effect = KMSUnavailable("private KMS error")
    monkeypatch.setattr(settings, "spool_max_attempts", 2)
    parser = UploadParser(cloud, store, silver_queue(), EncryptionKeys({}), storage)

    async def scenario():
        assert not await parser.process(key)
        assert record["status"] == "received"
        assert await parser.process(key)
        assert record["status"] == "parse_failed"
        assert key in objects
        cloud.dead_letter.assert_awaited_once()

    asyncio.run(scenario())


def test_load_failures_dlq_before_ack_and_never_delete_raw(
    upload_setup,
    traffic_payload,
    storage_encryption,
    repository,
    monkeypatch,
):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, objects = upload_setup
    record = store.records[response.json()["upload_id"]]
    key = record["descriptor"]["object_key"]
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)
    loader = UploadLoader(cloud, store, queue, repository)
    monkeypatch.setattr(settings, "spool_max_attempts", 2)
    repository.insert_queued_traffic_logs.side_effect = PersistenceError("failed")

    async def scenario():
        await parser.process(key)
        queue.receive.return_value = queue.messages[0]
        assert not await loader.consume_once()
        queue.acknowledge.assert_not_awaited()
        cloud.dead_letter.side_effect = PersistenceError("DLQ unavailable")
        with pytest.raises(PersistenceError):
            await loader.consume_once()
        queue.acknowledge.assert_not_awaited()
        assert key in objects
        # Simulate lease recovery after the DLQ outage.
        record["status"] = "queued"
        cloud.dead_letter.side_effect = None
        assert await loader.consume_once()
        assert record["status"] == "load_failed"
        queue.acknowledge.assert_awaited_once()
        assert key in objects

    asyncio.run(scenario())


def test_cleanup_retry_never_reinserts_mongo(
    upload_setup,
    traffic_payload,
    storage_encryption,
    repository,
):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, _ = upload_setup
    record = store.records[response.json()["upload_id"]]
    key = record["descriptor"]["object_key"]
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)
    loader = UploadLoader(cloud, store, queue, repository)
    cloud.delete = AsyncMock(side_effect=PersistenceError("delete failed"))

    async def scenario():
        await parser.process(key)
        queue.receive.return_value = queue.messages[0]
        with pytest.raises(PersistenceError):
            await loader.consume_once()
        assert record["status"] == "stored"
        queue.acknowledge.assert_not_awaited()
        cloud.delete.side_effect = None
        assert await loader.consume_once()
        repository.insert_queued_traffic_logs.assert_awaited_once()
        assert record["raw_deleted"]

    asyncio.run(scenario())


def test_late_put_recreation_is_cleaned_without_reloading(
    upload_setup,
    traffic_payload,
    storage_encryption,
    repository,
):
    response, wire = upload(upload_setup, traffic_payload)
    _, store, cloud, objects = upload_setup
    record = store.records[response.json()["upload_id"]]
    descriptor = UploadDescriptor.model_validate(record["descriptor"])
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)
    loader = UploadLoader(cloud, store, queue, repository)

    async def scenario():
        await parser.process(descriptor.object_key)
        queue.receive.return_value = queue.messages[0]
        assert await loader.consume_once()
        assert not objects
        # An older ambiguous collector PUT completes after normal raw cleanup.
        await cloud.put(descriptor, wire)
        assert descriptor.object_key in objects
        await parser.process(descriptor.object_key)
        assert record["raw_deleted"] is False
        queue.receive.return_value = queue.messages[-1]
        assert await loader.consume_once()
        assert record["raw_deleted"] is True
        assert not objects
        repository.insert_queued_traffic_logs.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("event_source", ["minio", "gcs"])
def test_object_event_routing_and_poison_ack(
    upload_setup,
    traffic_payload,
    storage_encryption,
    event_source,
):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, _ = upload_setup
    record = store.records[response.json()["upload_id"]]
    event = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": settings.spool_bucket},
                    "object": {"key": record["descriptor"]["object_key"]},
                },
            }
        ]
    }
    if event_source == "gcs":
        event = {"bucket": settings.spool_bucket, "name": record["descriptor"]["object_key"]}
    cloud.receive = AsyncMock(
        return_value=[
            {
                "Body": json.dumps(event),
                "ReceiptHandle": "receipt",
            }
        ]
    )
    cloud.acknowledge = AsyncMock()
    parser = UploadParser(cloud, store, silver_queue(), EncryptionKeys({}), storage_encryption)
    assert asyncio.run(parser.consume_once())
    cloud.acknowledge.assert_awaited_once_with("receipt")
    cloud.receive.return_value[0]["Body"] = "invalid JSON"
    assert asyncio.run(parser.consume_once())
    cloud.dead_letter.assert_awaited_once_with("parse", None, "invalid_notification")


def test_invalid_silver_is_dead_lettered_without_blocking(upload_setup, repository):
    _, store, cloud, _ = upload_setup
    queue = silver_queue()
    queue.receive.return_value = (b"poison", {b"payload": b"invalid"})
    loader = UploadLoader(cloud, store, queue, repository)
    assert asyncio.run(loader.consume_once())
    cloud.dead_letter.assert_awaited_once_with("load", None, "invalid_silver_batch")
    queue.acknowledge.assert_awaited_once_with(b"poison")
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_upload_state_claim_uses_atomic_lease_and_majority():
    database = Mock()
    collection = AsyncMock()
    database.get_collection.return_value = collection
    store = UploadStateStore(database)
    asyncio.run(store.claim("upload", "load"))
    query, update = collection.find_one_and_update.call_args.args
    assert query["_id"] == "upload"
    assert {"status": {"$in": ["queued"]}} in query["$or"]
    assert update["$inc"] == {"load_attempts": 1}
    assert update["$set"]["lease_until"] > datetime.now(UTC)
    assert database.get_collection.call_args.kwargs["write_concern"].document == {"w": "majority"}


def test_mongo_recovery_writes_are_rate_limited(monkeypatch):
    clock = [100.0]
    delays = []

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(settings, "spool_mongo_documents_per_second", 100)
    monkeypatch.setattr("app.services.upload_pipeline.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.services.upload_pipeline.asyncio.sleep", sleep)
    limiter = MongoWriteLimiter()

    async def scenario():
        await limiter.wait(500)
        clock[0] += 1
        await limiter.wait(100)
        assert delays == [0, 4]

    asyncio.run(scenario())


def test_pubsub_distinguishes_empty_poll_from_lease_extension_failure(upload_setup):
    cloud = upload_setup[2]
    cloud.subscriber.pull.side_effect = DeadlineExceeded("empty long poll")
    assert asyncio.run(cloud.receive()) == []
    cloud.subscriber.pull.side_effect = None
    message = Mock(ack_id="receipt")
    message.message.data = b"{}"
    message.message.attributes = {}
    cloud.subscriber.pull.return_value.received_messages = [message]
    cloud.subscriber.modify_ack_deadline.side_effect = DeadlineExceeded("lease failed")
    with pytest.raises(PersistenceError, match="extend"):
        asyncio.run(cloud.receive())
    cloud.subscriber.acknowledge.assert_not_called()


def silver_document(batch_id):
    return {
        "_id": ObjectId(),
        "ingestion": {
            "batch_id": batch_id,
            "received_at": datetime.now(UTC).replace(microsecond=123000),
        },
        "logdata": {},
    }


def test_silver_pubsub_roundtrip_ack_and_retry_are_subscription_scoped(upload_setup):
    cloud = upload_setup[2]
    queue = UploadQueue(cloud)
    batch_id = str(uuid4())
    documents = [silver_document(batch_id)]

    async def scenario():
        await queue.publish(batch_id, documents)
        args = cloud.publisher.publish.call_args
        topic, payload = args.args
        assert topic == settings.spool_silver_topic
        assert args.kwargs == {
            "kind": "batch",
            "encoding": "gzip-bson-v2",
            "upload_id": batch_id,
        }
        cloud.publisher.publish.return_value.result.assert_called_once_with(timeout=25)
        message = Mock(ack_id="silver-receipt")
        message.message.data = payload
        message.message.attributes = args.kwargs
        cloud.subscriber.pull.return_value.received_messages = [message]
        queued = await queue.receive()
        assert queue.decode(*queued).documents == documents
        assert (
            cloud.subscriber.pull.call_args.kwargs["request"]["subscription"] == queue.subscription
        )
        await queue.retry(queued[0])
        assert cloud.subscriber.modify_ack_deadline.call_args.kwargs["request"] == {
            "subscription": queue.subscription,
            "ack_ids": ["silver-receipt"],
            "ack_deadline_seconds": 0,
        }
        await queue.acknowledge(queued[0])
        assert cloud.subscriber.acknowledge.call_args.kwargs["request"] == {
            "subscription": queue.subscription,
            "ack_ids": ["silver-receipt"],
        }

    asyncio.run(scenario())


def test_silver_publish_failure_keeps_raw_recoverable(
    upload_setup, traffic_payload, storage_encryption
):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, objects = upload_setup
    record = store.records[response.json()["upload_id"]]
    queue = UploadQueue(cloud)
    cloud.publisher.publish.return_value.result.side_effect = DeadlineExceeded("unavailable")
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)

    async def scenario():
        assert not await parser.process(record["descriptor"]["object_key"])
        assert record["status"] == "received"
        assert record["descriptor"]["object_key"] in objects
        cloud.subscriber.acknowledge.assert_not_called()
        cloud.publisher.publish.return_value.result.side_effect = None
        assert await parser.process(record["descriptor"]["object_key"])
        assert record["status"] == "queued"
        assert record["descriptor"]["object_key"] in objects

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["truncated", "trailing", "identity", "encoding", "expanded"])
def test_silver_decode_rejects_malformed_or_oversized_transport(damage, monkeypatch):
    batch_id = str(uuid4())
    fields = {
        b"payload": encode_silver(batch_id, [silver_document(batch_id)]),
        b"kind": b"batch",
        b"encoding": b"gzip-bson-v2",
        b"upload_id": batch_id.encode(),
    }
    if damage == "truncated":
        fields[b"payload"] = fields[b"payload"][:-1]
    elif damage == "trailing":
        fields[b"payload"] += b"unexpected member"
    elif damage == "identity":
        fields[b"upload_id"] = b"another-upload"
    elif damage == "encoding":
        fields[b"encoding"] = b"unknown"
    else:
        monkeypatch.setattr("app.services.upload_queue.MAX_SILVER_BYTES", 32)
    with pytest.raises(InvalidQueuedBatch):
        UploadQueue.decode(b"receipt", fields)


def test_silver_expanded_encrypted_batch_fits_pubsub_limit():
    batch_id = str(uuid4())
    document = silver_document(batch_id)
    # Encryption expands a valid 8 MiB repetitive body to incompressible ciphertext.
    document["storage_encryption"] = {
        "ciphertext": "vault:v1:" + base64.b64encode(os.urandom(8 * 1024 * 1024)).decode()
    }
    payload = encode_silver(batch_id, [document])
    assert len(payload) < 10_000_000
    assert UploadQueue.decode(
        b"receipt",
        {
            b"payload": payload,
            b"kind": b"batch",
            b"encoding": b"gzip-bson-v2",
            b"upload_id": batch_id.encode(),
        },
    ).documents == [document]
    assert len(BSON.encode(document)) < MAX_SILVER_BYTES


def test_silver_oversize_routes_parse_dlq_and_preserves_raw(
    upload_setup, traffic_payload, storage_encryption, monkeypatch
):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, objects = upload_setup
    record = store.records[response.json()["upload_id"]]
    queue = silver_queue()
    queue.publish.side_effect = SilverPayloadTooLarge("oversized")
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)
    assert asyncio.run(parser.process(record["descriptor"]["object_key"]))
    assert record["status"] == "parse_failed"
    assert record["descriptor"]["object_key"] in objects
    cloud.dead_letter.assert_awaited_once_with("parse", record["_id"], "silver_payload_too_large")
    monkeypatch.setattr("app.services.upload_queue.MAX_PUBSUB_BYTES", 8)
    with pytest.raises(SilverPayloadTooLarge):
        encode_silver(str(uuid4()), [{"body": "too large"}])


def test_loader_releases_delivery_when_parser_has_not_finished(
    upload_setup, traffic_payload, storage_encryption, repository
):
    response, _ = upload(upload_setup, traffic_payload)
    _, store, cloud, _ = upload_setup
    record = store.records[response.json()["upload_id"]]
    queue = silver_queue()
    parser = UploadParser(cloud, store, queue, EncryptionKeys({}), storage_encryption)
    loader = UploadLoader(cloud, store, queue, repository)

    async def scenario():
        await parser.process(record["descriptor"]["object_key"])
        record["status"] = "parsing"
        queue.receive.return_value = queue.messages[0]
        assert not await loader.consume_once()
        queue.retry.assert_awaited_once_with(queue.messages[0][0])
        queue.acknowledge.assert_not_awaited()
        repository.insert_queued_traffic_logs.assert_not_awaited()

    asyncio.run(scenario())


def test_loader_waits_for_rate_capacity_before_pulling(upload_setup, repository):
    _, store, cloud, _ = upload_setup
    queue = silver_queue()
    loader = UploadLoader(cloud, store, queue, repository)
    loader.limiter.wait = AsyncMock()

    async def receive():
        loader.limiter.wait.assert_awaited_once_with(0)

    queue.receive.side_effect = receive
    assert not asyncio.run(loader.consume_once())


@pytest.mark.skipif(
    not os.getenv("PUBSUB_EMULATOR_HOST"), reason="Set PUBSUB_EMULATOR_HOST for silver delivery"
)
def test_real_pubsub_retains_stalled_work_and_redelivers_expired_delivery(monkeypatch):
    project = "projects/logger-local"
    suffix = uuid4().hex
    monkeypatch.setattr(settings, "spool_silver_topic", f"{project}/topics/test-silver-{suffix}")
    monkeypatch.setattr(
        settings, "spool_silver_subscription", f"{project}/subscriptions/test-silver-{suffix}"
    )

    async def scenario():
        cloud = UploadCloud()
        queue = UploadQueue(cloud)
        try:
            await asyncio.to_thread(cloud.publisher.create_topic, name=queue.topic)
            await asyncio.to_thread(
                cloud.subscriber.create_subscription,
                request={
                    "name": queue.subscription,
                    "topic": queue.topic,
                    "ack_deadline_seconds": 10,
                    "message_retention_duration": {"seconds": 1209600},
                },
            )
            batch_id = str(uuid4())
            await queue.publish(batch_id, [silver_document(batch_id)])
            initial = await queue.receive()
            assert queue.decode(*initial).batch_id == batch_id
            await asyncio.to_thread(
                cloud.subscriber.modify_ack_deadline,
                subscription=queue.subscription,
                ack_ids=[initial[0].decode()],
                ack_deadline_seconds=1,
            )
            await asyncio.sleep(1.1)
            replacement = await queue.receive()
            assert queue.decode(*replacement).batch_id == batch_id
            await queue.acknowledge(replacement[0])
            for _ in range(10):
                await queue.publish(batch_id, [silver_document(batch_id)])
            # No Redis-style MAXLEN: an idle loader does not trim queued batches.
            for _ in range(10):
                queued = await queue.receive()
                assert queued is not None
                await queue.acknowledge(queued[0])
        finally:
            await asyncio.to_thread(
                cloud.subscriber.delete_subscription, subscription=queue.subscription
            )
            await asyncio.to_thread(cloud.publisher.delete_topic, topic=queue.topic)
            await cloud.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for spool leases")
def test_real_mongo_competing_claims_replay_and_partial_write_recovery(
    monkeypatch,
    traffic_payload,
    trusted_context,
):
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)

    async def scenario():
        name = "test_spool_" + uuid4().hex
        repository = MongoRepository(uri=os.environ["MONGO_TEST_URI"], db_name=name)
        store = UploadStateStore(repository.database)
        wire = gzip.compress(json.dumps({"logs": [traffic_payload, traffic_payload]}).encode())
        descriptor = UploadDescriptor(
            upload_id=str(uuid4()),
            object_key="raw/test.upload",
            device_id=trusted_context.identity.device_id,
            client_id=None,
            source_ip=trusted_context.source_ip,
            received_at=datetime.now(UTC).replace(microsecond=123000),
            encrypted=False,
            sha256=hashlib.sha256(wire).hexdigest(),
            wire_bytes=len(wire),
            policy=StoragePolicy(),
        )
        try:
            await repository.ensure_indexes()
            await store.initialize()
            reservations = await asyncio.gather(*[store.reserve(descriptor) for _ in range(5)])
            assert sum(created for _, created in reservations) == 1
            await store.received(descriptor.upload_id)
            claims = await asyncio.gather(
                *[store.claim(descriptor.upload_id, "parse") for _ in range(5)]
            )
            assert sum(record is not None for record in claims) == 1
            winner = next(record for record in claims if record)
            await store.finish(winner, status="queued", accepted_count=2)
            loader = await store.claim(descriptor.upload_id, "load")
            assert loader is not None
            assert await store.claim(descriptor.upload_id, "load") is None
            documents, _ = normalize_upload(
                descriptor.model_dump_json().encode() + b"\n" + wire,
                descriptor,
                EncryptionKeys({}),
            )
            await repository.insert_traffic_logs(documents[:1])  # Interrupted unordered write.
            await repository.insert_queued_traffic_logs(documents)
            await repository.insert_queued_traffic_logs(documents)
            assert await repository.traffic_logs.count_documents({}) == 2
            await store.collection.update_one(
                {"_id": descriptor.upload_id},
                {"$set": {"lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
            )
            recovered = await store.claim(descriptor.upload_id, "load")
            assert recovered is not None
            with pytest.raises(PersistenceError, match="lease expired"):
                await store.finish(loader, status="stored")
            await store.finish(recovered, status="stored")
        finally:
            await repository.client.drop_database(name)
            await repository.close()

    asyncio.run(scenario())


def test_gcs_uses_atomic_create_and_idempotent_cleanup(upload_setup, traffic_payload, monkeypatch):
    response, wire = upload(upload_setup, traffic_payload)
    _, store, _, _ = upload_setup
    descriptor = UploadDescriptor.model_validate(
        store.records[response.json()["upload_id"]]["descriptor"]
    )
    monkeypatch.setattr(settings, "spool_storage_backend", "gcs")
    objects = Mock()
    blob = objects.bucket.return_value.blob.return_value
    blob.size = len(wire) + 4096
    blob.generation = 1
    body = descriptor.model_dump_json().encode() + b"\n" + wire
    blob.download_as_bytes.return_value = body
    cloud = UploadCloud(objects, Mock(), Mock())

    async def scenario():
        await cloud.put(descriptor, wire)
        assert blob.upload_from_string.call_args.kwargs["if_generation_match"] == 0
        blob.upload_from_string.side_effect = PreconditionFailed("already exists")
        await cloud.put(descriptor, wire)
        assert blob.download_as_bytes.call_args.kwargs["if_generation_match"] == 1
        blob.delete.side_effect = NotFound("already deleted")
        await cloud.delete(descriptor.object_key)

    asyncio.run(scenario())


@pytest.mark.skipif(
    not all(
        os.getenv(name)
        for name in ("MINIO_TEST_ENDPOINT", "MONGO_TEST_URI", "PUBSUB_EMULATOR_HOST")
    ),
    reason="Set MinIO, MongoDB and Pub/Sub emulator test endpoints",
)
@pytest.mark.parametrize("encrypted", [False, True])
def test_real_minio_notification_to_mongo_and_cleanup(
    authorized_client,
    repository,
    storage_encryption,
    traffic_payload,
    encryption_keys,
    monkeypatch,
    encrypted,
):
    monkeypatch.setattr(settings, "spool_storage_backend", "minio")
    monkeypatch.setattr(settings, "spool_endpoint_url", os.environ["MINIO_TEST_ENDPOINT"])
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    repository.get_device_configuration.return_value["encryption_mode"] = "optional"
    traffic_payload["timestamp"] = datetime.now(UTC).isoformat()

    async def scenario():
        name = "test_minio_" + uuid4().hex
        mongo = MongoRepository(uri=os.environ["MONGO_TEST_URI"], db_name=name)
        store = UploadStateStore(mongo.database)
        cloud = UploadCloud()
        queue = UploadQueue(cloud)
        descriptor = None
        try:
            await mongo.ensure_indexes()
            await store.initialize()
            app.dependency_overrides[get_upload_cloud] = lambda: cloud
            app.dependency_overrides[get_upload_store] = lambda: store
            wire = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
            headers = HEADERS
            if encrypted:
                wire = jwe.encrypt_compact(
                    {
                        "alg": encryption_keys.algorithm,
                        "enc": encryption_keys.encryption,
                        "kid": encryption_keys.active_key_id,
                        "cty": "application/gzip",
                    },
                    wire,
                    encryption_keys.keys[encryption_keys.active_key_id],
                    registry=encryption_keys.registry,
                ).encode()
                headers = {"Content-Type": "application/jose"}
            async with AsyncClient(
                transport=ASGITransport(app),
                base_url="https://testserver",
            ) as client:
                response = await client.post(URL, content=wire, headers=headers)
                assert response.status_code == 202, response.text
                upload_id = response.json()["upload_id"]
                record = await store.get(upload_id)
                descriptor = UploadDescriptor.model_validate(record["descriptor"])
                assert (await cloud.get(descriptor.object_key)).split(b"\n", 1)[1] == wire
                parser = UploadParser(cloud, store, queue, encryption_keys, storage_encryption)
                async with asyncio.timeout(45):
                    while (await store.get(upload_id))["status"] != "queued":
                        await parser.consume_once()
                loader = UploadLoader(cloud, store, queue, mongo)
                assert await loader.consume_once()
                status = await client.get(f"{URL}/{upload_id}")
                assert status.json() == {
                    "upload_id": upload_id,
                    "status": "stored",
                    "accepted_count": 1,
                    "error_code": None,
                    "raw_deleted": True,
                }
                assert await mongo.traffic_logs.count_documents({}) == 1
                log = await mongo.traffic_logs.find_one({})
                assert "body" not in log["logdata"]
                assert log["storage_encryption"]["ciphertext"].startswith("vault:v1:")
                with pytest.raises(PersistenceError):
                    await cloud.get(descriptor.object_key)
                # Late duplicate notifications are ack-and-skip even after raw deletion.
                await cloud.replay_event(descriptor)
                assert await parser.consume_once()
                assert await loader.consume_once()
                assert await mongo.traffic_logs.count_documents({}) == 1
        finally:
            if descriptor is not None:
                await cloud.delete(descriptor.object_key)
            await cloud.close()
            await mongo.client.drop_database(name)
            await mongo.close()

    asyncio.run(scenario())
