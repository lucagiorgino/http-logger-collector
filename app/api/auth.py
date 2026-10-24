from fastapi import APIRouter

from app.core.auth import AuthenticatedClaims
from app.services.traffic_service import authenticated_subject

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get("/me")
async def get_current_user(claims: AuthenticatedClaims) -> dict[str, str]:
    return {"user_id": authenticated_subject(claims)}
