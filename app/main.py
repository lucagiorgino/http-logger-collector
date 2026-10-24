from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from app.api.agents import router as agents_router
from app.core.auth import auth0
from app.db.mongo import MongoRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    repository = MongoRepository()
    app.state.mongo_repository = repository
    await repository.ensure_indexes()
    try:
        yield
    finally:
        repository.close()


app = FastAPI(title="Amazing HTTP Logger", lifespan=lifespan)

app.include_router(agents_router)

AuthenticatedClaims = Annotated[dict, Depends(auth0.require_auth())]
ScopedClaims = Annotated[dict, Depends(auth0.require_auth(scopes="read:messages"))]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/public", tags=["public"])
async def public() -> dict[str, str]:
    return {"message": "Hello from a public endpoint! You don't need to be authenticated to see this."}


@app.get("/api/private", tags=["private"])
async def private(claims: AuthenticatedClaims) -> dict[str, str | None]:
    return {
        "message": "Hello from a private endpoint! You need to be authenticated to see this.",
        "user_id": claims.get("sub"),
    }


@app.get("/api/private-scoped", tags=["private-scoped"])
async def private_scoped(claims: ScopedClaims) -> dict[str, str | None]:
    return {
        "message": "Hello from a private endpoint! You need to be authenticated and have a scope of read:messages to see this.",
        "user_id": claims.get("sub"),
    }
