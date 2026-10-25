from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class StoragePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retain_request_body: bool = Field(default=True, strict=True)
    retain_authorization_header: bool = Field(default=True, strict=True)
    retain_cookies: bool = Field(default=True, strict=True)
    encrypt_request_body: bool = Field(default=True, strict=True)
    encrypt_authorization_header: bool = Field(default=True, strict=True)
    encrypt_cookies: bool = Field(default=True, strict=True)


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_enabled: bool = Field(default=True, strict=True)
    retention_days: int = Field(default=7, strict=True, ge=1, le=24855)
    suspicious_retention_enabled: bool = Field(default=True, strict=True)
    suspicious_retention_days: int = Field(default=30, strict=True, ge=1, le=24855)


class WorkspaceConfiguration(StoragePolicy, RetentionPolicy):
    version: int = 0
    updated_at: datetime | None = None


class WorkspacePatch(StoragePolicy, RetentionPolicy):
    expected_version: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
    encrypt_request_body: bool = Field(strict=True)
    encrypt_authorization_header: bool = Field(strict=True)
    encrypt_cookies: bool = Field(strict=True)
