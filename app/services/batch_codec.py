import json
import zlib
from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.schemas.traffic import TrafficLogBatch

MAX_COMPRESSED_BYTES = 1_048_576
MAX_DECOMPRESSED_BYTES = 8_388_608


def unique_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field")
        result[name] = value
    return result


def parse_compressed_batch(
    compressed: bytes,
    *,
    max_compressed_bytes: int = MAX_COMPRESSED_BYTES,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES,
) -> TrafficLogBatch:
    if len(compressed) > max_compressed_bytes:
        raise HTTPException(status_code=413, detail="Compressed batch exceeds 1 MiB")
    decompressor = zlib.decompressobj(wbits=31)
    try:
        body = decompressor.decompress(compressed, max_decompressed_bytes + 1)
    except zlib.error as exc:
        raise HTTPException(status_code=400, detail="Invalid gzip batch") from exc
    if len(body) > max_decompressed_bytes:
        raise HTTPException(status_code=413, detail="Decompressed batch exceeds 8 MiB")
    if decompressor.unused_data:
        raise HTTPException(status_code=400, detail="Expected exactly one gzip member")
    if not decompressor.eof:
        raise HTTPException(status_code=400, detail="Incomplete gzip batch")
    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=unique_json_fields)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=422, detail="Invalid JSON batch") from None
    try:
        return TrafficLogBatch.model_validate(decoded)
    except ValidationError as exc:
        raise RequestValidationError(
            exc.errors(include_input=False, include_context=False)
        ) from exc
