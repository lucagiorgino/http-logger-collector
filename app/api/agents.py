from fastapi import APIRouter, Depends

from app.core.auth import require_bearer_token
from app.core.security import redact_sensitive_headers
from app.schemas.traffic import TrafficLogRequest, TrafficLogResponse
from app.services.traffic_service import persist_traffic_log

router = APIRouter(tags=["private"])


@router.post("/agent/traffic_logs", response_model=TrafficLogResponse, tags=["private"])
async def receive_traffic_log(
    payload: TrafficLogRequest,
    claims: dict = Depends(require_bearer_token),
) -> TrafficLogResponse:
    del claims
    redacted_headers = redact_sensitive_headers(payload.headers)
    record = persist_traffic_log(payload, redacted_headers)
    return TrafficLogResponse(
        accepted=record["accepted"],
        record_id=record["record_id"],
        status="stored",
    )
