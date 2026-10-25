import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import Forbidden

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.db.mongo import MongoRepository, PersistenceError
from app.main import app
from app.services.pipeline_status import probe
from app.services.upload_cloud import UploadCloud
from app.spool_worker import run_loop

URL = "/api/v1/status"


@pytest.fixture
def status_client(authorized_client, repository, upload_harness):
    now = datetime.now(UTC)
    repository.pipeline_snapshot.return_value = {
        "workers": [
            {"role": role, "status": "healthy", "seen_at": now} for role in ("parse", "load")
        ],
        "uploads": [],
    }
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "operator",
        "permissions": ["manage:devices"],
    }
    return authorized_client, repository, upload_harness.cloud


def test_pipeline_status_requires_authentication_and_management(authorized_client, repository):
    assert authorized_client.get(URL).status_code == 403
    repository.pipeline_snapshot.assert_not_awaited()
    app.dependency_overrides.pop(require_bearer_token)
    assert TestClient(app).get(URL).status_code == 401


@pytest.mark.parametrize("backend", ["minio", "gcs"])
def test_pipeline_checks_are_read_only_and_portable(status_client, backend):
    client, repository, cloud = status_client
    cloud.backend = backend
    response = client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["deployment"] == ("local" if backend == "minio" else "gcp")
    assert result["status"] == "healthy"
    assert [node["id"] for node in result["nodes"]] == [
        "collector",
        "raw",
        "events",
        "parse",
        "silver",
        "load",
        "mongo",
        "cleanup",
    ]
    assert all(node["status"] == "healthy" for node in result["nodes"])
    cloud.check_storage.assert_awaited_once()
    assert cloud.check_subscription.await_count == 2
    cloud.receive.assert_not_awaited()
    repository.check_analysis_connection.assert_awaited_once()


def test_missing_stale_and_scaled_worker_heartbeats_are_not_assumed_healthy(status_client):
    client, repository, _ = status_client
    now = datetime.now(UTC)
    snapshot = repository.pipeline_snapshot.return_value
    snapshot["workers"] = [
        {"role": "parse", "status": "healthy", "seen_at": now - timedelta(hours=1)},
        {"role": "parse", "status": "healthy", "seen_at": now},
    ]
    nodes = {node["id"]: node for node in client.get(URL).json()["nodes"]}
    assert nodes["parse"]["status"] == "healthy"
    assert nodes["load"]["status"] == "unknown"
    snapshot["workers"].pop()
    nodes = {node["id"]: node for node in client.get(URL).json()["nodes"]}
    assert nodes["parse"]["status"] == "unavailable"


def test_stalled_uploads_failures_and_cleanup_degrade_corresponding_stages(status_client):
    client, repository, _ = status_client
    repository.pipeline_snapshot.return_value["uploads"] = [
        {"_id": state, "count": 2, "oldest": datetime.now(UTC) - timedelta(hours=1)}
        for state in ("received", "queued", "parse_failed", "stored")
    ]
    nodes = {node["id"]: node for node in client.get(URL).json()["nodes"]}
    for stage in ("events", "silver", "parse", "cleanup"):
        assert nodes[stage]["status"] == "degraded"
    assert nodes["events"]["pending"] == 2
    assert nodes["cleanup"]["pending"] == 2


def test_failed_probes_return_partial_status_without_private_error_details(status_client, caplog):
    client, repository, cloud = status_client
    repository.pipeline_snapshot.side_effect = PersistenceError("private database configuration")
    cloud.check_storage.side_effect = PersistenceError("private bucket information")
    response = client.get(URL)
    assert response.status_code == 200
    nodes = {node["id"]: node for node in response.json()["nodes"]}
    assert nodes["mongo"]["status"] == nodes["raw"]["status"] == "unavailable"
    assert nodes["parse"]["status"] == nodes["cleanup"]["status"] == "unknown"
    assert nodes["events"]["pending"] is None
    assert "private" not in response.text
    assert "Pipeline status check failed" in caplog.text


def test_analysis_failure_never_falls_back_to_primary(status_client):
    client, repository, _ = status_client
    repository.check_analysis_connection.side_effect = PersistenceError("No secondary")
    nodes = {node["id"]: node for node in client.get(URL).json()["nodes"]}
    assert nodes["mongo"]["status"] == "unavailable"
    assert nodes["collector"]["status"] == "healthy"


