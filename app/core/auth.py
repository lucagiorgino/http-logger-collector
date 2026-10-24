import re
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_plugin.fast_api_client import Auth0FastAPI

from app.core.config import settings

auth0 = Auth0FastAPI(
    domain=settings.auth0_domain,
    audience=settings.auth0_audience,
)

JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Auth0Bearer")


async def require_bearer_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    if not JWT_RE.fullmatch(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
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
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


AuthenticatedClaims = Annotated[dict, Depends(require_bearer_token)]
