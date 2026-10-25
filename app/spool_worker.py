import argparse
import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from app.core.config import settings
from app.core.encryption import get_encryption_keys
from app.core.logging import logger
from app.core.openbao import AsyncOpenBaoClient, KMSUnavailable
from app.core.storage_encryption import StorageEncryption
from app.db.mongo import MongoRepository, PersistenceError
from app.schemas.uploads import UploadDescriptor
from app.services.upload_cloud import UploadCloud
from app.services.upload_pipeline import UploadLoader, UploadParser
from app.services.upload_queue import UploadQueue
from app.services.upload_state import UploadStateStore

HEARTBEAT_INTERVAL = 10


async def operate(action: str, upload_id: str | None, *, manage_signals: bool = True) -> None:
    settings.validate_spool()
    cloud = UploadCloud()
    repository = MongoRepository()
    queue = UploadQueue(cloud)
    store = UploadStateStore(repository.database)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if manage_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
    try:
        await store.initialize()
        if action in {"replay", "cleanup"}:
            record = await store.get(upload_id)
            if record is None:
                raise PersistenceError("Upload receipt not found")
            if action == "cleanup":
                if record["status"] != "stored":
                    raise PersistenceError("Only Mongo-persisted uploads may be cleaned up")
                await UploadLoader(cloud, store, queue, repository).cleanup(record, force=True)
            else:
                if record["status"] in {"stored", "uploading"} or record.get("raw_deleted"):
                    raise PersistenceError("Upload is not eligible for replay")
                descriptor = UploadDescriptor.model_validate(record["descriptor"])
                # Verify raw availability before resetting progress. No bucket listing.
                await cloud.get(descriptor.object_key)
                changed = await store.update(
                    {
                        "_id": upload_id,
                        "status": record["status"],
                        "$or": [
                            {"lease_until": {"$exists": False}},
                            {"lease_until": {"$lt": datetime.now(UTC)}},
                            {
                                "status": {
                                    "$in": ["queued", "received", "parse_failed", "load_failed"]
                                }
                            },
                        ],
                    },
                    {
                        "status": "received",
                        "lease_token": "",
                        "parse_attempts": 0,
                        "load_attempts": 0,
                        "error_code": None,
                    },
                )
                if not changed:
                    raise PersistenceError("Upload is actively processing; retry replay later")
                await cloud.replay_event(descriptor)
            logger.info("Upload maintenance operation completed")
            return
        await repository.initialize_pipeline_status()
        instance = uuid4().hex

        async def heartbeat(status: str) -> None:
            await repository.report_pipeline_worker(action, instance, status)

        if action == "load":
            await repository.ensure_indexes()
            processor = UploadLoader(cloud, store, queue, repository)
            await run_loop(processor.consume_once, stop, heartbeat)
        else:
            keys = await asyncio.to_thread(get_encryption_keys)
            async with AsyncOpenBaoClient() as kms:
                await kms.authenticate()
                storage = StorageEncryption(kms)
                await asyncio.to_thread(storage.validate)
                parser = UploadParser(cloud, store, queue, keys, storage)
                await run_loop(parser.consume_once, stop, heartbeat)
    finally:
        try:
            await cloud.close()
        finally:
            await repository.close()
        if manage_signals:
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)


async def run_loop(
    consume: Callable[[], Awaitable[bool]],
    stop: asyncio.Event,
    heartbeat: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    last_report = 0.0
    last_status = None
    current_status = "healthy"
    report_lock = asyncio.Lock()

    async def report(status: str) -> None:
        nonlocal last_report, last_status
        if heartbeat is None:
            return
        async with report_lock:
            if last_status == status and monotonic() - last_report < HEARTBEAT_INTERVAL:
                return
            try:
                await heartbeat(status)
            except PersistenceError:
                logger.error("Worker heartbeat unavailable; ingestion continues")
            last_report, last_status = monotonic(), status

    async def keep_alive() -> None:
        while True:
            await report(current_status)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    heartbeat_task = asyncio.create_task(keep_alive()) if heartbeat is not None else None
    try:
        while not stop.is_set():
            try:
                await consume()
            except (PersistenceError, KMSUnavailable):
                logger.error("Upload dependency unavailable; durable work retained for retry")
                current_status = "degraded"
                await report(current_status)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_retry_seconds)
                except TimeoutError:
                    pass
            else:
                current_status = "healthy"
                await report(current_status)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        await report("unavailable")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingestion workers and upload recovery")
    parser.add_argument("action", choices=["parse", "load", "replay", "cleanup"])
    parser.add_argument("upload_id", nargs="?")
    args = parser.parse_args()
    if args.action in {"replay", "cleanup"} and args.upload_id is None:
        parser.error("replay and cleanup require upload_id")
    asyncio.run(operate(args.action, args.upload_id))


if __name__ == "__main__":
    main()
