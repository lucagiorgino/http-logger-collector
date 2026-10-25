import asyncio
import gzip
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from anyio.to_thread import current_default_thread_limiter
from bson import BSON
from httpx2 import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.db.mongo import MongoRepository, PersistenceError
from app.deps import get_ingestion_publisher, get_mongo_repository
from app.main import app
from app.schemas.traffic import TrafficLogRequest
from app.services.redis_queue import InvalidQueuedBatch, RedisStreamQueue
from app.services.traffic_service import normalize_traffic_log
from app.worker import consume_once


@pytest.fixture
def document(traffic_payload, trusted_context):
    document = normalize_traffic_log(
        TrafficLogRequest.model_validate(traffic_payload), str(uuid4()), trusted_context
    )
    document["received_at"] = document["received_at"].replace(microsecond=123000)
    return document


@pytest.fixture
def queue():
    client = Mock()
    pipeline = AsyncMock()
    pipeline.__aenter__.return_value = pipeline
    for command in ("eval", "execute_command", "xack", "xdel"):
        setattr(pipeline, command, Mock())
    pipeline.execute.return_value = [b"1-0", [1, 0]]
    client.pipeline.return_value = pipeline
    client.info = AsyncMock(
        side_effect=lambda section: (
            {"aof_enabled": 1} if section == "persistence" else {"maxmemory_policy": "noeviction"}
        )
    )
    client.execute_command = AsyncMock(return_value=[1, 0])
    client.xgroup_create = AsyncMock()
    client.xautoclaim = AsyncMock(return_value=[b"0-0", [], []])
    client.xreadgroup = AsyncMock(return_value=[])
    return RedisStreamQueue(client), client, pipeline


def test_publisher_preserves_bson_and_waits_for_durability_on_the_same_connection(queue, document):
    publisher, client, pipeline = queue
    receipt = asyncio.run(publisher.publish(document["batch_id"], [document]))
    assert receipt.status == "queued"
    assert receipt.accepted_count == 1
    client.pipeline.assert_called_once_with(transaction=False)
    pipeline.execute_command.assert_called_once_with("WAITAOF", 1, 0, settings.redis_aof_timeout_ms)
    payload = pipeline.eval.call_args.args[-1]
    decoded = publisher.decode(b"1-0", {b"payload": payload})
    assert decoded.documents == [document]


@pytest.mark.parametrize("outcome", [[None, [1, 0]], [b"1-0", [0, 0]]])
def test_full_or_unconfirmed_queue_is_not_acknowledged(queue, document, outcome):
    publisher, _, pipeline = queue
    pipeline.execute.return_value = outcome
    with pytest.raises(PersistenceError):
        asyncio.run(publisher.publish(document["batch_id"], [document]))


