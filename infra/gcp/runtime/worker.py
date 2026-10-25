"""HTTP-supervised Cloud Run consumer with continuously allocated CPU."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from socket import gethostname
from uuid import uuid4

from fastapi import FastAPI, HTTPException

from app.core.config import settings
from app.db.mongo import MongoRepository, PersistenceError
from app.services.redis_queue import RedisStreamQueue
from app.worker import consume_once

from .configuration import validate_cloud_connections

logger = logging.getLogger(__name__)


async def consume(queue: RedisStreamQueue, repository: MongoRepository) -> None:
    consumer = f"{gethostname()}-{uuid4().hex}"
    while True:
        try:
            await consume_once(queue, repository, consumer)
        except PersistenceError:
            logger.error("Ingestion failed; unacknowledged batches remain available for retry")
            await asyncio.sleep(settings.worker_retry_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    validate_cloud_connections()
    queue = RedisStreamQueue()
    repository = MongoRepository()
    try:
        await repository.ensure_indexes()
        await queue.initialize()
        task = asyncio.create_task(consume(queue, repository))
        app.state.consumer = task
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    finally:
        try:
            await queue.close()
        finally:
            await repository.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    if app.state.consumer.done():
        logger.error("Consumer stopped unexpectedly; instance must restart")
        raise HTTPException(status_code=503, detail="Consumer is not running")
    return {"status": "ok"}
