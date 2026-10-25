import asyncio
from dataclasses import dataclass
from typing import Any

from bson import BSON, ObjectId
from bson.codec_options import CodecOptions
from bson.errors import InvalidBSON
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, ResponseError

from app.core.config import settings
from app.db.mongo import PersistenceError
from app.schemas.traffic import BatchReceipt

ENQUEUE = """
if redis.call('XLEN', KEYS[1]) >= tonumber(ARGV[1]) then
    return false
end
return redis.call('XADD', KEYS[1], '*', 'payload', ARGV[2])
"""


@dataclass(frozen=True)
class QueuedBatch:
    message_id: bytes
    batch_id: str
    documents: list[dict[str, Any]]


class InvalidQueuedBatch(ValueError):
    pass


class RedisStreamQueue:
    def __init__(self, client: Redis | None = None) -> None:
        settings.validate_ingestion()
        self.client = (
            client
            if client is not None
            else Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.redis_socket_timeout_seconds,
                socket_timeout=settings.redis_socket_timeout_seconds,
                decode_responses=False,
                retry=Retry(NoBackoff(), 0),
            )
        )
        self.stream = settings.redis_stream
        self.group = settings.redis_group
        self.claim_cursor: bytes | str = "0-0"

    async def initialize(self) -> None:
        try:
            persistence = await self.client.info("persistence")
            if not persistence.get("aof_enabled"):
                raise PersistenceError("Redis ingestion requires AOF persistence")
            memory = await self.client.info("memory")
            if memory.get("maxmemory_policy") != "noeviction":
                raise PersistenceError("Redis ingestion requires the noeviction memory policy")
            await self.client.execute_command("WAITAOF", 1, 0, settings.redis_aof_timeout_ms)
            try:
                await self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            except ResponseError as exc:
                if not str(exc).startswith("BUSYGROUP"):
                    raise
        except RedisError as exc:
            raise PersistenceError("Redis ingestion is unavailable") from exc

    async def close(self) -> None:
        await self.client.aclose()

    async def publish(self, batch_id: str, documents: list[dict[str, Any]]) -> BatchReceipt:
        payload = await asyncio.to_thread(
            BSON.encode, {"version": 1, "batch_id": batch_id, "documents": documents}
        )
        try:
            # Both commands must use the same connection: WAITAOF covers this XADD.
            async with self.client.pipeline(transaction=False) as pipeline:
                pipeline.eval(ENQUEUE, 1, self.stream, settings.redis_max_batches, payload)
                pipeline.execute_command("WAITAOF", 1, 0, settings.redis_aof_timeout_ms)
                message_id, persisted = await pipeline.execute()
            if message_id is None:
                raise PersistenceError("Redis ingestion queue is full")
            if persisted[0] < 1:
                raise PersistenceError("Redis did not confirm durable batch acceptance")
        except RedisError as exc:
            raise PersistenceError("Failed to enqueue traffic log batch") from exc
        return BatchReceipt(batch_id=batch_id, accepted_count=len(documents), status="queued")

    async def receive(self, consumer: str) -> tuple[bytes, dict[bytes, bytes]] | None:
        try:
            self.claim_cursor, messages, *_ = await self.client.xautoclaim(
                self.stream,
                self.group,
                consumer,
                settings.worker_reclaim_idle_ms,
                start_id=self.claim_cursor,
                count=1,
            )
            if messages:
                return messages[0]
            streams = await self.client.xreadgroup(
                self.group,
                consumer,
                {self.stream: ">"},
                count=1,
                block=min(1000, max(1, int(settings.redis_socket_timeout_seconds * 500))),
            )
            return streams[0][1][0] if streams else None
        except RedisError as exc:
            raise PersistenceError("Failed to consume traffic log batch") from exc

    @staticmethod
    def decode(message_id: bytes, fields: dict[bytes, bytes]) -> QueuedBatch:
        try:
            envelope = BSON(fields[b"payload"]).decode(codec_options=CodecOptions(tz_aware=True))
        except (KeyError, InvalidBSON) as exc:
            raise InvalidQueuedBatch("Invalid queued BSON payload") from exc
        documents = envelope.get("documents")
        batch_id = envelope.get("batch_id")
        version = envelope.get("version")
        if (
            version != 1
            or not isinstance(batch_id, str)
            or not batch_id
            or not isinstance(documents, list)
            or not 1 <= len(documents) <= 500
            or any(
                not isinstance(document, dict)
                or not isinstance(document.get("_id"), ObjectId)
                or document.get("batch_id") != batch_id
                for document in documents
            )
        ):
            raise InvalidQueuedBatch("Invalid queued batch envelope")
        return QueuedBatch(message_id, batch_id, documents)

    async def acknowledge(self, message_id: bytes) -> None:
        try:
            async with self.client.pipeline(transaction=True) as pipeline:
                pipeline.xack(self.stream, self.group, message_id)
                pipeline.xdel(self.stream, message_id)
                await pipeline.execute()
        except RedisError as exc:
            raise PersistenceError("Failed to acknowledge traffic log batch") from exc
