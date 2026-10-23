from fastapi import FastAPI

from app.api.agents import router as agents_router
from app.api.auth import router as auth_router

app = FastAPI(title="Amazing HTTP Logger")
app.include_router(auth_router)
app.include_router(agents_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
