import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.core.auth import AuthenticatedClaims
from app.core.config import settings
from app.core.encryption import MAX_ENCRYPTED_BYTES
from app.core.logging import logger
from app.deps import MongoRepositoryDep
from app.schemas.traffic import TRAFFIC_BATCH_EXAMPLE, TrafficLogRequest
from app.schemas.uploads import UploadDescriptor, UploadReceipt, UploadStatus
from app.schemas.workspace import StoragePolicy
from app.services.batch_codec import MAX_COMPRESSED_BYTES
from app.services.traffic_service import authorized_device_id, ingestion_context
from app.services.upload_cloud import UploadCloud
from app.services.upload_state import UploadStateStore

router = APIRouter(prefix="/api/v1/traffic-log-uploads", tags=["traffic logs"])


def get_upload_cloud(request: Request) -> UploadCloud:
    return request.app.state.upload_cloud


def get_upload_store(request: Request) -> UploadStateStore:
    return request.app.state.upload_store


CloudDep = Annotated[UploadCloud, Depends(get_upload_cloud)]
StoreDep = Annotated[UploadStateStore, Depends(get_upload_store)]


@router.post(
    "",
    status_code=202,
    response_model=UploadReceipt,
    summary="Durably spool an upload for asynchronous validation and storage",
    responses={200: {"model": UploadReceipt}, 400: {}, 403: {}, 409: {}, 413: {}, 415: {}, 503: {}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "Send gzip JSON with Content-Encoding: gzip, or compact JWE over gzip "
                "with no Content-Encoding. The JSON example is before compression. "
                "Limits: 1 MiB gzip, 8 MiB expanded, 1-500 events; JWE wire 1,500,000 bytes. "
                "Identity and upload source IP are server-derived. Event/schema/JWE errors "
                "are reported asynchronously through the upload status endpoint."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["logs"],
                        "properties": {
                            "logs": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 500,
                                "items": TrafficLogRequest.model_json_schema(),
                            }
                        },
                    },
                    "examples": {
                        "before_compression": {
                            "summary": "JSON before gzip compression; send compressed bytes",
                            "value": TRAFFIC_BATCH_EXAMPLE,
                        }
                    },
                },
                "application/jose": {
                    "schema": {"type": "string"},
                    "examples": {"jwe": {"value": "<protected>..<iv>.<ciphertext>.<tag>"}},
                },
            },
        }
    },
)
async def create_upload(
    request: Request,
    response: Response,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    cloud: CloudDep,
    store: StoreDep,
    x_batch_id: Annotated[str | None, Header(max_length=128)] = None,
) -> UploadReceipt:
    """Store exact wire bytes before expensive content processing.

    **Required permission:** None beyond authentication.

    HTTP 202 means raw storage, NOT validated logs or Mongo persistence.
    Poll the returned upload_id. Invalid content is rejected asynchronously.
    Original fields (including fields later discarded) are temporarily retained,
    encrypted at rest. X-Batch-Id is optional; reuse it only for identical bytes.
    """
    context = ingestion_context(claims, request)
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    encoding = request.headers.get("content-encoding", "").strip().lower()
    encrypted = media == "application/jose"
    if (encrypted and encoding) or (
        not encrypted and (media != "application/json" or encoding != "gzip")
    ):
        raise HTTPException(status_code=415, detail="Expected gzip JSON or application/jose")
    if x_batch_id is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", x_batch_id):
        raise HTTPException(status_code=400, detail="Invalid X-Batch-Id")
    configuration = await repository.get_device_configuration(context.identity.device_id)
    mode = configuration["encryption_mode"]
    if (mode == "required" and not encrypted) or (mode == "tls_only" and encrypted):
        raise HTTPException(
            status_code=403, detail="Upload format violates device encryption policy"
        )
    policy = StoragePolicy.model_validate(
        {
            key: value
            for key, value in (await repository.get_workspace_configuration()).items()
            if key in StoragePolicy.model_fields
        }
    )
    wire = bytearray()
    limit = MAX_ENCRYPTED_BYTES if encrypted else MAX_COMPRESSED_BYTES
    async for chunk in request.stream():
        if len(wire) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Batch exceeds wire size limit")
        wire.extend(chunk)
    if not wire:
        raise HTTPException(status_code=400, detail="Empty upload")
    upload_id = (
        str(
            uuid5(
                NAMESPACE_URL,
                json.dumps(
                    [
                        "traffic-log-upload-v1",
                        context.identity.device_id,
                        x_batch_id,
                    ]
                ),
            )
        )
        if x_batch_id
        else str(uuid4())
    )
    now = datetime.now(UTC)
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    agent = hashlib.sha256(context.identity.device_id.encode()).hexdigest()
    tenant = hashlib.sha256(settings.spool_tenant.encode()).hexdigest()
    descriptor = UploadDescriptor(
        upload_id=upload_id,
        object_key=f"raw/tenantId={tenant}/date={now.date()}/agentId={agent}/{upload_id}.upload",
        device_id=context.identity.device_id,
        client_id=context.identity.client_id,
        source_ip=context.source_ip,
        received_at=now,
        encrypted=encrypted,
        sha256=hashlib.sha256(wire).hexdigest(),
        wire_bytes=len(wire),
        policy=policy,
    )
    record, created = await store.reserve(descriptor)
    saved = UploadDescriptor.model_validate(record["descriptor"])
    if saved.sha256 != descriptor.sha256 or saved.encrypted != encrypted:
        raise HTTPException(status_code=409, detail="X-Batch-Id already used for different content")
    if record["status"] == "uploading":
        await cloud.put(saved, bytes(wire))
        await store.received(upload_id)
        record = await store.get(upload_id)
        if record is None:
            raise HTTPException(status_code=503, detail="Upload receipt is unavailable")
    if not created:
        response.status_code = 200
    logger.info(
        "metric=%s value=1", "ingest.batch.accepted" if created else "ingest.batch.duplicate"
    )
    return UploadReceipt(upload_id=upload_id, status=record["status"], duplicate=not created)


@router.get(
    "/{upload_id}",
    response_model=UploadStatus,
    summary="Read deferred validation and persistence status",
    responses={404: {}, 503: {}},
)
async def get_upload(
    upload_id: str,
    claims: AuthenticatedClaims,
    store: StoreDep,
) -> UploadStatus:
    """Return operational progress without exposing raw content or error details.

    **Required permission:** None beyond authentication.

    Agents see only their device's uploads; manage:devices permits fleet status reads.
    accepted_count is known only after worker #1 validates the entire batch.
    """
    device = authorized_device_id(claims)
    record = await store.get(upload_id)
    if record is None or (device and record["descriptor"]["device_id"] != device):
        raise HTTPException(status_code=404, detail="Upload not found")
    return UploadStatus(
        upload_id=upload_id,
        status=record["status"],
        accepted_count=record.get("accepted_count"),
        error_code=record.get("error_code"),
        raw_deleted=record.get("raw_deleted", False),
    )
