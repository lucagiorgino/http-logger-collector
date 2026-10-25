from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EncryptionMode = Literal["tls_only", "optional", "required"]


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    items: list[DeviceSummary]
    next_after: str | None


class ConfigurationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, le=2**63 - 2)
    encryption_mode: EncryptionMode


class ConfigurationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0, le=2**63 - 1)
    status: Literal["applied", "unsupported", "error"]
    supports_encryption: bool
    key_id: str | None = Field(default=None, min_length=1, max_length=128)
    detail: str | None = Field(default=None, max_length=500)


class ReportedConfigurationStatus(ConfigurationStatus):
    reported_at: datetime


class EncryptionParameters(BaseModel):
    algorithm: str
    encryption: str
    curve: str
    content_type: Literal["application/jose"] = "application/jose"
    public_key: dict[str, str]


class DeviceConfiguration(BaseModel):
    device_id: str
    name: str | None = None
    registered_at: datetime | None = None
    updated_at: datetime | None = None
    version: int
    encryption_mode: EncryptionMode
    encryption: EncryptionParameters | None = None
    refresh_after_seconds: int = 300
    configuration_status: ReportedConfigurationStatus | None = None
