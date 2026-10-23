from typing import Any

from app.schemas.traffic import TrafficLogRequest


def persist_traffic_log(
    payload: TrafficLogRequest,
    redacted_headers: dict[str, str],
) -> dict[str, Any]:
    record_id = f"{payload.device_id}:{payload.timestamp.isoformat()}"
    return {
        "accepted": True,
        "record_id": record_id,
        "redacted_headers": redacted_headers,
    }
