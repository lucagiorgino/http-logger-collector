import os
import re

from dotenv import load_dotenv
from fastapi import HTTPException, Request, status
from fastapi_plugin.fast_api_client import Auth0FastAPI

load_dotenv()

auth0 = Auth0FastAPI(
    domain=os.environ.get("AUTH0_DOMAIN"),
    audience=os.environ.get("AUTH0_AUDIENCE", ""),
)

JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


async def require_bearer_token(request: Request) -> dict:
    authorization = request.headers.get("Authorization", "")
    if not authorization or not authorization.lower().startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
        )

    token = authorization.split(" ", 1)[1].strip()
    if not JWT_RE.fullmatch(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
        )

    try:
        return await auth0.api_client.verify_request(
            headers=dict(request.headers),
            http_method=request.method,
            http_url=str(request.url),
        )
    except Exception as exc:  # pragma: no cover - SDK raises verification errors
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
        ) from exc
