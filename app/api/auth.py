from fastapi import APIRouter

from app.core.auth import AuthenticatedClaims
from app.services.traffic_service import ClientIdentity, authenticated_identity

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get(
    "/me",
    summary="Resolve the verified device and client identity",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {"device_id": "device-456", "client_id": "agent-client"}
                }
            }
        },
        401: {"description": "Missing/invalid access token or authenticated identity."},
        503: {"description": "Auth0 discovery or signing keys are temporarily unavailable."},
    },
)
async def get_current_identity(claims: AuthenticatedClaims) -> ClientIdentity:
    """Return server-resolved identity, not caller-supplied attribution.

    **Required permission:** None beyond authentication.

    Device ID uses the namespaced device claim, falling back to `sub`.
    Client ID uses `azp`, falling back to `client_id`, and may be null.
    """
    return authenticated_identity(claims)
