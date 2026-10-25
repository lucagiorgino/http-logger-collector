import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from typing import Any
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException, Request

from app.core.security import redact_sensitive_headers
from app.core.storage_encryption import StorageEncryption
from app.schemas.traffic import BatchReceipt, TrafficLogRequest
from app.schemas.workspace import StoragePolicy
from app.services.ingestion import IngestionPublisher

DEVICE_ID_CLAIM = "https://amazing-http-logger/device_id"


@dataclass(frozen=True)
class ClientIdentity:
    device_id: str
    client_id: str | None


@dataclass(frozen=True)
class IngestionContext:
    identity: ClientIdentity
    source_ip: str | None


def identity_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authenticated identity",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return value


def authenticated_subject(claims: dict[str, Any]) -> str:
    return identity_claim(claims, "sub")


def authenticated_identity(claims: dict[str, Any]) -> ClientIdentity:
    subject = authenticated_subject(claims)
    client_claim = "azp" if "azp" in claims else "client_id"
    return ClientIdentity(
        device_id=identity_claim(claims, DEVICE_ID_CLAIM) if DEVICE_ID_CLAIM in claims else subject,
        client_id=identity_claim(claims, client_claim) if client_claim in claims else None,
    )


def can_manage_devices(claims: dict[str, Any]) -> bool:
    permissions = claims.get("permissions", [])
    scope = claims.get("scope", "")
    return (
        isinstance(permissions, list)
        and "manage:devices" in permissions
        or isinstance(scope, str)
        and "manage:devices" in scope.split()
    )


def authorized_device_id(claims: dict[str, Any], device_id: str | None = None) -> str | None:
    identity = authenticated_identity(claims)
    if can_manage_devices(claims):
        return device_id
    if device_id is not None and device_id != identity.device_id:
        raise HTTPException(status_code=403, detail="Access is limited to the authenticated device")
    return identity.device_id


def ingestion_context(claims: dict[str, Any], request: Request) -> IngestionContext:
    identity = authenticated_identity(claims)
    source_ip = None
    if request.client is not None:
        try:
            source_ip = str(ip_address(request.client.host))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid client network address") from None
    return IngestionContext(identity=identity, source_ip=source_ip)


async def ingest_traffic_logs(
    payloads: list[TrafficLogRequest],
    context: IngestionContext,
    publisher: IngestionPublisher,
    policy: StoragePolicy,
    storage: StorageEncryption,
) -> BatchReceipt:
    batch_id = str(uuid4())
    documents = await asyncio.to_thread(normalize_batch, payloads, batch_id, context)
    await storage.protect(documents, payloads, policy)
    return await publisher.publish(batch_id, documents)


def normalize_batch(
    payloads: list[TrafficLogRequest], batch_id: str, context: IngestionContext
) -> list[dict[str, Any]]:
    return [normalize_traffic_log(payload, batch_id, context) for payload in payloads]


def normalize_traffic_log(
    payload: TrafficLogRequest, batch_id: str, context: IngestionContext
) -> dict[str, Any]:
    body_bytes = payload.body.encode("utf-8") if payload.body is not None else None
    return {
        "_id": ObjectId(),
        "batch_id": batch_id,
        "metadata": {
            "device_id": context.identity.device_id,
            "suspicious": False,
        },
        "client_id": context.identity.client_id,
        "timestamp": payload.timestamp,
        "received_at": datetime.now(UTC),
        "method": payload.method,
        "url": str(payload.url),
        "status_code": payload.status_code,
        "request_headers": redact_sensitive_headers(payload.request_headers),
        "response_headers": redact_sensitive_headers(payload.response_headers),
        "captured_source_ip": (
            str(payload.captured_source_ip) if payload.captured_source_ip is not None else None
        ),
        "destination_ip": str(payload.destination_ip)
        if payload.destination_ip is not None
        else None,
        **payload.model_dump(
            include={
                "source_port",
                "destination_port",
                "http_version",
                "request_size_bytes",
                "response_size_bytes",
                "duration_ms",
                "process_name",
                "process_id",
                "request_id",
            }
        ),
        "source_ip": context.source_ip,
        "body_length": len(body_bytes) if body_bytes is not None else 0,
        "body_sha256": sha256(body_bytes).hexdigest() if body_bytes is not None else None,
    }