def test_collector_returns_queued_without_waiting_for_mongo_writes(
    authorized_client, repository, traffic_payload, queue
):
    publisher, _, pipeline = queue
    app.dependency_overrides[get_ingestion_publisher] = lambda: publisher
    repository.insert_traffic_logs.side_effect = AssertionError("Collector must not write logs")
    response = authorized_client.post(
        "/api/v1/traffic-log-batches",
        content=gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    repository.insert_traffic_logs.assert_not_awaited()
    payload = pipeline.eval.call_args.args[-1]
    queued = publisher.decode(b"1-0", {b"payload": payload}).documents[0]
    assert "body" not in queued
    assert queued["storage_encryption"]["ciphertext"].startswith("vault:v1:")
    assert traffic_payload["body"].encode() not in payload


@pytest.mark.parametrize("encrypted", [False, True])
def test_concurrent_collectors_progress_with_stalled_mongo_and_full_fastapi_thread_pool(
    repository,
    storage_encryption,
    encryption_keys,
    traffic_payload,
    queue,
    document,
    monkeypatch,
    encrypted,
):
    from joserfc import jwe

    stream, redis, pipeline = queue
    repository.get_device_configuration.return_value = {
        "version": 1,
        "encryption_mode": "optional",
    }
    wire = gzip.compress(json.dumps({"logs": [traffic_payload]}).encode())
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
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

    async def claims():
        return {"sub": "test-device"}

    monkeypatch.setitem(app.dependency_overrides, require_bearer_token, claims)
    for name, value in (
        ("mongo_repository", repository),
        ("ingestion_queue", stream),
        ("encryption_keys", encryption_keys),
        ("storage_encryption", storage_encryption),
    ):
        monkeypatch.setattr(app.state, name, value, raising=False)
    redis.xautoclaim.return_value = [
        b"0-0",
        [
            (
                b"1-0",
                {
                    b"payload": BSON.encode(
                        {
                            "version": 1,
                            "batch_id": document["batch_id"],
                            "documents": [document],
                        }
                    )
                },
            )
        ],
        [],
    ]

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        writer = AsyncMock()

        async def blocked_write(documents):
            entered.set()
            await release.wait()

        writer.insert_queued_traffic_logs.side_effect = blocked_write
        worker = asyncio.create_task(consume_once(stream, writer, "slow-worker"))
        limiter = current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                limiter.total_tokens = 1
                async with (
                    limiter,
                    AsyncClient(
                        transport=ASGITransport(app=app), base_url="https://testserver"
                    ) as client,
                ):
                    responses = await asyncio.gather(
                        *(
                            client.post(
                                "/api/v1/traffic-log-batches", content=wire, headers=headers
                            )
                            for _ in range(10)
                        ),
                        client.get("/health"),
                    )
                assert all(response.status_code == 200 for response in responses)
                assert all(response.json()["status"] == "queued" for response in responses[:-1])
                assert len({response.json()["batch_id"] for response in responses[:-1]}) == 10
                assert not worker.done()
                pipeline.xack.assert_not_called()
                pipeline.xdel.assert_not_called()
                repository.insert_traffic_logs.assert_not_awaited()
                repository.insert_queued_traffic_logs.assert_not_awaited()
        finally:
            limiter.total_tokens = original_tokens
            release.set()
            await worker
        pipeline.xack.assert_called_once_with(stream.stream, stream.group, b"1-0")

    asyncio.run(scenario())


def test_redis_outage_returns_sanitized_503_without_mongo_fallback(
    authorized_client, repository, traffic_payload, queue
):
    publisher, _, pipeline = queue
    pipeline.execute.side_effect = RedisConnectionError("private connection details")
    app.dependency_overrides[get_ingestion_publisher] = lambda: publisher
    response = authorized_client.post(
        "/api/v1/traffic-log-batches",
        content=gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 503
    assert "private connection details" not in response.text
    repository.insert_traffic_logs.assert_not_awaited()


def test_worker_does_not_acknowledge_failed_writes(queue, document):
    stream, client, pipeline = queue
    fields = {
        b"payload": BSON.encode(
            {"version": 1, "batch_id": document["batch_id"], "documents": [document]}
        )
    }
    client.xreadgroup.return_value = [(b"stream", [(b"1-0", fields)])]
    repository = AsyncMock()
    repository.insert_queued_traffic_logs.side_effect = PersistenceError("write failed")
    with pytest.raises(PersistenceError):
        asyncio.run(consume_once(stream, repository, "worker"))
    pipeline.xack.assert_not_called()
    pipeline.xdel.assert_not_called()


def test_worker_acknowledges_only_after_successful_bulk_write(queue, document):
    stream, client, pipeline = queue
    fields = {
        b"payload": BSON.encode(
            {"version": 1, "batch_id": document["batch_id"], "documents": [document]}
        )
    }
    client.xautoclaim.return_value = [b"0-0", [(b"1-0", fields)], []]
    repository = AsyncMock()

    async def persisted(documents):
        pipeline.xack.assert_not_called()
        assert documents == [document]

    repository.insert_queued_traffic_logs.side_effect = persisted
    assert asyncio.run(consume_once(stream, repository, "replacement-worker"))
    client.xreadgroup.assert_not_awaited()
    pipeline.xack.assert_called_once_with(stream.stream, stream.group, b"1-0")
    pipeline.xdel.assert_called_once_with(stream.stream, b"1-0")


def test_pending_recovery_advances_past_busy_or_poison_entries(queue):
    stream, client, _ = queue
    client.xautoclaim.side_effect = [[b"20-0", [], []], [b"0-0", [], []]]

    async def scenario():
        assert await stream.receive("worker") is None
        assert await stream.receive("worker") is None

    asyncio.run(scenario())
    assert client.xautoclaim.await_args_list[0].kwargs["start_id"] == "0-0"
    assert client.xautoclaim.await_args_list[1].kwargs["start_id"] == b"20-0"


def test_malformed_message_is_logged_and_left_pending(queue, caplog):
    stream, client, pipeline = queue
    client.xautoclaim.return_value = [b"0-0", [(b"7-0", {b"payload": b"private-invalid"})], []]
    repository = AsyncMock()
    assert not asyncio.run(consume_once(stream, repository, "worker"))
    assert "7-0" in caplog.text
    assert "private-invalid" not in caplog.text
    repository.insert_queued_traffic_logs.assert_not_awaited()
    pipeline.xack.assert_not_called()


def test_redis_requires_aof_at_startup(queue):
    stream, client, _ = queue
    client.info.side_effect = None
    client.info.return_value = {"aof_enabled": 0}
    with pytest.raises(PersistenceError, match="AOF"):
        asyncio.run(stream.initialize())
    client.xgroup_create.assert_not_awaited()


def test_redis_rejects_policies_that_can_evict_accepted_logs(queue):
    stream, client, _ = queue
    client.info.side_effect = [{"aof_enabled": 1}, {"maxmemory_policy": "allkeys-lru"}]
    with pytest.raises(PersistenceError, match="noeviction"):
        asyncio.run(stream.initialize())
    client.xgroup_create.assert_not_awaited()


def test_unknown_queue_envelope_version_is_rejected(document):
    payload = BSON.encode(
        {"version": 999, "batch_id": document["batch_id"], "documents": [document]}
    )
    with pytest.raises(InvalidQueuedBatch):
        RedisStreamQueue.decode(b"1-0", {b"payload": payload})


@asynccontextmanager
async def live_queue():
    client = Redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=False)
    queue = RedisStreamQueue(client)
    try:
        await queue.initialize()
        yield queue
    finally:
        await client.delete(queue.stream)
        await queue.close()


@pytest.fixture
def isolated_stream(monkeypatch):
    monkeypatch.setattr(settings, "redis_stream", "ingestion-test-" + uuid4().hex)
    monkeypatch.setattr(settings, "redis_group", "test-workers")


@pytest.mark.skipif(not os.getenv("REDIS_TEST_URL"), reason="Set REDIS_TEST_URL for Redis")
def test_real_stream_reclaims_abandoned_messages_and_never_trims_pending(
    isolated_stream, monkeypatch, document
):
    monkeypatch.setattr(settings, "redis_max_batches", 1)

    async def scenario():
        async with live_queue() as queue:
            await queue.publish(document["batch_id"], [document])
            await queue.client.xreadgroup(queue.group, "crashed", {queue.stream: ">"}, count=1)
            with pytest.raises(PersistenceError, match="full"):
                await queue.publish(document["batch_id"], [document])
            assert await queue.client.xlen(queue.stream) == 1
            monkeypatch.setattr(settings, "worker_reclaim_idle_ms", 1)
            await asyncio.sleep(0.02)
            repository = AsyncMock()
            assert await consume_once(queue, repository, "replacement")
            repository.insert_queued_traffic_logs.assert_awaited_once_with([document])
            assert await queue.client.xlen(queue.stream) == 0
            assert (await queue.client.xpending(queue.stream, queue.group))["pending"] == 0

    asyncio.run(scenario())


@pytest.mark.skipif(
    not (os.getenv("REDIS_TEST_URL") and os.getenv("MONGO_TEST_URI")),
    reason="Set REDIS_TEST_URL and MONGO_TEST_URI for queued persistence",
)
def test_real_collector_worker_and_recovery_after_mongo_write_before_ack(
    isolated_stream, authorized_client, monkeypatch, traffic_payload
):
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")
    traffic_payload["timestamp"] = datetime.now(UTC).isoformat()

    async def scenario():
        repository = MongoRepository(
            uri=os.environ["MONGO_TEST_URI"], db_name="redis_ingestion_test_" + uuid4().hex
        )
        try:
            await repository.ensure_indexes()
            async with live_queue() as queue:
                app.dependency_overrides[get_ingestion_publisher] = lambda: queue
                app.dependency_overrides[get_mongo_repository] = lambda: repository
                async with AsyncClient(
                    transport=ASGITransport(app), base_url="https://testserver"
                ) as client:
                    response = await client.post(
                        "/api/v1/traffic-log-batches",
                        content=gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
                        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                    )
                    assert response.status_code == 200
                    assert response.json()["status"] == "queued"
                    assert await repository.traffic_logs.count_documents({}) == 0
                    acknowledge = queue.acknowledge
                    queue.acknowledge = AsyncMock(side_effect=PersistenceError("lost connection"))
                    with pytest.raises(PersistenceError):
                        await consume_once(queue, repository, "interrupted-worker")
                    assert await repository.traffic_logs.count_documents({}) == 1
                    assert (await queue.client.xpending(queue.stream, queue.group))["pending"] == 1
                    queue.acknowledge = acknowledge
                    monkeypatch.setattr(settings, "worker_reclaim_idle_ms", 1)
                    await asyncio.sleep(0.02)
                    assert await consume_once(queue, repository, "replacement-worker")
                    assert await repository.traffic_logs.count_documents({}) == 1
                    assert await queue.client.xlen(queue.stream) == 0
                    stored = await repository.traffic_logs.find_one({})
                    assert stored["batch_id"] == response.json()["batch_id"]
                    detail = await client.get(f"/api/v1/traffic-logs/{stored['_id']}")
                    assert detail.status_code == 200
                    assert detail.json()["body"] == traffic_payload["body"]
        finally:
            await repository.client.drop_database(repository.database.name)
            await repository.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not (os.getenv("REDIS_TEST_URL") and os.getenv("MONGO_TEST_URI")),
    reason="Set REDIS_TEST_URL and MONGO_TEST_URI for the standalone worker",
)
def test_standalone_worker_consumes_without_auth0_or_kms_and_stops_gracefully(
    isolated_stream, document, tmp_path
):
    environment_file = tmp_path / "worker.env"
    environment_file.write_text("")
    database_name = "redis_worker_process_test_" + uuid4().hex
    document["timestamp"] = datetime.now(UTC)

    async def scenario():
        repository = MongoRepository(uri=os.environ["MONGO_TEST_URI"], db_name=database_name)
        try:
            async with live_queue() as queue:
                environment = {
                    **os.environ,
                    "ENV_FILE": str(environment_file),
                    "REDIS_URL": os.environ["REDIS_TEST_URL"],
                    "REDIS_STREAM": queue.stream,
                    "REDIS_GROUP": queue.group,
                    "MONGO_URI": os.environ["MONGO_TEST_URI"],
                    "MONGO_DB_NAME": database_name,
                    "MONGO_ALLOW_PRIMARY_READS": "false",
                    "AUTH0_DOMAIN": "",
                    "AUTH0_AUDIENCE": "",
                    "OPENBAO_ADDR": "https://unavailable.invalid",
                }
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.worker",
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    await queue.publish(document["batch_id"], [document])
                    async with asyncio.timeout(15):
                        while (
                            await repository.traffic_logs.count_documents({}) != 1
                            or await queue.client.xlen(queue.stream) != 0
                        ):
                            assert process.returncode is None, (
                                "Worker exited before persisting the batch"
                            )
                            await asyncio.sleep(0.05)
                    assert await queue.client.xlen(queue.stream) == 0
                finally:
                    if process.returncode is None:
                        process.terminate()
                    try:
                        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
                    except TimeoutError:
                        process.kill()
                        await process.communicate()
                        pytest.fail("Worker did not stop gracefully")
                    assert process.returncode == 0, output.decode()
        finally:
            await repository.client.drop_database(database_name)
            await repository.close()

    asyncio.run(scenario())
