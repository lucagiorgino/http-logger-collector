import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from google.api_core.exceptions import DeadlineExceeded, GoogleAPICallError, PreconditionFailed
from google.auth.exceptions import GoogleAuthError
from google.cloud import pubsub_v1, storage

from app.core.config import settings
from app.db.mongo import PersistenceError
from app.schemas.uploads import UploadDescriptor

MAX_OBJECT_BYTES = 1_500_000 + 32_768
CLOUD_ERRORS = (GoogleAPICallError, GoogleAuthError, BotoCoreError, ClientError)


def minio_client(*, probe: bool = False) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.spool_endpoint_url,
        region_name="us-east-1",
        aws_access_key_id=Path(settings.spool_minio_access_key_file).read_text().strip(),
        aws_secret_access_key=Path(settings.spool_minio_secret_key_file).read_text().strip(),
        config=Config(
            connect_timeout=2 if probe else 3,
            read_timeout=2 if probe else 25,
            retries={"mode": "standard", "total_max_attempts": 1 if probe else 2},
        ),
    )


class UploadCloud:
    """GCS/PubSub in production, MinIO/PubSub emulator in local development."""

    def __init__(
        self, object_client: Any = None, publisher: Any = None, subscriber: Any = None
    ) -> None:
        self.backend = settings.spool_storage_backend
        if object_client is not None:
            self.objects = object_client
        elif self.backend == "gcs":
            self.objects = storage.Client()
        else:
            self.objects = minio_client()
        self.publisher = publisher if publisher is not None else pubsub_v1.PublisherClient()
        self.subscriber = subscriber if subscriber is not None else pubsub_v1.SubscriberClient()

    async def check_storage(self) -> None:
        def probe() -> None:
            if self.backend == "gcs":
                self.objects.bucket(settings.spool_bucket).reload(timeout=2, retry=None)
            else:
                client = minio_client(probe=True)
                try:
                    client.head_bucket(Bucket=settings.spool_bucket)
                finally:
                    client.close()

        try:
            await asyncio.to_thread(probe)
        except (*CLOUD_ERRORS, OSError) as exc:
            raise PersistenceError("Raw storage metadata probe failed") from exc

    async def check_subscription(self, subscription: str) -> None:
        try:
            await asyncio.to_thread(
                self.subscriber.get_subscription,
                request={"subscription": subscription},
                timeout=2,
                retry=None,
            )
        except (GoogleAPICallError, GoogleAuthError) as exc:
            raise PersistenceError("Pub/Sub metadata probe failed") from exc

    async def put(self, descriptor: UploadDescriptor, wire: bytes) -> None:
        # One immutable framed object atomically contains provenance and exact wire bytes.
        body = descriptor.model_dump_json().encode() + b"\n" + wire

        def write() -> None:
            if self.backend == "gcs":
                self.objects.bucket(settings.spool_bucket).blob(
                    descriptor.object_key
                ).upload_from_string(
                    body,
                    content_type="application/octet-stream",
                    if_generation_match=0,
                    timeout=25,
                    retry=None,
                )
            else:
                # Local MinIO volume is loopback-only, not a production encryption service.
                self.objects.put_object(
                    Bucket=settings.spool_bucket,
                    Key=descriptor.object_key,
                    Body=body,
                    ContentType="application/octet-stream",
                    IfNoneMatch="*",
                )

        try:
            await asyncio.to_thread(write)
        except (PreconditionFailed, ClientError) as exc:
            if isinstance(exc, ClientError) and exc.response["Error"]["Code"] not in {
                "PreconditionFailed",
                "412",
            }:
                raise PersistenceError("Failed to persist raw upload") from exc
            if await self.get(descriptor.object_key) != body:
                raise PersistenceError("Immutable upload object conflicts with retry") from exc
        except CLOUD_ERRORS as exc:
            raise PersistenceError("Failed to persist raw upload") from exc

    async def get(self, key: str) -> bytes:
        def read() -> bytes:
            if self.backend == "gcs":
                blob = self.objects.bucket(settings.spool_bucket).blob(key)
                blob.reload(timeout=25, retry=None)
                if blob.size > MAX_OBJECT_BYTES:
                    raise PersistenceError("Raw upload exceeds object size limit")
                return blob.download_as_bytes(
                    if_generation_match=blob.generation,
                    timeout=25,
                    retry=None,
                )
            response = self.objects.get_object(Bucket=settings.spool_bucket, Key=key)
            body = response["Body"]
            try:
                if response["ContentLength"] > MAX_OBJECT_BYTES:
                    raise PersistenceError("Raw upload exceeds object size limit")
                value = body.read(MAX_OBJECT_BYTES + 1)
                if len(value) > MAX_OBJECT_BYTES:
                    raise PersistenceError("Raw upload exceeds object size limit")
                return value
            finally:
                body.close()

        try:
            return await asyncio.to_thread(read)
        except CLOUD_ERRORS as exc:
            raise PersistenceError("Failed to fetch raw upload") from exc

    async def delete(self, key: str) -> None:
        def remove() -> None:
            if self.backend == "gcs":
                # Receipts and IAM prohibit replacement of a live object key.
                self.objects.bucket(settings.spool_bucket).blob(key).delete(timeout=25, retry=None)
            else:
                self.objects.delete_object(Bucket=settings.spool_bucket, Key=key)

        try:
            await asyncio.to_thread(remove)
        except GoogleAPICallError as exc:
            if exc.code != 404:
                raise PersistenceError("Failed to delete consumed upload") from exc
        except (GoogleAuthError, BotoCoreError, ClientError) as exc:
            raise PersistenceError("Failed to delete consumed upload") from exc

    async def receive(self, subscription: str | None = None) -> list[dict[str, Any]]:
        subscription = subscription or settings.spool_subscription
        try:
            response = await asyncio.to_thread(
                self.subscriber.pull,
                request={"subscription": subscription, "max_messages": 1},
                timeout=15,
                retry=None,
            )
        except DeadlineExceeded:
            return []  # An empty long poll is normal; no work was acknowledged.
        except (GoogleAPICallError, GoogleAuthError) as exc:
            raise PersistenceError("Failed to receive upload message") from exc
        messages = response.received_messages
        if not messages:
            return []
        try:
            await asyncio.to_thread(
                self.subscriber.modify_ack_deadline,
                request={
                    "subscription": subscription,
                    "ack_ids": [message.ack_id for message in messages],
                    "ack_deadline_seconds": settings.spool_lease_seconds + 30,
                },
                timeout=15,
                retry=None,
            )
            return [
                {
                    "Body": message.message.data,
                    "ReceiptHandle": message.ack_id,
                    "Attributes": dict(message.message.attributes),
                }
                for message in messages
            ]
        except (GoogleAPICallError, GoogleAuthError) as exc:
            raise PersistenceError("Failed to extend upload message lease") from exc

    async def acknowledge(self, receipt: str, subscription: str | None = None) -> None:
        try:
            await asyncio.to_thread(
                self.subscriber.acknowledge,
                request={
                    "subscription": subscription or settings.spool_subscription,
                    "ack_ids": [receipt],
                },
                timeout=15,
                retry=None,
            )
        except (GoogleAPICallError, GoogleAuthError) as exc:
            raise PersistenceError("Failed to acknowledge upload message") from exc

    async def retry(self, receipt: str, subscription: str) -> None:
        try:
            await asyncio.to_thread(
                self.subscriber.modify_ack_deadline,
                request={
                    "subscription": subscription,
                    "ack_ids": [receipt],
                    "ack_deadline_seconds": 0,
                },
                timeout=15,
                retry=None,
            )
        except (GoogleAPICallError, GoogleAuthError) as exc:
            raise PersistenceError("Failed to release upload message for retry") from exc

    async def publish(self, topic: str, event: dict[str, Any]) -> None:
        await self.publish_bytes(topic, json.dumps(event).encode())

    async def publish_bytes(self, topic: str, payload: bytes, **attributes: str) -> None:
        def send() -> None:
            self.publisher.publish(topic, payload, **attributes).result(timeout=25)

        try:
            await asyncio.to_thread(send)
        except (GoogleAPICallError, GoogleAuthError, TimeoutError) as exc:
            raise PersistenceError("Failed to publish upload notification") from exc

    async def dead_letter(self, stage: str, upload_id: str | None, code: str) -> None:
        await self.publish(
            settings.spool_parse_dlq_topic if stage == "parse" else settings.spool_load_dlq_topic,
            {"upload_id": upload_id, "error_code": code},
        )

    async def replay_event(self, descriptor: UploadDescriptor) -> None:
        await self.publish(
            settings.spool_event_topic,
            {
                "Records": [
                    {
                        "eventName": "ObjectCreated:Put",
                        "s3": {
                            "bucket": {"name": settings.spool_bucket},
                            "object": {"key": quote(descriptor.object_key, safe="/")},
                        },
                    }
                ],
            },
        )

    async def close(self) -> None:
        await asyncio.to_thread(self.objects.close)
        await asyncio.to_thread(self.subscriber.close)
        await asyncio.to_thread(self.publisher.stop)
        await asyncio.to_thread(self.publisher.transport.close)
