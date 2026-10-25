import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from bson import ObjectId
from pymongo.errors import CollectionInvalid, OperationFailure, ServerSelectionTimeoutError
from pymongo.read_preferences import ReadPreference

from app.core.config import settings
from app.db.mongo import MongoRepository, PersistenceError
from app.schemas.workspace import WorkspaceConfiguration


@pytest.fixture
def mongo(monkeypatch):
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", False)
    monkeypatch.setattr(settings, "app_env", "production")
    repository = MongoRepository(uri="mongodb://localhost:27017/?replicaSet=rs0")
    try:
        yield repository
    finally:
        asyncio.run(repository.close())


def test_writes_use_primary_and_analysis_uses_strict_secondary(mongo):
    assert mongo.traffic_logs.read_preference == ReadPreference.PRIMARY
    assert mongo.traffic_logs.write_concern.document == {"w": "majority"}
    assert mongo.analysis_logs.read_preference == ReadPreference.SECONDARY
    assert mongo.analysis_logs.read_concern.document == {"level": "majority"}
    assert mongo.device_configurations.read_preference == ReadPreference.PRIMARY
    assert mongo.device_configurations.read_concern.document == {"level": "majority"}
    assert mongo.device_configurations.write_concern.document == {"w": "majority"}
    assert mongo.workspace_configurations.read_preference == ReadPreference.PRIMARY
    assert mongo.workspace_configurations.read_concern.document == {"level": "majority"}
    assert mongo.workspace_configurations.write_concern.document == {"w": "majority"}


def test_local_development_can_explicitly_select_primary(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    repository = MongoRepository(uri="mongodb://localhost:28017/?replicaSet=rs0")
    try:
        assert repository.analysis_logs.read_preference == ReadPreference.PRIMARY
        assert repository.traffic_logs.write_concern.document == {"w": "majority"}
    finally:
        asyncio.run(repository.close())


def test_primary_analysis_reads_are_rejected_outside_development(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "mongo_allow_primary_reads", True)
    with pytest.raises(PersistenceError, match="restricted to development"):
        MongoRepository()


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://localhost:27017",
        "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true",
    ],
)
def test_standalone_and_direct_connections_cannot_bypass_replica_routing(uri):
    with pytest.raises(PersistenceError, match="replicaSet"):
        MongoRepository(uri=uri)


@pytest.mark.parametrize(
    "creation_error", [None, CollectionInvalid("exists"), OperationFailure("exists", code=48)]
)
def test_startup_creates_and_validates_time_series_collection(mongo, creation_error):
    mongo.database = AsyncMock()
    mongo.traffic_logs = AsyncMock()
    mongo.device_configurations = AsyncMock()
    mongo.ensure_retention_indexes = AsyncMock()
    mongo.database.create_collection.side_effect = creation_error
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {"options": {"timeseries": {"timeField": "timestamp", "metaField": "metadata"}}}
        ]
    )
    mongo.database.list_collections.return_value = cursor
    asyncio.run(mongo.ensure_indexes())
    mongo.database.create_collection.assert_awaited_once_with(
        "traffic_logs",
        timeseries={"timeField": "timestamp", "metaField": "metadata", "granularity": "seconds"},
    )
    mongo.traffic_logs.create_indexes.assert_awaited_once()
    indexes = {
        index.document["name"]: dict(index.document["key"])
        for index in mongo.traffic_logs.create_indexes.call_args.args[0]
    }
    assert indexes["ingestion_received_at"] == {"ingestion.received_at": -1}
    assert indexes["ingestion_batch_record"] == {"ingestion.batch_id": 1, "_id": 1}
    mongo.device_configurations.create_index.assert_awaited_once()
    mongo.ensure_retention_indexes.assert_awaited_once()


@pytest.mark.parametrize("already_stored", [1, 2])
@pytest.mark.parametrize("legacy", [False, True])
def test_queued_batch_retry_skips_records_already_majority_persisted(mongo, already_stored, legacy):
    attribution = {"batch_id": "batch-123"}
    documents = [
        {"_id": ObjectId(), **(attribution if legacy else {"ingestion": attribution})}
        for _ in range(2)
    ]
    mongo.traffic_logs = Mock()
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=documents[:already_stored])
    collection = mongo.traffic_logs.with_options.return_value
    collection.find.return_value = cursor
    mongo.insert_traffic_logs = AsyncMock()
    asyncio.run(mongo.insert_queued_traffic_logs(documents))
    assert collection.find.call_args.args[0] == {
        ("batch_id" if legacy else "ingestion.batch_id"): "batch-123",
        "_id": {"$in": [document["_id"] for document in documents]},
    }
    if already_stored == 1:
        mongo.insert_traffic_logs.assert_awaited_once_with(documents[1:])
    else:
        mongo.insert_traffic_logs.assert_not_awaited()


