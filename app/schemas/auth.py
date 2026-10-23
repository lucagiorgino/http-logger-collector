from pydantic import BaseModel, Field


class AuthorizeRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    purpose: str = "traffic-monitoring"


class TokenRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    audience: str = "agent"


class TokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"
