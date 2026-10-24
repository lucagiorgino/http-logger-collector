import zlib
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.auth import AuthenticatedClaims
from app.core.encryption import MAX_ENCRYPTED_BYTES, InvalidEnvelope
from app.deps import EncryptionKeysDep, IngestionPublisherDep, MongoRepositoryDep
from app.schemas.traffic import BatchReceipt, TrafficLogBatch, TrafficLogRequest
from app.services.traffic_service import authenticated_subject, ingest_traffic_logs

router = APIRouter(prefix="/api/v1/traffic-log-batches", tags=["traffic logs"])
MAX_COMPRESSED_BYTES = 1_048_576
MAX_DECOMPRESSED_BYTES = 8_388_608


async def read_compressed_batch(
    request: Request,
    keys: EncryptionKeysDep,
    content_encoding: Annotated[
        str | None, Header(description="gzip for JSON; omit for application/jose")
    ] = None,
) -> TrafficLogBatch:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content_encoding = (content_encoding or "").lower().strip()
    encrypted = media_type == "application/jose"
    if encrypted:
        if content_encoding:
            raise HTTPException(status_code=415, detail="JWE must not have Content-Encoding")
        if not keys.active_key_id:
            raise HTTPException(status_code=503, detail="Application encryption is unavailable")
    elif media_type != "application/json" or content_encoding != "gzip":
        raise HTTPException(
            status_code=415, detail="Expected gzip JSON or an application/jose encrypted batch"
        )

    wire = bytearray()
    limit = MAX_ENCRYPTED_BYTES if encrypted else MAX_COMPRESSED_BYTES
    async for chunk in request.stream():
        if len(wire) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Batch exceeds wire size limit")
        wire.extend(chunk)
    compressed = bytes(wire)
    if encrypted:
        try:
            compressed = await run_in_threadpool(keys.decrypt, compressed)
        except InvalidEnvelope as exc:
            raise HTTPException(status_code=400, detail="Invalid encrypted batch") from exc
    request.state.batch_encrypted = encrypted
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise HTTPException(status_code=413, detail="Compressed batch exceeds 1 MiB")
    decompressor = zlib.decompressobj(wbits=31)
    try:
        body = decompressor.decompress(compressed, MAX_DECOMPRESSED_BYTES + 1)
    except zlib.error as exc:
        raise HTTPException(status_code=400, detail="Invalid gzip batch") from exc
    if len(body) > MAX_DECOMPRESSED_BYTES:
        raise HTTPException(status_code=413, detail="Decompressed batch exceeds 8 MiB")
    if decompressor.unused_data:
        raise HTTPException(status_code=400, detail="Expected exactly one gzip member")
    if not decompressor.eof:
        raise HTTPException(status_code=400, detail="Incomplete gzip batch")
    try:
        return TrafficLogBatch.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            exc.errors(include_input=False, include_context=False)
        ) from exc


@router.post(
    "",
    status_code=202,
    response_model=BatchReceipt,
    summary="Accept a gzip JSON or JWE-encrypted batch of traffic logs",
    responses={400: {}, 401: {}, 403: {}, 413: {}, 415: {}, 422: {}, 503: {}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "JSON: Content-Encoding gzip. JWE: configured ECDH/AES suite, "
                "protected kid, public epk and cty=application/gzip; no Content-Encoding. "
                "At most 1 MiB gzip and 8 MiB expanded; JWE wire limit 1,500,000 bytes."
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
                            },
                            "application/jose": {"schema": {"type": "string"}},
                        },
                    }
                }
            },
        }
    },
)
async def create_traffic_log_batch(
    request: Request,
    claims: AuthenticatedClaims,
    publisher: IngestionPublisherDep,
    repository: MongoRepositoryDep,
    payload: Annotated[TrafficLogBatch, Depends(read_compressed_batch)],
) -> BatchReceipt:
    subject = authenticated_subject(claims)
    if any(log.user_id != subject for log in payload.logs):
        raise HTTPException(
            status_code=403, detail="Authenticated subject cannot submit these logs"
        )
    for device_id in {log.device_id for log in payload.logs}:
        configuration = await repository.get_device_configuration(subject, device_id)
        mode = configuration["encryption_mode"]
        if mode == "required" and not request.state.batch_encrypted:
            raise HTTPException(status_code=403, detail="Device requires application encryption")
        if mode == "tls_only" and request.state.batch_encrypted:
            raise HTTPException(status_code=403, detail="Application encryption is not enabled")
    return await ingest_traffic_logs(payload.logs, subject, publisher)
