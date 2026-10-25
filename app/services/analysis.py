import base64
import binascii
import json
from datetime import datetime
from typing import Any

from bson import ObjectId
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from app.core.security import redact_sensitive_headers
from app.schemas.traffic import StoredTrafficLog


class PageCursor(BaseModel):
    timestamp: AwareDatetime
    id: str = Field(pattern=r"^[0-9a-f]{24}$")


def decode_cursor(cursor: str | None) -> tuple[datetime, ObjectId] | None:
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        position = PageCursor.model_validate_json(raw)
    except (ValueError, binascii.Error, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc
    return position.timestamp, ObjectId(position.id)


def encode_cursor(document: dict[str, Any]) -> str:
    raw = json.dumps(
        {"timestamp": document["timestamp"].isoformat(), "id": str(document["_id"])}
    ).encode()
    return base64.urlsafe_b64encode(raw).decode()


def stored_log(document: dict[str, Any], *, include_body: bool = False) -> StoredTrafficLog:
    request_headers = redact_sensitive_headers(
        document.get("request_headers", document.get("headers", {}))
    )
    return StoredTrafficLog(
        id=str(document["_id"]),
        device_id=document["metadata"]["device_id"],
        client_id=document.get("client_id"),
        headers=request_headers,
        request_headers=request_headers,
        response_headers=redact_sensitive_headers(document.get("response_headers", {})),
        body=document.get("body") if include_body else None,
        storage_encrypted_fields=document.get("storage_encryption", {}).get("fields", []),
        **{
            key: document[key]
            for key in (
                "captured_source_ip",
                "destination_ip",
                "source_port",
                "destination_port",
                "http_version",
                "request_size_bytes",
                "response_size_bytes",
                "duration_ms",
                "process_name",
                "process_id",
                "request_id",
            )
            if key in document
        },
        **{
            key: document[key]
            for key in (
                "batch_id",
                "timestamp",
                "received_at",
                "method",
                "url",
                "status_code",
                "source_ip",
                "body_length",
                "body_sha256",
            )
        },
    )
