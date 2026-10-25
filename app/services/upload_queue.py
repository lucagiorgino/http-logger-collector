import asyncio
import gzip
import zlib
from typing import Any

from bson import BSON

from app.core.config import settings
from app.services.queued_batch import InvalidQueuedBatch, QueuedBatch, decode_batch
from app.services.upload_cloud import UploadCloud

# Leave headroom beneath Pub/Sub's 10 MB request limit, including message attributes.
MAX_PUBSUB_BYTES = 9_500_000
MAX_SILVER_BYTES = 32 * 1024 * 1024


class SilverPayloadTooLarge(ValueError):
    pass


def encode_silver(batch_id: str, documents: list[dict[str, Any]]) -> bytes:
    raw = BSON.encode({"version": 2, "batch_id": batch_id, "documents": documents})
    payload = gzip.compress(raw, mtime=0)
    if len(raw) > MAX_SILVER_BYTES or len(payload) > MAX_PUBSUB_BYTES:
        raise SilverPayloadTooLarge("Normalized batch exceeds silver transport limits")
    return payload


class UploadQueue:
    """Pub/Sub silver transport; raw objects remain available until Mongo succeeds."""

    def __init__(self, cloud: UploadCloud) -> None:
        self.cloud = cloud
        self.topic = settings.spool_silver_topic
        self.subscription = settings.spool_silver_subscription

    async def publish(self, batch_id: str, documents: list[dict[str, Any]]) -> None:
        payload = await asyncio.to_thread(encode_silver, batch_id, documents)
        await self.cloud.publish_bytes(
            self.topic, payload, kind="batch", encoding="gzip-bson-v2", upload_id=batch_id
        )

    async def publish_cleanup(self, upload_id: str) -> None:
        await self.cloud.publish_bytes(self.topic, b"", kind="cleanup", upload_id=upload_id)

    async def receive(self) -> tuple[bytes, dict[bytes, bytes]] | None:
        messages = await self.cloud.receive(self.subscription)
        if not messages:
            return None
        message = messages[0]
        fields = {key.encode(): value.encode() for key, value in message["Attributes"].items()}
        fields[b"payload"] = message["Body"]
        return message["ReceiptHandle"].encode(), fields

    @staticmethod
    def decode(message_id: bytes, fields: dict[bytes, bytes]) -> QueuedBatch:
        try:
            if fields.get(b"kind") != b"batch" or fields.get(b"encoding") != b"gzip-bson-v2":
                raise InvalidQueuedBatch("Invalid silver message attributes")
            compressed = fields[b"payload"]
            if len(compressed) > MAX_PUBSUB_BYTES:
                raise InvalidQueuedBatch("Silver message exceeds wire size limit")
            decoder = zlib.decompressobj(wbits=31)
            payload = decoder.decompress(compressed, MAX_SILVER_BYTES + 1)
            if len(payload) > MAX_SILVER_BYTES or not decoder.eof or decoder.unused_data:
                raise InvalidQueuedBatch("Invalid silver compression envelope")
            batch = decode_batch(message_id, {b"payload": payload})
            if fields.get(b"upload_id") != batch.batch_id.encode():
                raise InvalidQueuedBatch("Silver upload identity mismatch")
            return batch
        except (KeyError, zlib.error) as exc:
            raise InvalidQueuedBatch("Invalid silver payload") from exc

    async def acknowledge(self, message_id: bytes) -> None:
        await self.cloud.acknowledge(message_id.decode(), self.subscription)

    async def retry(self, message_id: bytes) -> None:
        await self.cloud.retry(message_id.decode(), self.subscription)
