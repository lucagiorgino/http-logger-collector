"""In-memory object notifications and silver deliveries driving the real workers."""

import copy
import gzip
import hashlib
import io
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from botocore.exceptions import ClientError

from app.core.encryption import EncryptionKeys
from app.schemas.uploads import UploadDescriptor
from app.services.upload_cloud import UploadCloud
from app.services.upload_pipeline import UploadLoader, UploadParser
from app.services.upload_queue import UploadQueue, encode_silver


class MemoryUploads:
    def __init__(self):
        self.records = {}

    async def reserve(self, descriptor):
        created = descriptor.upload_id not in self.records
        if created:
            self.records[descriptor.upload_id] = {
                "_id": descriptor.upload_id,
                "descriptor": descriptor.model_dump(),
                "status": "uploading",
                "raw_deleted": False,
            }
        return copy.deepcopy(self.records[descriptor.upload_id]), created

    async def received(self, upload_id):
        if self.records[upload_id]["status"] == "uploading":
            self.records[upload_id]["status"] = "received"

    async def get(self, upload_id):
        return copy.deepcopy(self.records.get(upload_id))

    async def claim(self, upload_id, stage):
        record = self.records[upload_id]
        eligible = ["uploading", "received"] if stage == "parse" else ["queued"]
        if record["status"] not in eligible:
            return None
        record.update(status="parsing" if stage == "parse" else "loading", lease_token="token")
        record[f"{stage}_attempts"] = record.get(f"{stage}_attempts", 0) + 1
        return copy.deepcopy(record)

    async def finish(self, record, **values):
        self.records[record["_id"]].update(values)

    async def update(self, query, values):
        self.records[query["_id"]].update(values)
        return True


def silver_queue():
    queue = AsyncMock()
    queue.decode = UploadQueue.decode
    queue.messages = []

    async def publish(batch_id, documents):
        queue.messages.append(
            (
                b"1-0",
                {
                    b"payload": encode_silver(batch_id, documents),
                    b"kind": b"batch",
                    b"encoding": b"gzip-bson-v2",
                    b"upload_id": batch_id.encode(),
                },
            )
        )

    async def publish_cleanup(upload_id):
        queue.messages.append(
            (b"2-0", {b"kind": b"cleanup", b"upload_id": upload_id.encode(), b"payload": b""})
        )

    queue.publish.side_effect = publish
    queue.publish_cleanup.side_effect = publish_cleanup
    return queue


class UploadHarness:
    def __init__(self, repository, storage, keys=None):
        self.store = MemoryUploads()
        self.objects = {}
        self.notifications = []
        objects = Mock()

        def put(**kwargs):
            key = kwargs["Key"]
            if key in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.objects[key] = kwargs["Body"]
            self.notifications.append(
                {
                    "Body": json.dumps({"bucket": kwargs["Bucket"], "name": key}),
                    "ReceiptHandle": key,
                }
            )

        def get(**kwargs):
            body = self.objects[kwargs["Key"]]
            return {"Body": io.BytesIO(body), "ContentLength": len(body)}

        objects.put_object.side_effect = put
        objects.get_object.side_effect = get
        objects.delete_object.side_effect = lambda **kw: self.objects.pop(kw["Key"], None)
        self.cloud = UploadCloud(objects, Mock(), Mock())
        self.cloud.check_storage = AsyncMock()
        self.cloud.check_subscription = AsyncMock()
        self.cloud.dead_letter = AsyncMock()
        self.cloud.acknowledge = AsyncMock()
        self.cloud.receive = AsyncMock(side_effect=self.receive)
        self.queue = silver_queue()
        self.queue.receive.side_effect = self.receive_silver
        self.parser = UploadParser(
            self.cloud, self.store, self.queue, keys or EncryptionKeys({}), storage
        )
        self.loader = UploadLoader(self.cloud, self.store, self.queue, repository)

    async def receive(self):
        return [self.notifications.pop(0)] if self.notifications else []

    async def receive_silver(self):
        return self.queue.messages.pop(0) if self.queue.messages else None

    async def drain(self):
        while self.notifications:
            if not await self.parser.consume_once():
                break
        while self.queue.messages:
            if not await self.loader.consume_once():
                break

    async def enqueue(self, payloads, context, policy):
        """Publish a raw object and notification without involving API authentication."""
        upload_id = str(uuid4())
        wire = gzip.compress(
            json.dumps({"logs": [p.model_dump(mode="json") for p in payloads]}).encode()
        )
        descriptor = UploadDescriptor(
            upload_id=upload_id,
            object_key=f"raw/{upload_id}.upload",
            device_id=context.identity.device_id,
            client_id=context.identity.client_id,
            source_ip=context.source_ip,
            received_at=datetime.now(UTC),
            encrypted=False,
            sha256=hashlib.sha256(wire).hexdigest(),
            wire_bytes=len(wire),
            policy=policy,
        )
        await self.store.reserve(descriptor)
        await self.cloud.put(descriptor, wire)
        await self.store.received(upload_id)
        return upload_id
