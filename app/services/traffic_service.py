from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from fastapi import HTTPException, status

from app.schemas.traffic import TrafficLogRequest


class TrafficLogRepository(Protocol):
    async def insert_traffic_log(self, document: dict[str, Any]) -> str: ...


async def persist_traffic_log(
    payload: TrafficLogRequest,
    redacted_headers: dict[str, str],
    authenticated_subject: str | None,
    repository: TrafficLogRepository,
) -> dict[str, Any]:
    if not authenticated_subject or authenticated_subject != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated subject cannot submit logs for this user",
        )

    body_bytes = payload.body.encode("utf-8") if payload.body is not None else None
    document: dict[str, Any] = {
        "user_id": payload.user_id,
        "device_id": payload.device_id,
        "timestamp": payload.timestamp,
        "received_at": datetime.now(UTC),
        "method": payload.method,
        "url": str(payload.url),
        "status_code": payload.status_code,
        "headers": redacted_headers,
        "source_ip": payload.source_ip,
        "body_length": len(body_bytes) if body_bytes is not None else 0,
        "body_sha256": sha256(body_bytes).hexdigest() if body_bytes is not None else None,
    }
    record_id = await repository.insert_traffic_log(document)
    return {
        "accepted": True,
        "record_id": record_id,
    }
