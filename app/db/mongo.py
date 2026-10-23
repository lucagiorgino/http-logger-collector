from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings


class MongoRepository:
    def __init__(self, uri: str | None = None, db_name: str | None = None) -> None:
        self.client = AsyncIOMotorClient(uri or settings.mongo_uri)
        self.database = self.client[db_name or settings.mongo_db_name]

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:
            return False
