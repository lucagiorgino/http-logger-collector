from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException, status

from app.core.security import redact_sensitive_headers
from app.schemas.traffic import BatchReceipt, TrafficLogRequest
from app.services.ingestion import IngestionPublisher


def authenticated_subject(claims: dict) -> str:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(
            status_code=401,
            detail="Missing authenticated subject",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return subject


async def ingest_traffic_logs(
    payloads: list[TrafficLogRequest],
    subject: str,
    publisher: IngestionPublisher,
) -> BatchReceipt:
    if any(payload.user_id != subject for payload in payloads):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated subject cannot submit logs for this user",
        )

    batch_id = str(uuid4())
    documents = [normalize_traffic_log(payload, batch_id) for payload in payloads]
    return await publisher.publish(batch_id, documents)


def normalize_traffic_log(payload: TrafficLogRequest, batch_id: str) -> dict[str, Any]:
    body_bytes = payload.body.encode("utf-8") if payload.body is not None else None
    return {
        "_id": ObjectId(),
        "batch_id": batch_id,
        "metadata": {"user_id": payload.user_id, "device_id": payload.device_id},
        "timestamp": payload.timestamp,
        "received_at": datetime.now(UTC),
        "method": payload.method,
        "url": str(payload.url),
        "status_code": payload.status_code,
        "headers": redact_sensitive_headers(payload.headers),
        "source_ip": payload.source_ip,
        "body_length": len(body_bytes) if body_bytes is not None else 0,
        "body_sha256": sha256(body_bytes).hexdigest() if body_bytes is not None else None,
    }
