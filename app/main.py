from fastapi import Depends, FastAPI

from app.api.agents import router as agents_router
from app.core.auth import auth0

app = FastAPI(title="Amazing HTTP Logger")

app.include_router(agents_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/public", tags=["public"])
async def public() -> dict[str, str]:
    return {"message": "Hello from a public endpoint! You don't need to be authenticated to see this."}


@app.get("/api/private", tags=["private"])
async def private(claims: dict = Depends(auth0.require_auth())) -> dict[str, str | None]:
    return {
        "message": "Hello from a private endpoint! You need to be authenticated to see this.",
        "user_id": claims.get("sub"),
    }


@app.get("/api/private-scoped", tags=["private-scoped"])
async def private_scoped(claims: dict = Depends(auth0.require_auth(scopes="read:messages"))) -> dict[str, str | None]:
    return {
        "message": "Hello from a private endpoint! You need to be authenticated and have a scope of read:messages to see this.",
        "user_id": claims.get("sub"),
    }
