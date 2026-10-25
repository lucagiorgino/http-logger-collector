import asyncio
import hashlib
import json
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import unquote_plus
from uuid import UUID

from bson import ObjectId
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.config import settings
from app.core.encryption import EncryptionKeys, InvalidEnvelope
from app.core.logging import logger
from app.core.openbao import KMSUnavailable
from app.core.storage_encryption import StorageEncryption
from app.db.mongo import MongoRepository, PersistenceError
from app.schemas.traffic import TrafficLogRequest
from app.schemas.uploads import UploadDescriptor
from app.services.batch_codec import parse_compressed_batch
from app.services.queued_batch import InvalidQueuedBatch
from app.services.traffic_service import ClientIdentity, IngestionContext, normalize_batch
from app.services.upload_cloud import UploadCloud
from app.services.upload_queue import SilverPayloadTooLarge, UploadQueue
from app.services.upload_state import UploadStateStore


def metric(name: str, value: float = 1) -> None:
    logger.info("metric=%s value=%s", name, value)


def normalize_upload(
    value: bytes,
    descriptor: UploadDescriptor,
    keys: EncryptionKeys,
) -> tuple[list[dict[str, Any]], list[TrafficLogRequest]]:
    try:
        header, wire = value.split(b"\n", 1)
        stored = UploadDescriptor.model_validate_json(header)
        if (
            stored != descriptor
            or len(wire) != descriptor.wire_bytes
            or hashlib.sha256(wire).hexdigest() != descriptor.sha256
        ):
            raise ValueError("Invalid raw upload integrity")
    except (ValueError, ValidationError) as exc:
        raise InvalidEnvelope("Invalid raw upload framing") from exc
    compressed = keys.decrypt(wire) if descriptor.encrypted else wire
    batch = parse_compressed_batch(compressed)
    context = IngestionContext(
        ClientIdentity(descriptor.device_id, descriptor.client_id), descriptor.source_ip
    )
    documents = normalize_batch(batch.logs, descriptor.upload_id, context)
    for index, document in enumerate(documents):
        # Deterministic IDs survive raw reprocessing, unlike fresh collector ObjectIds.
        document["_id"] = ObjectId(
            hashlib.sha256(f"{descriptor.upload_id}:{index}".encode()).digest()[:12]
        )
        document["ingestion"]["received_at"] = descriptor.received_at
    return documents, batch.logs


