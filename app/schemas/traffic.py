from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class TrafficLogRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    timestamp: datetime
    method: str = Field(..., min_length=1)
    url: HttpUrl
    status_code: int = Field(..., ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    source_ip: str | None = None


class TrafficLogResponse(BaseModel):
    accepted: bool
    record_id: str
    status: str
