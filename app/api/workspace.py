from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.api.devices import require_device_management
from app.core.auth import AuthenticatedClaims
from app.deps import MongoRepositoryDep, StorageEncryptionDep
from app.schemas.workspace import WorkspaceConfiguration, WorkspacePatch

router = APIRouter(prefix="/api/v1/workspace/configuration", tags=["workspace"])


@router.get("", response_model=WorkspaceConfiguration, responses={401: {}, 403: {}, 503: {}})
async def get_configuration(
    claims: AuthenticatedClaims, repository: MongoRepositoryDep
) -> WorkspaceConfiguration:
    require_device_management(claims)
    return WorkspaceConfiguration.model_validate(await repository.get_workspace_configuration())


@router.patch(
    "",
    response_model=WorkspaceConfiguration,
    responses={401: {}, 403: {}, 409: {}, 422: {}, 503: {}},
)
async def patch_configuration(
    payload: WorkspacePatch,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    storage: StorageEncryptionDep,
) -> WorkspaceConfiguration:
    require_device_management(claims)
    if any(
        (
            payload.encrypt_request_body,
            payload.encrypt_authorization_header,
            payload.encrypt_cookies,
        )
    ):
        await run_in_threadpool(storage.validate)
    document = await repository.update_workspace_configuration(
        payload.expected_version,
        payload.model_dump(exclude={"expected_version"}, exclude_unset=True),
    )
    if document is None:
        raise HTTPException(status_code=409, detail="Workspace configuration version has changed")
    return WorkspaceConfiguration.model_validate(document)
