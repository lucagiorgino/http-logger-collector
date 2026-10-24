import zlib
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.auth import AuthenticatedClaims
from app.deps import IngestionPublisherDep
from app.schemas.traffic import BatchReceipt, TrafficLogBatch, TrafficLogRequest
from app.services.traffic_service import authenticated_subject, ingest_traffic_logs

router = APIRouter(prefix="/api/v1/traffic-log-batches", tags=["traffic logs"])
MAX_COMPRESSED_BYTES = 1_048_576
MAX_DECOMPRESSED_BYTES = 8_388_608


async def read_compressed_batch(
    request: Request,
    content_encoding: Annotated[str | None, Header(description="gzip-compressed JSON")] = None,
) -> TrafficLogBatch:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content_encoding = (content_encoding or "").lower().strip()
    if media_type != "application/json" or content_encoding != "gzip":
        raise HTTPException(status_code=415, detail="Expected gzip-compressed JSON")
    compressed = bytearray()
    async for chunk in request.stream():
        if len(compressed) + len(chunk) > MAX_COMPRESSED_BYTES:
            raise HTTPException(status_code=413, detail="Compressed batch exceeds 1 MiB")
        compressed.extend(chunk)
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
    summary="Accept a gzip-compressed batch of traffic logs",
    responses={400: {}, 401: {}, 403: {}, 413: {}, 415: {}, 422: {}, 503: {}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "One gzip-compressed JSON batch, at most 1 MiB gzip and 8 MiB expanded."
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
                        },
                    }
                }
            },
        }
    },
)
async def create_traffic_log_batch(
    claims: AuthenticatedClaims,
    publisher: IngestionPublisherDep,
    payload: Annotated[TrafficLogBatch, Depends(read_compressed_batch)],
) -> BatchReceipt:
    subject = authenticated_subject(claims)
    if any(log.user_id != subject for log in payload.logs):
        raise HTTPException(
            status_code=403, detail="Authenticated subject cannot submit these logs"
        )
    return await ingest_traffic_logs(payload.logs, subject, publisher)
