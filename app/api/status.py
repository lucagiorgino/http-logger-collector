from fastapi import APIRouter

from app.api.devices import require_device_management
from app.api.uploads import CloudDep
from app.core.auth import AuthenticatedClaims
from app.deps import MongoRepositoryDep
from app.schemas.status import PipelineStatus
from app.services.pipeline_status import pipeline_status

router = APIRouter(prefix="/api/v1/status", tags=["status"])


@router.get(
    "",
    response_model=PipelineStatus,
    summary="Inspect ingestion pipeline health",
    responses={401: {}, 403: {}},
)
async def get_pipeline_status(
    claims: AuthenticatedClaims, repository: MongoRepositoryDep, cloud: CloudDep
) -> PipelineStatus:
    """Read bounded metadata probes, consumer heartbeats and operational receipt counts.

    **Required permission:** `manage:devices`.

    Dependency failures are reported per node, not hidden behind an all-green response.
    No payloads, resource names, credentials or consumer messages are returned.
    """
    require_device_management(claims)
    return await pipeline_status(repository, cloud)
