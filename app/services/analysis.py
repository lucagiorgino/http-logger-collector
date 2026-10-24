import base64
import binascii
import json
from datetime import datetime
from typing import Any

from bson import ObjectId
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

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


def stored_log(document: dict[str, Any]) -> StoredTrafficLog:
    return StoredTrafficLog(
        id=str(document["_id"]),
        user_id=document["metadata"]["user_id"],
        device_id=document["metadata"]["device_id"],
        **{
            key: document[key]
            for key in (
                "batch_id",
                "timestamp",
                "received_at",
                "method",
                "url",
                "status_code",
                "headers",
                "source_ip",
                "body_length",
                "body_sha256",
            )
        },
    )