class UploadParser:
    """CPU-bound bronze -> silver, isolated from I/O-bound Mongo loading."""

    def __init__(
        self,
        cloud: UploadCloud,
        store: UploadStateStore,
        queue: UploadQueue,
        keys: EncryptionKeys,
        storage: StorageEncryption,
    ) -> None:
        self.cloud, self.store, self.queue = cloud, store, queue
        self.keys, self.storage = keys, storage

    async def process(self, key: str) -> bool:
        upload_id = key.rsplit("/", 1)[-1].removesuffix(".upload")
        record = await self.store.get(upload_id)
        if record is None or record["descriptor"]["object_key"] != key:
            raise PersistenceError("Raw upload has no matching receipt")
        if record["status"] == "stored":
            # A collector retry may recreate an already-consumed key after an ambiguous PUT.
            await self.store.update({"_id": upload_id, "status": "stored"}, {"raw_deleted": False})
            await self.queue.publish_cleanup(upload_id)
            return True
        if record["status"] in {"queued", "loading", "parse_failed", "load_failed"}:
            return True
        record = await self.store.claim(upload_id, "parse")
        if record is None:
            return False
        descriptor = UploadDescriptor.model_validate(record["descriptor"])
        try:
            async with asyncio.timeout(settings.spool_processing_seconds):
                value = await self.cloud.get(key)
                documents, payloads = await asyncio.to_thread(
                    normalize_upload,
                    value,
                    descriptor,
                    self.keys,
                )
                await self.storage.protect(documents, payloads, descriptor.policy)
                await self.queue.publish(upload_id, documents)
            await self.store.finish(
                record,
                status="queued",
                accepted_count=len(documents),
                error_code=None,
            )
            metric("ingest.parse.success")
            return True
        except (InvalidEnvelope, HTTPException, RequestValidationError):
            await self.fail(record, "invalid_content", permanent=True)
            return True
        except SilverPayloadTooLarge:
            await self.fail(record, "silver_payload_too_large", permanent=True)
            return True
        except (PersistenceError, KMSUnavailable, TimeoutError):
            return await self.fail(record, "parse_dependency_unavailable", permanent=False)

    async def fail(self, record: dict[str, Any], code: str, *, permanent: bool) -> bool:
        if permanent or record["parse_attempts"] >= settings.spool_max_attempts:
            # Never finalize or acknowledge before the DLQ write succeeds.
            await self.cloud.dead_letter("parse", record["_id"], code)
            await self.store.finish(record, status="parse_failed", error_code=code)
            metric("ingest.parse.dlq")
            logger.error("Upload parse DLQ requires operator action")
            return True
        await self.store.finish(record, status="received", error_code=code)
        return False

    async def consume_once(self) -> bool:
        messages = await self.cloud.receive()
        if not messages:
            return False
        message = messages[0]
        try:
            event = json.loads(message["Body"])
            if "bucket" in event and "name" in event:
                # Native GCS OBJECT_FINALIZE JSON_API_V1 notification, without listing.
                event = {
                    "Records": [
                        {
                            "eventName": "ObjectCreated:Put",
                            "s3": {
                                "bucket": {"name": event["bucket"]},
                                "object": {
                                    "key": event["name"].replace("%", "%25").replace("+", "%2B")
                                },
                            },
                        }
                    ]
                }
            if event.get("Event") == "s3:TestEvent":
                await self.cloud.acknowledge(message["ReceiptHandle"])
                return True
            records = event["Records"]
            if not isinstance(records, list) or not records:
                raise ValueError()
            keys = []
            for item in records:
                if (
                    not item["eventName"].removeprefix("s3:").startswith("ObjectCreated:")
                    or item["s3"]["bucket"]["name"] != settings.spool_bucket
                ):
                    raise ValueError()
                key = unquote_plus(item["s3"]["object"]["key"])
                if not key.startswith("raw/") or not key.endswith(".upload"):
                    raise ValueError()
                keys.append(key)
        except (ValueError, KeyError, TypeError, AttributeError):
            await self.cloud.dead_letter("parse", None, "invalid_notification")
            await self.cloud.acknowledge(message["ReceiptHandle"])
            logger.error("Invalid object notification routed to parse DLQ")
            metric("ingest.parse.dlq")
            return True
        completed = True
        for key in keys:
            if not await self.process(key):
                completed = False
        if completed:
            await self.cloud.acknowledge(message["ReceiptHandle"])
        return completed


class MongoWriteLimiter:
    """One limiter per loader process; deployment replicas multiply the configured rate."""

    def __init__(self) -> None:
        self.next_write = 0.0

    async def wait(self, count: int) -> None:
        now = monotonic()
        await asyncio.sleep(max(0, self.next_write - now))
        self.next_write = monotonic() + count / settings.spool_mongo_documents_per_second


