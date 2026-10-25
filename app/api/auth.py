from fastapi import APIRouter

from app.core.auth import AuthenticatedClaims
from app.services.traffic_service import ClientIdentity, authenticated_identity

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get("/me")
async def get_current_identity(claims: AuthenticatedClaims) -> ClientIdentity:
    return authenticated_identity(claims)
