import re
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_plugin import Auth0FastAPI

from app.core.config import settings

auth0 = Auth0FastAPI(
    domain=settings.auth0_domain,
    audience=settings.auth0_audience,
)
verify_with_auth0 = auth0.require_auth()

JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="Auth0Bearer",
    bearerFormat="JWT",
    description=(
        "Auth0 RS256 access token for this API's configured audience. "
        "Use Authorization: Bearer <access_token> over HTTPS. "
        "Each operation lists its required permission and device-access restrictions. "
        "manage:devices is accepted from the JWT permissions array or space-separated scope claim; "
        "it grants fleet management/read access, not permission to impersonate another device."
    ),
)


class AuthenticationUnavailable(RuntimeError):
    pass


async def require_bearer_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
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
        claims = await verify_with_auth0(request)
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise AuthenticationUnavailable("Authentication service is unavailable") from None
        raise HTTPException(
            status_code=exc.status_code,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if "cnf" in claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Proof-bound tokens are not supported",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


AuthenticatedClaims = Annotated[dict[str, Any], Depends(require_bearer_token)]
