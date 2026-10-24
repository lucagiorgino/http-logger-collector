from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class TrafficLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=255)
    timestamp: datetime
    method: str = Field(min_length=1, max_length=16)
    url: HttpUrl
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=1_000_000)
    source_ip: str | None = Field(default=None, max_length=45)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.upper()

    @field_validator("headers")
    @classmethod
    def limit_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("headers must contain at most 100 entries")
        if any(len(key) > 256 or len(header_value) > 8192 for key, header_value in value.items()):
            raise ValueError("header names and values exceed allowed lengths")
        return value


class TrafficLogBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs: list[TrafficLogRequest] = Field(min_length=1, max_length=500)


class BatchReceipt(BaseModel):
    batch_id: str
    accepted_count: int
    status: Literal["stored", "queued"]


class StoredTrafficLog(BaseModel):
    id: str
    batch_id: str
    user_id: str
    device_id: str
    timestamp: datetime
    received_at: datetime
    method: str
    url: str
    status_code: int
    headers: dict[str, str]
    source_ip: str | None
    body_length: int
    body_sha256: str | None


class TrafficLogPage(BaseModel):
    items: list[StoredTrafficLog]
    next_cursor: str | None
