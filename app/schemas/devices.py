from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EncryptionMode = Literal["tls_only", "optional", "required"]

DEVICE_EXAMPLE = {
    "device_id": "device-456",
    "name": "Office laptop",
    "registered_at": "2026-10-25T07:00:00Z",
    "version": 1,
    "encryption_mode": "tls_only",
}


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "device_id": "device-456",
                    "name": "Office laptop",
                    "encryption_mode": "tls_only",
                }
            ]
        },
    )

    device_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=128)
    encryption_mode: EncryptionMode = "tls_only"

    @field_validator("device_id", "name")
    @classmethod
    def validate_registration_text(cls, value: str) -> str:
        if (
            not value.strip()
            or value != value.strip()
            or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        ):
            raise ValueError(
                "Registration fields must be nonblank and contain no controls or surrounding whitespace"
            )
        value.encode("utf-8")
        return value

    @field_validator("device_id")
    @classmethod
    def validate_device_path(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("device_id must be a single resource path segment")
        return value


class DeviceSummary(BaseModel):
    device_id: str
    name: str | None = None
    registered_at: datetime | None = None
    version: int
    encryption_mode: EncryptionMode


class DevicePage(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"items": [DEVICE_EXAMPLE], "next_after": None}]}
    )

    items: list[DeviceSummary]
    next_after: str | None


class ConfigurationPatch(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"expected_version": 1, "encryption_mode": "required"}]},
    )

    expected_version: int = Field(ge=0, le=2**63 - 2)
    encryption_mode: EncryptionMode


class ConfigurationStatus(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "version": 1,
                    "status": "applied",
                    "supports_encryption": False,
                    "key_id": None,
                    "detail": "TLS-only policy applied",
                }
            ]
        },
    )

    version: int = Field(ge=0, le=2**63 - 1)
    status: Literal["applied", "unsupported", "error"]
    supports_encryption: bool
    key_id: str | None = Field(default=None, min_length=1, max_length=128)
    detail: str | None = Field(default=None, max_length=500)


class ReportedConfigurationStatus(ConfigurationStatus):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "version": 1,
                    "status": "applied",
                    "supports_encryption": False,
                    "key_id": None,
                    "detail": "TLS-only policy applied",
                    "reported_at": "2026-10-25T07:01:00Z",
                }
            ]
        }
    )

    reported_at: datetime


class EncryptionParameters(BaseModel):
    algorithm: str
    encryption: str
    curve: str
    content_type: Literal["application/jose"] = "application/jose"
    public_key: dict[str, str]


class DeviceConfiguration(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    **DEVICE_EXAMPLE,
                    "updated_at": "2026-10-25T07:00:00Z",
                    "encryption": None,
                    "refresh_after_seconds": 300,
                    "configuration_status": None,
                }
            ]
        }
    )

    device_id: str
    name: str | None = None
    registered_at: datetime | None = None
    updated_at: datetime | None = None
    version: int
    encryption_mode: EncryptionMode
    encryption: EncryptionParameters | None = None
    refresh_after_seconds: int = 300
    configuration_status: ReportedConfigurationStatus | None = None
