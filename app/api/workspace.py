from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.api.devices import require_device_management
from app.core.auth import AuthenticatedClaims
from app.deps import MongoRepositoryDep, StorageEncryptionDep
from app.schemas.workspace import WorkspaceConfiguration, WorkspacePatch

router = APIRouter(prefix="/api/v1/workspace/configuration", tags=["workspace"])


@router.get(
    "",
    response_model=WorkspaceConfiguration,
    summary="Read workspace storage and retention policies",
    responses={401: {}, 403: {}, 503: {}},
)
async def get_configuration(
    claims: AuthenticatedClaims, repository: MongoRepositoryDep
) -> WorkspaceConfiguration:
    """Read shared storage/discard choices, field encryption and log-retention windows.

    **Required permission:** `manage:devices`.

    Defaults retain sensitive categories encrypted, expire ordinary logs after 7 days
    and suspicious logs after 30 days. Disabling a retention window keeps that category
    indefinitely. The returned version is required for updates.
    """
    require_device_management(claims)
    return WorkspaceConfiguration.model_validate(await repository.get_workspace_configuration())


@router.patch(
    "",
    response_model=WorkspaceConfiguration,
    summary="Update workspace storage and retention policies",
    responses={401: {}, 403: {}, 409: {}, 422: {}, 503: {}},
)
async def patch_configuration(
    payload: WorkspacePatch,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    storage: StorageEncryptionDep,
) -> WorkspaceConfiguration:
    """Save versioned policies shared by all devices.

    **Required permission:** `manage:devices`.

    Send the current `expected_version` and all three `encrypt_*` flags. Other omitted
    settings keep their saved values. A false `retain_*` discards that category before
    storage; discarding a body preserves its byte length and SHA-256 digest.
    Storage changes affect new uploads only. Retention changes also affect existing
    logs through asynchronous, event-time MongoDB TTL deletion.

    A stale version returns 409. A TTL-index failure returns 503 even if desired
    settings were saved; read the latest version and retry to reconcile indexes.
    """
    require_device_management(claims)
    changes = payload.model_dump(exclude={"expected_version"}, exclude_unset=True)
    policy = WorkspaceConfiguration.model_validate(
        {**await repository.get_workspace_configuration(), **changes}
    )
    if any(
        (
            policy.retain_request_body and policy.encrypt_request_body,
            policy.retain_authorization_header and policy.encrypt_authorization_header,
            policy.retain_cookies and policy.encrypt_cookies,
        )
    ):
        await run_in_threadpool(storage.validate)
    document = await repository.update_workspace_configuration(payload.expected_version, changes)
    if document is None:
        raise HTTPException(status_code=409, detail="Workspace configuration version has changed")
    return WorkspaceConfiguration.model_validate(document)
