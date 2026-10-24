from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.errors import CollectionInvalid, DuplicateKeyError, OperationFailure, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from app.core.config import settings


class PersistenceError(RuntimeError):
    pass


class MongoRepository:
    collection_name = "traffic_logs"

    def __init__(self, uri: str | None = None, db_name: str | None = None) -> None:
        if settings.mongo_allow_primary_reads and settings.app_env != "development":
            raise PersistenceError("Primary analysis reads are restricted to development")
        self.client = AsyncIOMotorClient(
            uri or settings.mongo_uri,
            serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
            tz_aware=True,
        )
        if not self.client.options.replica_set_name or self.client.options.direct_connection:
            self.client.close()
            raise PersistenceError(
                "MONGO_URI must specify replicaSet and must not enable directConnection"
            )
        self.database = self.client[db_name or settings.mongo_db_name]
        self.traffic_logs: AsyncIOMotorCollection = self.database.get_collection(
            self.collection_name,
            read_preference=ReadPreference.PRIMARY,
            write_concern=WriteConcern("majority"),
        )
        self.analysis_logs: AsyncIOMotorCollection = self.database.get_collection(
            self.collection_name,
            read_preference=(
                ReadPreference.PRIMARY
                if settings.mongo_allow_primary_reads
                else ReadPreference.SECONDARY
            ),
            read_concern=ReadConcern("majority"),
        )
        self.device_configurations: AsyncIOMotorCollection = self.database.get_collection(
            "device_configurations",
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern("majority"),
        )

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except PyMongoError:
            return False

    async def ensure_indexes(self) -> None:
        indexes = [
            IndexModel(
                [("metadata.user_id", ASCENDING), ("timestamp", DESCENDING), ("_id", DESCENDING)],
                name="user_timestamp",
            ),
            IndexModel(
                [
                    ("metadata.user_id", ASCENDING),
                    ("metadata.device_id", ASCENDING),
                    ("timestamp", DESCENDING),
                    ("_id", DESCENDING),
                ],
                name="user_device_timestamp",
            ),
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
            options = collections[0].get("options", {}).get("timeseries", {}) if collections else {}
            if options.get("timeField") != "timestamp" or options.get("metaField") != "metadata":
                raise PersistenceError(
                    "traffic_logs must use timestamp and metadata as its time-series fields"
                )
            await self.traffic_logs.create_indexes(indexes)
        except PyMongoError as exc:
            raise PersistenceError("Failed to initialize MongoDB time-series storage") from exc

    async def insert_traffic_logs(self, documents: list[dict[str, Any]]) -> None:
        try:
            await self.traffic_logs.insert_many(documents, ordered=False)
        except PyMongoError as exc:
            raise PersistenceError("Failed to persist traffic log batch") from exc

    async def list_traffic_logs(
        self,
        subject: str,
        *,
        device_id: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        before: tuple[datetime, ObjectId] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"metadata.user_id": subject}
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
                self.analysis_logs.find(query)
                .sort([("timestamp", DESCENDING), ("_id", DESCENDING)])
                .limit(limit)
                .to_list(length=limit)
            )
        except PyMongoError as exc:
            raise PersistenceError("Failed to read traffic logs from a replica") from exc

    async def get_traffic_log(self, subject: str, record_id: ObjectId) -> dict[str, Any] | None:
        try:
            return await self.analysis_logs.find_one(
                {"metadata.user_id": subject, "_id": record_id}
            )
        except PyMongoError as exc:
            raise PersistenceError("Failed to read traffic log from a replica") from exc

    async def get_device_configuration(self, subject: str, device_id: str) -> dict[str, Any]:
        try:
            document = await self.device_configurations.find_one(
                {"_id": {"user_id": subject, "device_id": device_id}}
            )
            return document or {"version": 0, "encryption_mode": "tls_only"}
        except PyMongoError as exc:
            raise PersistenceError("Failed to read device configuration") from exc

    async def update_device_configuration(
        self, subject: str, device_id: str, expected_version: int, mode: str
    ) -> dict[str, Any] | None:
        try:
            return await self.device_configurations.find_one_and_update(
                {
                    "_id": {"user_id": subject, "device_id": device_id},
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

    async def report_device_configuration(
        self, subject: str, device_id: str, report: dict[str, Any]
    ) -> bool:
        try:
            result = await self.device_configurations.update_one(
                {
                    "_id": {"user_id": subject, "device_id": device_id},
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

    def close(self) -> None:
        self.client.close()
