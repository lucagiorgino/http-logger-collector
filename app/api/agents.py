from fastapi import APIRouter, Header, HTTPException

from app.core.security import redact_sensitive_headers
from app.schemas.traffic import TrafficLogRequest, TrafficLogResponse
from app.services.traffic_service import persist_traffic_log

router = APIRouter(tags=["agents"])


@router.post("/agent/traffic_logs", response_model=TrafficLogResponse)
async def receive_traffic_log(
    payload: TrafficLogRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> TrafficLogResponse:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")

    redacted_headers = redact_sensitive_headers(payload.headers)
    record = persist_traffic_log(payload, redacted_headers)
    return TrafficLogResponse(
        accepted=record["accepted"],
        record_id=record["record_id"],
        status="stored",
    )
