from fastapi import APIRouter

from app.schemas.auth import AuthorizeRequest, TokenRequest, TokenResponse
from app.services.auth_service import issue_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/authorize")
async def authorize(payload: AuthorizeRequest) -> dict[str, str]:
    return {
        "status": "authorized",
        "user_id": payload.user_id,
        "device_id": payload.device_id,
    }


@router.post("/token", response_model=TokenResponse)
async def token(payload: TokenRequest) -> TokenResponse:
    return TokenResponse(token=issue_token(payload.user_id), token_type="bearer")