def test_metadata_probes_do_not_consume_messages_and_surface_permission_failures(monkeypatch):
    monkeypatch.setattr(settings, "spool_storage_backend", "gcs")
    objects, subscriber = Mock(), Mock()
    cloud = UploadCloud(objects, Mock(), subscriber)

    async def scenario():
        await cloud.check_storage()
        await cloud.check_subscription("test-subscription")
        subscriber.get_subscription.side_effect = Forbidden("private details")
        with pytest.raises(PersistenceError, match="metadata probe failed"):
            await cloud.check_subscription("test-subscription")

    asyncio.run(scenario())
    objects.bucket.return_value.reload.assert_called_once_with(timeout=2, retry=None)
    subscriber.pull.assert_not_called()
    subscriber.acknowledge.assert_not_called()


def test_minio_status_checks_bucket_with_bounded_separate_client(monkeypatch):
    monkeypatch.setattr(settings, "spool_storage_backend", "minio")
    client = Mock(spec=["head_bucket", "close"])
    factory = Mock(return_value=client)
    monkeypatch.setattr("app.services.upload_cloud.minio_client", factory)
    cloud = UploadCloud(Mock(), Mock(), Mock())
    asyncio.run(cloud.check_storage())
    factory.assert_called_once_with(probe=True)
    client.head_bucket.assert_called_once_with(Bucket=settings.spool_bucket)
    client.close.assert_called_once()


def test_status_probe_timeout_is_reported_explicitly():
    async def scenario():
        async def slow():
            await asyncio.sleep(30)

        assert await probe(slow, "slow test dependency") == (False, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("heartbeat_failure", [False, True])
def test_worker_reports_recovery_and_shutdown_without_monitoring_blocking_ingestion(
    monkeypatch, heartbeat_failure, caplog
):
    monkeypatch.setattr(settings, "worker_retry_seconds", 0.001)

    async def scenario():
        stop = asyncio.Event()
        attempts = 0
        reports = []

        async def consume():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PersistenceError("test dependency failed")
            stop.set()
            return True

        async def heartbeat(status):
            reports.append(status)
            if heartbeat_failure:
                raise PersistenceError("test heartbeat failed")

        await run_loop(consume, stop, heartbeat)
        assert attempts == 2
        assert reports == ["degraded", "healthy", "unavailable"]

    asyncio.run(scenario())
    if heartbeat_failure:
        assert "Worker heartbeat unavailable; ingestion continues" in caplog.text


def test_worker_heartbeats_continue_during_long_polls_or_rate_limiting(monkeypatch):
    monkeypatch.setattr("app.spool_worker.HEARTBEAT_INTERVAL", 0.01)

    async def scenario():
        stop, reported_twice = asyncio.Event(), asyncio.Event()
        reports = []

        async def consume():
            await asyncio.wait_for(reported_twice.wait(), timeout=1)
            stop.set()
            return False

        async def heartbeat(status):
            reports.append(status)
            if len(reports) == 2:
                reported_twice.set()

        await run_loop(consume, stop, heartbeat)
        assert reports[:2] == ["healthy", "healthy"]
        assert reports[-1] == "unavailable"

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv("MONGO_TEST_URI"), reason="Set MONGO_TEST_URI for status storage")
def test_real_status_storage_upserts_instances_and_excludes_completed_uploads(monkeypatch):
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    monkeypatch.setattr(settings, "app_env", "development")

    async def scenario():
        repository = MongoRepository(
            uri=os.environ["MONGO_TEST_URI"], db_name="pipeline_status_test_" + uuid4().hex
        )
        try:
            await repository.ensure_indexes()
            await repository.initialize_pipeline_status()
            await repository.report_pipeline_worker("parse", "first", "degraded")
            await repository.report_pipeline_worker("parse", "first", "healthy")
            await repository.report_pipeline_worker("load", "second", "healthy")
            await repository.pipeline_uploads.insert_many(
                [
                    {
                        "status": state,
                        "raw_deleted": deleted,
                        "updated_at": datetime.now(UTC),
                        "descriptor": {"private": "never returned"},
                    }
                    for state, deleted in [("queued", False), ("stored", False), ("stored", True)]
                ]
            )
            snapshot = await repository.pipeline_snapshot()
            assert len(snapshot["workers"]) == 2
            assert all(worker["status"] == "healthy" for worker in snapshot["workers"])
            assert {row["_id"]: row["count"] for row in snapshot["uploads"]} == {
                "queued": 1,
                "stored": 1,
            }
            assert "never returned" not in repr(snapshot)
            await repository.check_analysis_connection()
        finally:
            await repository.client.drop_database(repository.database.name)
            await repository.close()

    asyncio.run(scenario())
