from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workspace import StoragePolicy

UploadState = Literal[
    "uploading",
    "received",
    "parsing",
    "queued",
    "loading",
    "stored",
    "parse_failed",
    "load_failed",
]


class UploadDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    upload_id: str
    object_key: str
    device_id: str
    client_id: str | None
    source_ip: str | None
    received_at: datetime
    encrypted: bool
    sha256: str
    wire_bytes: int
    policy: StoragePolicy


class UploadReceipt(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "upload_id": "84568e1a-9cdc-4b53-95dd-b0269103ea32",
                    "status": "received",
                    "duplicate": False,
                }
            ]
        }
    )

    upload_id: str
    status: UploadState
    duplicate: bool = False


class UploadStatus(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "upload_id": "84568e1a-9cdc-4b53-95dd-b0269103ea32",
                    "status": "stored",
                    "accepted_count": 1,
                    "error_code": None,
                    "raw_deleted": True,
                }
            ]
        }
    )

    upload_id: str
    status: UploadState
    accepted_count: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    raw_deleted: bool = False
