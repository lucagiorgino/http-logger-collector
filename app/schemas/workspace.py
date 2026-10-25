from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class StoragePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encrypt_request_body: bool = Field(default=True, strict=True)
    encrypt_authorization_header: bool = Field(default=True, strict=True)
    encrypt_cookies: bool = Field(default=True, strict=True)


class WorkspaceConfiguration(StoragePolicy):
    version: int = 0
    updated_at: datetime | None = None


class WorkspacePatch(StoragePolicy):
    expected_version: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
    encrypt_request_body: bool = Field(strict=True)
    encrypt_authorization_header: bool = Field(strict=True)
    encrypt_cookies: bool = Field(strict=True)