def test_retention_indexes_separate_ordinary_legacy_and_suspicious_logs(mongo):
    mongo.traffic_logs = AsyncMock()
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[])
    mongo.traffic_logs.list_indexes.return_value = cursor
    mongo.get_workspace_configuration = AsyncMock(
        return_value=WorkspaceConfiguration().model_dump()
    )
    asyncio.run(mongo.ensure_retention_indexes())
    assert mongo.traffic_logs.create_index.await_count == 2
    ordinary, suspicious = mongo.traffic_logs.create_index.await_args_list
    assert ordinary.kwargs == {
        "name": "retention_ordinary",
        "expireAfterSeconds": 7 * 86400,
        "partialFilterExpression": {"metadata.suspicious": {"$in": [False, None]}},
    }
    assert suspicious.kwargs == {
        "name": "retention_suspicious",
        "expireAfterSeconds": 30 * 86400,
        "partialFilterExpression": {"metadata.suspicious": True},
    }


def test_retention_reconciles_current_policy_after_a_concurrent_update(mongo):
    mongo.traffic_logs = AsyncMock()
    mongo.database = AsyncMock()
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "name": "retention_ordinary",
                "expireAfterSeconds": 7 * 86400,
                "partialFilterExpression": {"metadata.suspicious": {"$in": [False, None]}},
            },
            {
                "name": "retention_suspicious",
                "expireAfterSeconds": 30 * 86400,
                "partialFilterExpression": {"metadata.suspicious": True},
            },
        ]
    )
    mongo.traffic_logs.list_indexes.return_value = cursor
    policy = WorkspaceConfiguration(retention_days=14)
    newer = policy.model_copy(
        update={"version": 1, "retention_enabled": False, "suspicious_retention_days": 365}
    )
    mongo.get_workspace_configuration = AsyncMock(
        side_effect=[
            policy.model_dump(),
            newer.model_dump(),
            newer.model_dump(),
            newer.model_dump(),
        ]
    )
    asyncio.run(mongo.ensure_retention_indexes())
    assert [call.args[0] for call in mongo.traffic_logs.drop_index.await_args_list] == [
        "retention_ordinary",
        "retention_ordinary",
        "retention_suspicious",
    ]
    assert mongo.traffic_logs.create_index.await_args_list[-1].kwargs["name"] == (
        "retention_suspicious"
    )
    assert mongo.traffic_logs.create_index.await_args_list[-1].kwargs["expireAfterSeconds"] == (
        365 * 86400
    )


def test_retention_index_errors_are_explicit_and_configuration_is_persisted_first(mongo):
    mongo.workspace_configurations = AsyncMock()
    mongo.workspace_configurations.find_one_and_update.return_value = {
        "_id": "workspace",
        "version": 1,
        "retention_days": 14,
    }
    mongo.ensure_retention_indexes = AsyncMock(side_effect=PersistenceError("DDL failed"))
    with pytest.raises(PersistenceError, match="DDL failed"):
        asyncio.run(mongo.update_workspace_configuration(0, {"retention_days": 14}))
    mongo.workspace_configurations.find_one_and_update.assert_awaited_once()


def test_retention_does_not_swallow_database_permission_errors(mongo):
    mongo.traffic_logs = AsyncMock()
    mongo.traffic_logs.list_indexes.side_effect = OperationFailure("unauthorized", code=13)
    mongo.get_workspace_configuration = AsyncMock(
        return_value=WorkspaceConfiguration().model_dump()
    )
    with pytest.raises(PersistenceError, match="retention"):
        asyncio.run(mongo.ensure_retention_indexes())


