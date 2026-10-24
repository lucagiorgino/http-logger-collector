from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import PyMongoError

from app.core.config import settings


class PersistenceError(RuntimeError):
    pass


class MongoRepository:
    def __init__(self, uri: str | None = None, db_name: str | None = None) -> None:
        self.client = AsyncIOMotorClient(
            uri or settings.mongo_uri,
            serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
        )
        self.database = self.client[db_name or settings.mongo_db_name]
        self.traffic_logs: AsyncIOMotorCollection = self.database["traffic_logs"]

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except PyMongoError:
            return False

    async def ensure_indexes(self) -> None:
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("timestamp", DESCENDING)],
                name="user_timestamp",
            ),
            IndexModel(
                [("device_id", ASCENDING), ("timestamp", DESCENDING)],
                name="device_timestamp",
            ),
            IndexModel([("received_at", DESCENDING)], name="received_at"),
        ]
        try:
            await self.traffic_logs.create_indexes(indexes)
        except PyMongoError as exc:
            raise PersistenceError("Failed to initialize MongoDB indexes") from exc

    async def insert_traffic_log(self, document: dict[str, Any]) -> str:
        try:
            result = await self.traffic_logs.insert_one(document)
        except PyMongoError as exc:
            raise PersistenceError("Failed to persist traffic log") from exc
        return str(result.inserted_id)

    def close(self) -> None:
        self.client.close()
