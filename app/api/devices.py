from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, Query, Response

from app.core.auth import AuthenticatedClaims
from app.core.encryption import EncryptionKeys
from app.db.mongo import DuplicateDeviceError
from app.deps import EncryptionKeysDep, MongoRepositoryDep
from app.schemas.devices import (
    ConfigurationPatch,
    ConfigurationStatus,
    DeviceConfiguration,
    DevicePage,
    DeviceRegistration,
    DeviceSummary,
    EncryptionParameters,
    ReportedConfigurationStatus,
)
from app.services.traffic_service import (
    authenticated_identity,
    authorized_device_id,
    can_manage_devices,
)

router = APIRouter(prefix="/api/v1/devices", tags=["device configuration"])
DeviceID = Annotated[str, Path(min_length=1, max_length=255)]


def configuration_response(
    device_id: str, document: dict, keys: EncryptionKeys
) -> DeviceConfiguration:
    encryption = None
    if document["encryption_mode"] != "tls_only":
        if keys.public_key is None:
            raise HTTPException(status_code=503, detail="Application encryption is unavailable")
        encryption = EncryptionParameters(
            algorithm=keys.algorithm,
            encryption=keys.encryption,
            curve=keys.curve,
            public_key=keys.public_key,
        )
    return DeviceConfiguration(
        device_id=device_id,
        name=document.get("name"),
        registered_at=document.get("registered_at"),
        updated_at=document.get("updated_at"),
        version=document["version"],
        encryption_mode=document["encryption_mode"],
        encryption=encryption,
        configuration_status=document.get("configuration_status"),
    )


def require_device_management(claims: dict) -> None:
    authenticated_identity(claims)
    if not can_manage_devices(claims):
        raise HTTPException(status_code=403, detail="manage:devices permission is required")


@router.post(
    "",
    status_code=201,
    response_model=DeviceConfiguration,
    responses={400: {}, 401: {}, 403: {}, 422: {}, 503: {}},
)
async def register_device(
    payload: DeviceRegistration,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    keys: EncryptionKeysDep,
    response: Response,
) -> DeviceConfiguration:
    require_device_management(claims)
    if payload.encryption_mode != "tls_only" and keys.public_key is None:
        raise HTTPException(status_code=503, detail="Application encryption is unavailable")
    try:
        document = await repository.register_device(
            payload.device_id, payload.name, payload.encryption_mode
        )
    except DuplicateDeviceError:
        raise HTTPException(
            status_code=400, detail="A device with this device_id or name already exists"
        ) from None
    response.headers["Location"] = f"/api/v1/devices/{quote(payload.device_id, safe='')}"
    return configuration_response(payload.device_id, document, keys)


@router.get("", response_model=DevicePage, responses={401: {}, 422: {}, 503: {}})
async def list_devices(
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    after: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DevicePage:
    documents = await repository.list_devices(
        device_id=authorized_device_id(claims), after=after, limit=limit + 1
    )
    page = documents[:limit]
    return DevicePage(
        items=[
            DeviceSummary(
                device_id=document["_id"],
                name=document.get("name"),
                registered_at=document.get("registered_at"),
                version=document["version"],
                encryption_mode=document["encryption_mode"],
            )
            for document in page
        ],
        next_after=page[-1]["_id"] if len(documents) > limit else None,
    )


@router.get(
    "/{device_id}",
    response_model=DeviceConfiguration,
    responses={401: {}, 403: {}, 404: {}, 503: {}},
)
async def get_device(
    device_id: DeviceID,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    keys: EncryptionKeysDep,
) -> DeviceConfiguration:
    authorized_device_id(claims, device_id)
    document = await repository.get_device_configuration(device_id)
    if "_id" not in document:
        raise HTTPException(status_code=404, detail="Device not found")
    return configuration_response(device_id, document, keys)


@router.get(
    "/{device_id}/configuration",
    response_model=DeviceConfiguration,
    responses={401: {}, 403: {}, 503: {}},
)
async def get_configuration(
    device_id: DeviceID,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    keys: EncryptionKeysDep,
) -> DeviceConfiguration:
    authorized_device_id(claims, device_id)
    document = await repository.get_device_configuration(device_id)
    return configuration_response(device_id, document, keys)


@router.patch(
    "/{device_id}/configuration",
    response_model=DeviceConfiguration,
    responses={401: {}, 403: {}, 409: {}, 503: {}},
)
async def patch_configuration(
    device_id: DeviceID,
    payload: ConfigurationPatch,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    keys: EncryptionKeysDep,
) -> DeviceConfiguration:
    require_device_management(claims)
    if payload.encryption_mode != "tls_only" and keys.public_key is None:
        raise HTTPException(status_code=503, detail="Application encryption is unavailable")
    document = await repository.update_device_configuration(
        device_id, payload.expected_version, payload.encryption_mode
    )
    if document is None:
        raise HTTPException(status_code=409, detail="Device configuration version has changed")
    return configuration_response(device_id, document, keys)


@router.put(
    "/{device_id}/configuration-status",
    response_model=ReportedConfigurationStatus,
    responses={401: {}, 403: {}, 409: {}, 503: {}},
)
async def put_configuration_status(
    device_id: DeviceID,
    payload: ConfigurationStatus,
    claims: AuthenticatedClaims,
    repository: MongoRepositoryDep,
    keys: EncryptionKeysDep,
) -> ReportedConfigurationStatus:
    if authenticated_identity(claims).device_id != device_id:
        raise HTTPException(
            status_code=403, detail="Only the authenticated device can report status"
        )
    document = await repository.get_device_configuration(device_id)
    if payload.version != document["version"]:
        raise HTTPException(status_code=409, detail="Device configuration version has changed")
    if (
        payload.status == "applied"
        and document["encryption_mode"] != "tls_only"
        and (not payload.supports_encryption or payload.key_id != keys.active_key_id)
    ):
        raise HTTPException(status_code=422, detail="Applied encryption capability/key mismatch")
    report = ReportedConfigurationStatus(**payload.model_dump(), reported_at=datetime.now(UTC))
    if not await repository.report_device_configuration(device_id, report.model_dump()):
        raise HTTPException(status_code=409, detail="Device configuration version has changed")
    return report
