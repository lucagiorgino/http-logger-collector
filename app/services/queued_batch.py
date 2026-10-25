from dataclasses import dataclass
from typing import Any

from bson import BSON, ObjectId
from bson.codec_options import CodecOptions
from bson.errors import InvalidBSON


@dataclass(frozen=True)
class QueuedBatch:
    message_id: bytes
    batch_id: str
    documents: list[dict[str, Any]]


class InvalidQueuedBatch(ValueError):
    pass


def decode_batch(message_id: bytes, fields: dict[bytes, bytes]) -> QueuedBatch:
    try:
        envelope = BSON(fields[b"payload"]).decode(codec_options=CodecOptions(tz_aware=True))
    except (KeyError, InvalidBSON) as exc:
        raise InvalidQueuedBatch("Invalid queued BSON payload") from exc
    documents = envelope.get("documents")
    batch_id = envelope.get("batch_id")
    version = envelope.get("version")
    if (
        version != 2
        or not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(documents, list)
        or not 1 <= len(documents) <= 500
        or any(
            not isinstance(document, dict)
            or not isinstance(document.get("_id"), ObjectId)
            or not isinstance(document.get("ingestion"), dict)
            or document["ingestion"].get("batch_id") != batch_id
            or not isinstance(document.get("logdata"), dict)
            for document in documents
        )
    ):
        raise InvalidQueuedBatch("Invalid queued batch envelope")
    return QueuedBatch(message_id, batch_id, documents)
