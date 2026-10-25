from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import CollectionInvalid, DuplicateKeyError, OperationFailure, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.uri_parser import parse_uri
from pymongo.write_concern import WriteConcern

from app.core.config import settings
from app.schemas.workspace import WorkspaceConfiguration


class PersistenceError(RuntimeError):
    pass


class DuplicateDeviceError(PersistenceError):
    pass


class MongoRepository:
    collection_name = "traffic_logs"

    def __init__(self, uri: str | None = None, db_name: str | None = None) -> None:
        if settings.mongo_allow_primary_reads and settings.app_env != "development":
            raise PersistenceError("Primary analysis reads are restricted to development")
        uri = uri or settings.mongo_uri
        try:
            options = parse_uri(uri)["options"]
        except PyMongoError:
            raise PersistenceError("Invalid MongoDB connection configuration") from None
        if not options.get("replicaSet") or options.get("directConnection"):
            raise PersistenceError(
                "MONGO_URI must specify replicaSet and must not enable directConnection"
            )
        self.client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            uri,
            serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
            tz_aware=True,
        )
        self.database = self.client[db_name or settings.mongo_db_name]
        self.traffic_logs: AsyncCollection[dict[str, Any]] = self.database.get_collection(
            self.collection_name,
            read_preference=ReadPreference.PRIMARY,
            write_concern=WriteConcern("majority"),
        )
        self.analysis_logs: AsyncCollection[dict[str, Any]] = self.database.get_collection(
            self.collection_name,
            read_preference=(
                ReadPreference.PRIMARY
                if settings.mongo_allow_primary_reads
                else ReadPreference.SECONDARY
            ),
            read_concern=ReadConcern("majority"),
        )
        self.device_configurations: AsyncCollection[dict[str, Any]] = self.database.get_collection(
            "device_configurations",
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern("majority"),
        )
        self.workspace_configurations: AsyncCollection[dict[str, Any]] = (
            self.database.get_collection(
                "workspace_configurations",
                read_preference=ReadPreference.PRIMARY,
                read_concern=ReadConcern("majority"),
                write_concern=WriteConcern("majority"),
            )
        )

    async def get_workspace_configuration(self) -> dict[str, Any]:
        try:
            document = await self.workspace_configurations.find_one(
                {"_id": "workspace"}, {"_id": 0}
            )
            return document if document is not None else WorkspaceConfiguration().model_dump()
        except PyMongoError as exc:
            raise PersistenceError("Failed to read workspace configuration") from exc

    async def update_workspace_configuration(
        self, expected_version: int, policy: dict[str, bool | int]
    ) -> dict[str, Any] | None:
        try:
            document = await self.workspace_configurations.find_one_and_update(
                {"_id": "workspace", "version": expected_version},
                {"$set": {**policy, "updated_at": datetime.now(UTC)}, "$inc": {"version": 1}},
                upsert=expected_version == 0,
                return_document=ReturnDocument.AFTER,
            )
            if document is not None:
                await self.ensure_retention_indexes()
            return (
                {key: value for key, value in document.items() if key != "_id"}
                if document is not None
                else None
            )
        except DuplicateKeyError:
            return None
        except PyMongoError as exc:
            raise PersistenceError("Failed to update workspace configuration") from exc

    async def ensure_retention_indexes(self) -> None:
        # DDL cannot join the settings CAS transaction. Re-read after applying so
        # concurrent updates converge on the latest policy, including across pods.
        for _ in range(5):
            configuration = WorkspaceConfiguration.model_validate(
                await self.get_workspace_configuration()
            )
            try:
                cursor = await self.traffic_logs.list_indexes()
                indexes = {index["name"]: index for index in await cursor.to_list(length=None)}
                for name, enabled, days, predicate in (
                    (
                        "retention_ordinary",
                        configuration.retention_enabled,
                        configuration.retention_days,
                        {"metadata.suspicious": {"$in": [False, None]}},
                    ),
                    (
                        "retention_suspicious",
                        configuration.suspicious_retention_enabled,
                        configuration.suspicious_retention_days,
                        {"metadata.suspicious": True},
                    ),
                ):
                    existing = indexes.get(name)
                    if not enabled:
                        if existing is not None:
                            await self.traffic_logs.drop_index(name)
                    elif (
                        existing is not None
                        and existing.get("partialFilterExpression") != predicate
                    ):
                        raise PersistenceError("Incompatible traffic log retention index")
                    elif existing is None or existing.get("expireAfterSeconds") != days * 86400:
                        # MongoDB 7 rejects collMod TTL changes on time-series indexes.
                        if existing is not None:
                            await self.traffic_logs.drop_index(name)
                        await self.traffic_logs.create_index(
                            [("timestamp", ASCENDING)],
                            name=name,
                            expireAfterSeconds=days * 86400,
                            partialFilterExpression=predicate,
                        )
                latest = await self.get_workspace_configuration()
                if latest["version"] == configuration.version:
                    return
            except OperationFailure as exc:
                if exc.code not in {27, 85, 86}:
                    raise PersistenceError("Failed to apply log retention indexes") from exc
                # Another process created, modified or dropped the same index.
            except PyMongoError as exc:
                raise PersistenceError("Failed to apply log retention indexes") from exc
        raise PersistenceError("Log retention configuration changed concurrently; retry")

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except PyMongoError:
            return False

    async def ensure_indexes(self) -> None:
        indexes = [
            IndexModel(
                [
                    ("metadata.device_id", ASCENDING),
                    ("timestamp", DESCENDING),
                    ("_id", DESCENDING),
                ],
                name="device_timestamp",
            ),
            IndexModel([("timestamp", DESCENDING), ("_id", DESCENDING)], name="timestamp_id"),
            IndexModel([("received_at", DESCENDING)], name="received_at"),
        ]
        try:
            try:
                await self.database.create_collection(
                    self.collection_name,
                    timeseries={
                        "timeField": "timestamp",
                        "metaField": "metadata",
                        "granularity": "seconds",
                    },
                )
            except (CollectionInvalid, OperationFailure) as exc:
                if isinstance(exc, OperationFailure) and exc.code != 48:
                    raise
                # Another instance may have created the collection during startup.
            cursor = await self.database.list_collections(filter={"name": self.collection_name})
            collections = await cursor.to_list(length=1)
            collection_options = collections[0].get("options", {}) if collections else {}
            options = collection_options.get("timeseries", {})
            if options.get("timeField") != "timestamp" or options.get("metaField") != "metadata":
                raise PersistenceError(
                    "traffic_logs must use timestamp and metadata as its time-series fields"
                )
            if collection_options.get("expireAfterSeconds", "off") != "off":
                raise PersistenceError(
                    "Collection-wide expiration must be disabled before using retention policies"
                )
            await self.traffic_logs.create_indexes(indexes)
            await self.device_configurations.create_index(
                "name",
                name="unique_device_name",
                unique=True,
                partialFilterExpression={"name": {"$type": "string"}},
            )
            await self.ensure_retention_indexes()
        except PyMongoError as exc:
            raise PersistenceError("Failed to initialize MongoDB storage") from exc

    async def insert_traffic_logs(self, documents: list[dict[str, Any]]) -> None:
        try:
            await self.traffic_logs.insert_many(documents, ordered=False)
        except PyMongoError as exc:
            raise PersistenceError("Failed to persist traffic log batch") from exc

    async def list_traffic_logs(
        self,
        *,
        device_id: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        before: tuple[datetime, ObjectId] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if device_id is not None:
            query["metadata.device_id"] = device_id
        if start_time is not None or end_time is not None:
            query["timestamp"] = {}
            if start_time is not None:
                query["timestamp"]["$gte"] = start_time
            if end_time is not None:
                query["timestamp"]["$lt"] = end_time
        if before is not None:
            timestamp, record_id = before
            query["$or"] = [
                {"timestamp": {"$lt": timestamp}},
                {"timestamp": timestamp, "_id": {"$lt": record_id}},
            ]
        try:
            return await (
                self.analysis_logs.find(query, {"body": 0, "storage_encryption.ciphertext": 0})
                .sort([("timestamp", DESCENDING), ("_id", DESCENDING)])
                .limit(limit)
                .to_list(length=limit)
            )
        except PyMongoError as exc:
            raise PersistenceError("Failed to read traffic logs from a replica") from exc

    async def get_traffic_log(
        self, record_id: ObjectId, *, device_id: str | None
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {"_id": record_id}
        if device_id is not None:
            query["metadata.device_id"] = device_id
        try:
            return await self.analysis_logs.find_one(query)
        except PyMongoError as exc:
            raise PersistenceError("Failed to read traffic log from a replica") from exc

    async def register_device(self, device_id: str, name: str, mode: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        document = {
            "_id": device_id,
            "name": name,
            "registered_at": now,
            "updated_at": now,
            "encryption_mode": mode,
            "version": 1,
        }
        try:
            await self.device_configurations.insert_one(document)
            return document
        except DuplicateKeyError as exc:
            raise DuplicateDeviceError("Device ID or name already exists") from exc
        except PyMongoError as exc:
            raise PersistenceError("Failed to register device") from exc

    async def list_devices(
        self, *, device_id: str | None, after: str | None, limit: int
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if after is not None:
            query["_id"] = {"$gt": after}
        if device_id is not None:
            query.setdefault("_id", {})["$eq"] = device_id
        try:
            return await (
                self.device_configurations.find(query)
                .sort("_id", ASCENDING)
                .limit(limit)
                .to_list(length=limit)
            )
        except PyMongoError as exc:
            raise PersistenceError("Failed to list devices") from exc

    async def get_device_configuration(self, device_id: str) -> dict[str, Any]:
        try:
            document = await self.device_configurations.find_one({"_id": device_id})
            return document or {"version": 0, "encryption_mode": "tls_only"}
        except PyMongoError as exc:
            raise PersistenceError("Failed to read device configuration") from exc

    async def update_device_configuration(
        self, device_id: str, expected_version: int, mode: str
    ) -> dict[str, Any] | None:
        try:
            return await self.device_configurations.find_one_and_update(
                {
                    "_id": device_id,
                    "version": expected_version,
                },
                {
                    "$set": {"encryption_mode": mode, "updated_at": datetime.now(UTC)},
                    "$inc": {"version": 1},
                },
                upsert=expected_version == 0,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            return None
        except PyMongoError as exc:
            raise PersistenceError("Failed to update device configuration") from exc

    async def report_device_configuration(self, device_id: str, report: dict[str, Any]) -> bool:
        try:
            result = await self.device_configurations.update_one(
                {
                    "_id": device_id,
                    "version": report["version"],
                },
                {
                    "$set": {"configuration_status": report},
                    "$setOnInsert": {"encryption_mode": "tls_only"},
                },
                upsert=report["version"] == 0,
            )
            return result.matched_count == 1 or result.upserted_id is not None
        except DuplicateKeyError:
            return False
        except PyMongoError as exc:
            raise PersistenceError("Failed to report device configuration") from exc

    async def close(self) -> None:
        await self.client.close()