class UploadLoader:
    def __init__(
        self,
        cloud: UploadCloud,
        store: UploadStateStore,
        queue: UploadQueue,
        repository: MongoRepository,
    ) -> None:
        self.cloud, self.store, self.queue, self.repository = cloud, store, queue, repository
        self.limiter = MongoWriteLimiter()

    async def cleanup(self, record: dict[str, Any], *, force: bool = False) -> None:
        if force or not record.get("raw_deleted"):
            await self.cloud.delete(record["descriptor"]["object_key"])
            if not await self.store.update(
                {"_id": record["_id"], "status": "stored"}, {"raw_deleted": True}
            ):
                raise PersistenceError("Upload cleanup receipt is unavailable")

    async def consume_once(self) -> bool:
        # Do not hold a Pub/Sub delivery lease while waiting for Mongo rate capacity.
        await self.limiter.wait(0)
        message = await self.queue.receive()
        if message is None:
            return False
        completed = False
        try:
            completed = await self.process_message(message)
            return completed
        finally:
            if not completed:
                await self.queue.retry(message[0])

    async def process_message(self, message: tuple[bytes, dict[bytes, bytes]]) -> bool:
        if message[1].get(b"kind") == b"cleanup":
            try:
                upload_id = str(UUID(message[1][b"upload_id"].decode("ascii")))
            except (KeyError, ValueError, AttributeError):
                await self.cloud.dead_letter("load", None, "invalid_cleanup_entry")
                await self.queue.acknowledge(message[0])
                metric("ingest.load.dlq")
                return True
            record = await self.store.get(upload_id)
            if record is None or record["status"] != "stored":
                await self.cloud.dead_letter("load", upload_id, "unsafe_cleanup_entry")
                await self.queue.acknowledge(message[0])
                metric("ingest.load.dlq")
                return True
            await self.cleanup(record, force=True)
            await self.queue.acknowledge(message[0])
            return True
        try:
            batch = self.queue.decode(*message)
        except InvalidQueuedBatch:
            await self.cloud.dead_letter("load", None, "invalid_silver_batch")
            await self.queue.acknowledge(message[0])
            metric("ingest.load.dlq")
            logger.error("Malformed silver entry routed to load DLQ; replay raw uploads")
            return True
        record = await self.store.get(batch.batch_id)
        if record is None:
            await self.cloud.dead_letter("load", batch.batch_id, "missing_upload_receipt")
            await self.queue.acknowledge(batch.message_id)
            metric("ingest.load.dlq")
            return True
        if record["status"] == "stored":
            await self.cleanup(record)
            await self.queue.acknowledge(batch.message_id)
            return True
        if record["status"] in {"parse_failed", "load_failed"}:
            await self.queue.acknowledge(batch.message_id)
            return True
        # Wait for rate capacity before acquiring a bounded processing lease.
        await self.limiter.wait(len(batch.documents))
        record = await self.store.claim(batch.batch_id, "load")
        if record is None:
            return False
        descriptor = UploadDescriptor.model_validate(record["descriptor"])
        if record.get("accepted_count") != len(batch.documents) or any(
            not isinstance(document.get("metadata"), dict)
            or document["metadata"].get("device_id") != descriptor.device_id
            or document["_id"]
            != ObjectId(hashlib.sha256(f"{batch.batch_id}:{index}".encode()).digest()[:12])
            for index, document in enumerate(batch.documents)
        ):
            return await self.fail(record, batch.message_id, "invalid_silver_identity", True)
        try:
            started = monotonic()
            async with asyncio.timeout(settings.spool_processing_seconds):
                await self.repository.insert_queued_traffic_logs(batch.documents)
            metric("mongo.write.latency_ms", (monotonic() - started) * 1000)
            await self.store.finish(record, status="stored", error_code=None)
        except (PersistenceError, TimeoutError):
            return await self.fail(record, batch.message_id, "mongo_write_failed", False)
        metric("ingest.load.success")
        metric(
            "pipeline.e2e.latency_ms",
            (datetime.now(UTC) - descriptor.received_at).total_seconds() * 1000,
        )
        # Cleanup failure leaves the entry pending, with status=stored: retry only deletion.
        await self.cleanup(record)
        await self.queue.acknowledge(batch.message_id)
        return True

    async def fail(
        self,
        record: dict[str, Any],
        message_id: bytes,
        code: str,
        permanent: bool,
    ) -> bool:
        if permanent or record["load_attempts"] >= settings.spool_max_attempts:
            await self.cloud.dead_letter("load", record["_id"], code)
            await self.store.finish(record, status="load_failed", error_code=code)
            await self.queue.acknowledge(message_id)
            metric("ingest.load.dlq")
            logger.error("Upload load DLQ requires operator action")
            return True
        await self.store.finish(record, status="queued", error_code=code)
        return False
