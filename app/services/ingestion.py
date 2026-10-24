from typing import Any, Protocol

from app.schemas.traffic import BatchReceipt


class BatchWriter(Protocol):
    async def insert_traffic_logs(self, documents: list[dict[str, Any]]) -> None: ...


class IngestionPublisher(Protocol):
    async def publish(self, batch_id: str, documents: list[dict[str, Any]]) -> BatchReceipt:
        """Acknowledge only after durable acceptance; raise PersistenceError on failure."""
        ...


class MongoIngestionPublisher:
    def __init__(self, repository: BatchWriter) -> None:
        self.repository = repository

    async def publish(self, batch_id: str, documents: list[dict[str, Any]]) -> BatchReceipt:
        await self.repository.insert_traffic_logs(documents)
        return BatchReceipt(batch_id=batch_id, accepted_count=len(documents), status="stored")
