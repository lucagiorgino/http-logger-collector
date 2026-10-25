from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from app.core.config import settings
from app.db.mongo import PersistenceError
from app.schemas.uploads import UploadDescriptor


class UploadStateStore:
    """Operational receipts/leases, not a bucket-polling trigger or log read path."""

    def __init__(self, database: AsyncDatabase) -> None:
        self.collection = database.get_collection(
            "ingestion_uploads",
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern("majority"),
        )

    async def initialize(self) -> None:
        try:
            await self.collection.create_index("descriptor.object_key", unique=True)
            await self.collection.create_index([("status", 1), ("updated_at", 1)])
            await self.collection.create_index(
                [("status", 1), ("raw_deleted", 1), ("updated_at", 1)]
            )
        except PyMongoError as exc:
            raise PersistenceError("Failed to initialize upload receipts") from exc

    async def reserve(self, descriptor: UploadDescriptor) -> tuple[dict[str, Any], bool]:
        record = {
            "_id": descriptor.upload_id,
            "descriptor": descriptor.model_dump(mode="python"),
            "status": "uploading",
            "updated_at": datetime.now(UTC),
            "raw_deleted": False,
        }
        try:
            await self.collection.insert_one(record)
            return record, True
        except DuplicateKeyError:
            existing = await self.get(descriptor.upload_id)
            if existing is None:
                raise PersistenceError("Upload reservation is unavailable") from None
            return existing, False
        except PyMongoError as exc:
            raise PersistenceError("Failed to reserve upload") from exc

    async def get(self, upload_id: str) -> dict[str, Any] | None:
        try:
            return await self.collection.find_one({"_id": upload_id})
        except PyMongoError as exc:
            raise PersistenceError("Failed to read upload receipt") from exc

    async def received(self, upload_id: str) -> None:
        await self.update({"_id": upload_id, "status": "uploading"}, {"status": "received"})

    async def update(self, query: dict[str, Any], values: dict[str, Any]) -> bool:
        try:
            result = await self.collection.update_one(
                query, {"$set": {**values, "updated_at": datetime.now(UTC)}}
            )
            return result.matched_count == 1
        except PyMongoError as exc:
            raise PersistenceError("Failed to update upload receipt") from exc

    async def claim(self, upload_id: str, stage: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        eligible = ["uploading", "received"] if stage == "parse" else ["queued"]
        working = "parsing" if stage == "parse" else "loading"
        try:
            return await self.collection.find_one_and_update(
                {
                    "_id": upload_id,
                    "$or": [
                        {"status": {"$in": eligible}},
                        {"status": working, "lease_until": {"$lt": now}},
                    ],
                },
                {
                    "$set": {
                        "status": working,
                        "lease_token": uuid4().hex,
                        "lease_until": now + timedelta(seconds=settings.spool_lease_seconds),
                        "updated_at": now,
                    },
                    "$inc": {f"{stage}_attempts": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as exc:
            raise PersistenceError("Failed to claim upload") from exc

    async def finish(self, record: dict[str, Any], **values: Any) -> None:
        if not await self.update(
            {
                "_id": record["_id"],
                "lease_token": record["lease_token"],
                "lease_until": {"$gt": datetime.now(UTC)},
            },
            values,
        ):
            raise PersistenceError("Upload processing lease expired")
