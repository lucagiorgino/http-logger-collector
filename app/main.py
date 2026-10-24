import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.batches import router as batches_router
from app.api.traffic_logs import router as traffic_logs_router
from app.db.mongo import MongoRepository, PersistenceError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    repository = MongoRepository()
    app.state.mongo_repository = repository
    try:
        await repository.ensure_indexes()
        yield
    finally:
        repository.close()


app = FastAPI(title="Amazing HTTP Logger", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(batches_router)
app.include_router(traffic_logs_router)


@app.exception_handler(PersistenceError)
async def storage_unavailable(request: Request, exc: PersistenceError) -> JSONResponse:
    logger.error("Storage is unavailable")
    return JSONResponse(
        status_code=503,
        content={"detail": "Storage is temporarily unavailable"},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