def test_startup_rejects_existing_non_time_series_collection(mongo):
    mongo.database = AsyncMock()
    mongo.traffic_logs = AsyncMock()
    mongo.database.create_collection.side_effect = CollectionInvalid("exists")
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[{"options": {}}])
    mongo.database.list_collections.return_value = cursor
    with pytest.raises(PersistenceError, match="time-series"):
        asyncio.run(mongo.ensure_indexes())
    mongo.traffic_logs.create_indexes.assert_not_awaited()


def test_collection_wide_ttl_cannot_override_suspicious_retention(mongo):
    mongo.database = AsyncMock()
    mongo.database.create_collection.side_effect = CollectionInvalid("exists")
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "options": {
                    "timeseries": {"timeField": "timestamp", "metaField": "metadata"},
                    "expireAfterSeconds": 604800,
                }
            }
        ]
    )
    mongo.database.list_collections.return_value = cursor
    with pytest.raises(PersistenceError, match="Collection-wide expiration"):
        asyncio.run(mongo.ensure_indexes())


def test_startup_does_not_swallow_database_permission_errors(mongo):
    mongo.database = AsyncMock()
    mongo.database.create_collection.side_effect = OperationFailure("unauthorized", code=13)
    with pytest.raises(PersistenceError, match="initialize"):
        asyncio.run(mongo.ensure_indexes())
    mongo.database.list_collections.assert_not_awaited()


def test_analysis_query_contains_device_and_stable_cursor(mongo):
    mongo.analysis_logs = Mock()
    cursor = mongo.analysis_logs.find.return_value
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[])
    position = (datetime(2026, 1, 15, tzinfo=UTC), ObjectId())
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 2, 1, tzinfo=UTC)
    asyncio.run(
        mongo.list_traffic_logs(
            device_id="device", start_time=start, end_time=end, before=position, limit=11
        )
    )
    mongo.analysis_logs.find.assert_called_once_with(
        {
            "metadata.device_id": "device",
            "timestamp": {"$gte": start, "$lt": end},
            "$or": [
                {"timestamp": {"$lt": position[0]}},
                {"timestamp": position[0], "_id": {"$lt": position[1]}},
            ],
        },
        {"logdata.body": 0, "body": 0, "storage_encryption.ciphertext": 0},
    )
    cursor.sort.assert_called_once_with([("timestamp", -1), ("_id", -1)])
    cursor.limit.assert_called_once_with(11)


def test_record_query_contains_device_when_restricted(mongo):
    mongo.analysis_logs = AsyncMock()
    record_id = ObjectId()
    asyncio.run(mongo.get_traffic_log(record_id, device_id="device"))
    mongo.analysis_logs.find_one.assert_awaited_once_with(
        {"metadata.device_id": "device", "_id": record_id}
    )


def test_database_errors_are_explicit_and_never_fall_back(mongo):
    mongo.traffic_logs = AsyncMock()
    mongo.analysis_logs = AsyncMock()
    mongo.traffic_logs.insert_many.side_effect = ServerSelectionTimeoutError("no primary")
    mongo.analysis_logs.find_one.side_effect = ServerSelectionTimeoutError("no secondary")
    with pytest.raises(PersistenceError, match="persist"):
        asyncio.run(mongo.insert_traffic_logs([{"timestamp": datetime.now(UTC)}]))
    with pytest.raises(PersistenceError, match="replica"):
        asyncio.run(mongo.get_traffic_log(ObjectId(), device_id="device"))
    mongo.traffic_logs.find_one.assert_not_awaited()


def test_device_configuration_lookup_and_cas_are_device_scoped(mongo):
    mongo.device_configurations = AsyncMock()
    mongo.device_configurations.find_one.return_value = None
    document = asyncio.run(mongo.get_device_configuration("device"))
    assert document == {"version": 0, "encryption_mode": "tls_only"}
    mongo.device_configurations.find_one.assert_awaited_once_with({"_id": "device"})
    asyncio.run(mongo.update_device_configuration("device", 3, "required"))
    args, kwargs = mongo.device_configurations.find_one_and_update.call_args
    assert args[0] == {"_id": "device", "version": 3}
    assert args[1]["$inc"] == {"version": 1}
    assert kwargs["upsert"] is False
    mongo.device_configurations.find_one.side_effect = ServerSelectionTimeoutError("no primary")
    with pytest.raises(PersistenceError, match="configuration"):
        asyncio.run(mongo.get_device_configuration("device"))
