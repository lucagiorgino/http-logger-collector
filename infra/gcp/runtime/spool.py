"""Cloud Run supervisor for parsing and Mongo loading workers."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException

from app.spool_worker import operate

from .configuration import validate_cloud_connections


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    validate_cloud_connections()
    stage = os.environ["SPOOL_WORKER_STAGE"]
    if stage not in {"parse", "load"}:
        raise ValueError("SPOOL_WORKER_STAGE must be parse or load")
    task = asyncio.create_task(operate(stage, None, manage_signals=False))
    app.state.consumer = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    if app.state.consumer.done():
        raise HTTPException(status_code=503, detail="Upload worker stopped")
    return {"status": "ok"}
