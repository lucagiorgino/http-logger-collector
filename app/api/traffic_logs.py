from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import AwareDatetime

from app.core.auth import AuthenticatedClaims
from app.deps import MongoRepositoryDep
from app.schemas.traffic import StoredTrafficLog, TrafficLogPage
from app.services.analysis import decode_cursor, encode_cursor, stored_log
from app.services.traffic_service import authorized_device_id

router = APIRouter(prefix="/api/v1/traffic-logs", tags=["traffic logs"])


@router.get(
    "",
    response_model=TrafficLogPage,
    responses={401: {}, 403: {}, 422: {}, 503: {}},
)
async def list_traffic_logs(
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    device_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    start_time: AwareDatetime | None = None,
    end_time: AwareDatetime | None = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TrafficLogPage:
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise HTTPException(status_code=422, detail="start_time must be before end_time")
    documents = await repository.list_traffic_logs(
        device_id=authorized_device_id(claims, device_id),
        start_time=start_time,
        end_time=end_time,
        before=decode_cursor(cursor),
        limit=limit + 1,
    )
    page = documents[:limit]
    return TrafficLogPage(
        items=[stored_log(document) for document in page],
        next_cursor=encode_cursor(page[-1]) if len(documents) > limit else None,
    )


@router.get("/{record_id}", response_model=StoredTrafficLog, responses={401: {}, 404: {}, 503: {}})
async def get_traffic_log(
    record_id: Annotated[str, Path(pattern=r"^[0-9a-f]{24}$")],
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
) -> StoredTrafficLog:
    document = await repository.get_traffic_log(
        ObjectId(record_id), device_id=authorized_device_id(claims)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Traffic log not found")
    return stored_log(document)
