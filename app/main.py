import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from app.api.auth import router as auth_router
from app.api.batches import router as batches_router
from app.api.devices import router as devices_router
from app.api.traffic_logs import router as traffic_logs_router
from app.api.workspace import router as workspace_router
from app.core.auth import AuthenticationUnavailable
from app.core.config import settings
from app.core.encryption import get_encryption_keys
from app.core.openbao import KMSUnavailable
from app.core.storage_encryption import get_storage_encryption
from app.db.mongo import MongoRepository, PersistenceError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_encryption_keys()
    get_storage_encryption().validate()
    repository = MongoRepository()
    app.state.mongo_repository = repository
    try:
        await repository.ensure_indexes()
        yield
    finally:
        await repository.close()


app = FastAPI(
    title="Amazing HTTP Logger",
    description=(
        "Collect HTTP traffic from authenticated devices and manage their configuration. "
        "Expand an operation to see its **Required permission**, access restrictions and examples. "
        "All `/api/v1/` operations require an Auth0 bearer access token and HTTPS by default. "
        "An operation stating **None beyond authentication** does not require any OAuth scope. "
        "The device identity comes from the verified `https://amazing-http-logger/device_id` "
        "claim, falling back to `sub`; `/api/v1/me` shows the resolved identity. "
        "Use **Authorize** to supply an access token; this API does not issue tokens."
    ),
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(batches_router)
app.include_router(traffic_logs_router)
app.include_router(devices_router)
app.include_router(workspace_router)


@app.middleware("http")
async def require_secure_transport(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    if (
        settings.require_https
        and request.url.path.startswith("/api/v1/")
        and request.url.scheme != "https"
    ):
        return JSONResponse(status_code=400, content={"detail": "HTTPS is required"})
    response = await call_next(request)
    if request.url.path.startswith(
        ("/api/v1/devices", "/api/v1/workspace/", "/api/v1/traffic-logs")
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(PersistenceError)
@app.exception_handler(KMSUnavailable)
async def storage_unavailable(
    request: Request, exc: PersistenceError | KMSUnavailable
) -> JSONResponse:
    logger.error("Storage or key management is unavailable")
    return JSONResponse(
        status_code=503,
        content={"detail": "Storage or key management is temporarily unavailable"},
    )


@app.get(
    "/health",
    tags=["health"],
    summary="Check that the API process is responding",
    responses={200: {"content": {"application/json": {"example": {"status": "ok"}}}}},
)
async def health() -> dict[str, str]:
    """Return process liveness; this does not check MongoDB, OpenBao or Auth0 availability.

    **Required permission:** None. Public endpoint; no bearer token is required.
    """
    return {"status": "ok"}


@app.exception_handler(AuthenticationUnavailable)
async def authentication_unavailable(
    request: Request, exc: AuthenticationUnavailable
) -> JSONResponse:
    logger.error("The authentication service is unavailable")
    return JSONResponse(
        status_code=503,
        content={"detail": "The authentication service is temporarily unavailable"},
    )
