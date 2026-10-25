import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    UrlConstraints,
    field_validator,
)

ByteCount = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
ProcessId = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
Duration = Annotated[float, Field(strict=True, ge=0, le=86_400_000, allow_inf_nan=False)]
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class TrafficLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    method: str = Field(min_length=1, max_length=16, pattern=HTTP_TOKEN.pattern)
    url: Annotated[AnyHttpUrl, UrlConstraints(max_length=8192)]
    status_code: int = Field(strict=True, ge=100, le=599)
    request_headers: dict[str, str] = Field(
        default_factory=dict,
        max_length=100,
        validation_alias=AliasChoices("request_headers", "headers"),
        description="Captured request headers; legacy 'headers' is accepted instead, never both.",
    )
    response_headers: dict[str, str] = Field(default_factory=dict, max_length=100)
    captured_source_ip: IPvAnyAddress | None = Field(
        default=None,
        description="Agent-reported source of the captured HTTP connection, not the upload.",
    )
    destination_ip: IPvAnyAddress | None = Field(
        default=None,
        description="Agent-reported captured destination; the backend does not resolve it.",
    )
    source_port: Port | None = None
    destination_port: Port | None = None
    http_version: Literal["1.0", "1.1", "2", "3"] | None = None
    request_size_bytes: ByteCount | None = None
    response_size_bytes: ByteCount | None = None
    duration_ms: Duration | None = None
    process_name: str | None = Field(default=None, min_length=1, max_length=255)
    process_id: ProcessId | None = None
    request_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    body: str | None = Field(default=None, max_length=1_000_000)

    @field_validator("timestamp", mode="before")
    @classmethod
    def require_iso_timestamp(cls, value):
        if not isinstance(value, datetime) and (
            not isinstance(value, str)
            or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})",
                value,
                flags=re.ASCII,
            )
        ):
            raise ValueError("timestamp must be a timezone-aware ISO 8601 datetime")
        return value

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

    @field_validator("url", mode="before")
    @classmethod
    def validate_url_text(cls, value):
        if (
            not isinstance(value, str)
            or len(value) > 8192
            or CONTROL_CHARACTERS.search(value)
            or value != value.strip()
        ):
            raise ValueError(
                "url must be an HTTP(S) URL of at most 8192 characters without controls"
            )
        return value

    @field_validator("url")
    @classmethod
    def reject_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("url must not contain credentials")
        return value

    @field_validator("captured_source_ip", "destination_ip", mode="before")
    @classmethod
    def require_ip_text(cls, value):
        if value is not None and (not isinstance(value, str) or "%" in value):
            raise ValueError("IP addresses must be IPv4 or IPv6 strings without a zone ID")
        return value

    @field_validator("process_name")
    @classmethod
    def validate_process_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or CONTROL_CHARACTERS.search(value)):
            raise ValueError("process_name must be nonblank and contain no control characters")
        return value

    @field_validator("body", "process_name")
    @classmethod
    def require_valid_unicode(cls, value: str | None) -> str | None:
        if value is not None:
            value.encode("utf-8")
        return value

    @field_validator("request_headers", "response_headers")
    @classmethod
    def limit_headers(cls, value: dict[str, str]) -> dict[str, str]:
        names = set()
        total_bytes = 0
        for key, header_value in value.items():
            if (
                len(key) > 256
                or not HTTP_TOKEN.fullmatch(key)
                or "." in key
                or "$" in key
                or key.lower() in {"__proto__", "constructor", "prototype"}
                or key.lower() in names
            ):
                raise ValueError("header names must be unique HTTP tokens without unsafe map keys")
            if len(header_value) > 8192 or CONTROL_CHARACTERS.search(header_value):
                raise ValueError("header values must be at most 8192 characters without controls")
            names.add(key.lower())
            total_bytes += len(key.encode("utf-8")) + len(header_value.encode("utf-8"))
        if total_bytes > 32_768:
            raise ValueError("each header map must contain at most 32768 UTF-8 bytes")
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
    device_id: str
    client_id: str | None = None
    timestamp: datetime
    received_at: datetime
    method: str
    url: str
    status_code: int
    headers: dict[str, str] = Field(
        deprecated=True, description="Compatibility alias for request_headers."
    )
    request_headers: dict[str, str] = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(default_factory=dict)
    source_ip: str | None
    captured_source_ip: str | None = None
    destination_ip: str | None = None
    source_port: int | None = None
    destination_port: int | None = None
    http_version: str | None = None
    request_size_bytes: int | None = None
    response_size_bytes: int | None = None
    duration_ms: float | None = None
    process_name: str | None = None
    process_id: int | None = None
    request_id: str | None = None
    body_length: int
    body_sha256: str | None
    body: str | None = None
    storage_encrypted_fields: list[Literal["body", "authorization", "cookies"]] = Field(
        default_factory=list
    )


class TrafficLogPage(BaseModel):
    items: list[StoredTrafficLog]
    next_cursor: str | None
