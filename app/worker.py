import asyncio
import logging
import signal
from socket import gethostname
from uuid import uuid4

from pymongo import timeout

from app.core.config import settings
from app.db.mongo import MongoRepository, PersistenceError
from app.services.redis_queue import InvalidQueuedBatch, RedisStreamQueue

logger = logging.getLogger(__name__)


async def consume_once(queue: RedisStreamQueue, repository: MongoRepository, consumer: str) -> bool:
    message = await queue.receive(consumer)
    if message is None:
        return False
    try:
        batch = queue.decode(*message)
    except InvalidQueuedBatch:
        logger.error("Malformed queued batch %r retained pending for investigation", message[0])
        return False
    with timeout(settings.worker_write_timeout_seconds):
        await repository.insert_queued_traffic_logs(batch.documents)
    await queue.acknowledge(batch.message_id)
    return True


async def run() -> None:
    queue = RedisStreamQueue()
    repository = MongoRepository()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    consumer = f"{gethostname()}-{uuid4().hex}"
    try:
        await repository.ensure_indexes()
        await queue.initialize()
        logger.info("Redis ingestion worker started")
        while not stop.is_set():
            try:
                await consume_once(queue, repository, consumer)
            except PersistenceError:
                logger.error("Ingestion failed; unacknowledged batches remain available for retry")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_retry_seconds)
                except TimeoutError:
                    pass
    finally:
        try:
            await queue.close()
        finally:
            await repository.close()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
