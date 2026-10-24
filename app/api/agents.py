from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import require_bearer_token
from app.core.security import redact_sensitive_headers
from app.db.mongo import PersistenceError
from app.deps import MongoRepositoryDep
from app.schemas.traffic import TrafficLogRequest, TrafficLogResponse
from app.services.traffic_service import persist_traffic_log

router = APIRouter(prefix="/agent", tags=["private"])
AuthenticatedClaims = Annotated[dict, Depends(require_bearer_token)]


@router.post("/traffic_logs", response_model=TrafficLogResponse)
async def receive_traffic_log(
    payload: TrafficLogRequest,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
) -> TrafficLogResponse:
    redacted_headers = redact_sensitive_headers(payload.headers)
    try:
        record = await persist_traffic_log(
            payload,
            redacted_headers,
            authenticated_subject=claims.get("sub"),
            repository=repository,
        )
    except PersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Traffic log storage is temporarily unavailable",
        ) from exc
    return TrafficLogResponse(
        accepted=record["accepted"],
        record_id=record["record_id"],
        status="stored",
    )
