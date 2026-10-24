from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EncryptionMode = Literal["tls_only", "optional", "required"]


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
    version: int
    encryption_mode: EncryptionMode
    encryption: EncryptionParameters | None = None
    refresh_after_seconds: int = 300
    configuration_status: ReportedConfigurationStatus | None = None
