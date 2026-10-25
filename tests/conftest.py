from unittest.mock import AsyncMock

import pytest
from fake_storage import FakeStorageEncryption
from fastapi.testclient import TestClient
from joserfc.jwk import ECKey

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.core.encryption import EncryptionKeys, get_encryption_keys
from app.core.storage_encryption import get_storage_encryption
from app.deps import get_mongo_repository
from app.main import app
from app.schemas.workspace import WorkspaceConfiguration
from app.services.traffic_service import DEVICE_ID_CLAIM, ClientIdentity, IngestionContext


@pytest.fixture
def storage_encryption(monkeypatch):
    monkeypatch.setattr(settings, "openbao_addr", "https://test-openbao.invalid")
    return FakeStorageEncryption()


@pytest.fixture
def repository():
    repository = AsyncMock()
    repository.list_traffic_logs.return_value = []
    repository.get_traffic_log.return_value = None
    repository.get_device_configuration.return_value = {"version": 0, "encryption_mode": "tls_only"}
    repository.list_devices.return_value = []
    repository.get_workspace_configuration.return_value = WorkspaceConfiguration().model_dump()
    return repository


@pytest.fixture
def authorized_client(repository, storage_encryption):
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "user-123",
        DEVICE_ID_CLAIM: "device-456",
        "azp": "oauth-client-123",
    }
    app.dependency_overrides[get_mongo_repository] = lambda: repository
    app.dependency_overrides[get_encryption_keys] = lambda: EncryptionKeys({})
    app.dependency_overrides[get_storage_encryption] = lambda: storage_encryption
    try:
        yield TestClient(app, client=("203.0.113.10", 50000))
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def trusted_context():
    return IngestionContext(
        identity=ClientIdentity("device-456", "oauth-client-123"),
        source_ip="203.0.113.10",
    )


@pytest.fixture
def traffic_payload():
    return {
        "timestamp": "2026-01-15T10:00:00+02:00",
        "method": "get",
        "url": "https://example.com/path",
        "status_code": 200,
        "headers": {"Authorization": "secret", "Cookie": "session=secret"},
        "body": "sensitive request content",
    }


@pytest.fixture
def rich_traffic_payload(traffic_payload):
    return {
        **{key: value for key, value in traffic_payload.items() if key != "headers"},
        "request_headers": {
            "Authorization": "private-credential",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        },
        "response_headers": {"Content-Type": "application/json", "Set-Cookie": "private-cookie"},
        "captured_source_ip": "192.0.2.10",
        "destination_ip": "2001:db8::50",
        "source_port": 49152,
        "destination_port": 443,
        "http_version": "2",
        "request_size_bytes": 256,
        "response_size_bytes": 1024,
        "duration_ms": 12.5,
        "process_name": "chrome.exe",
        "process_id": 1234,
        "request_id": "agent-request-123",
    }


@pytest.fixture(scope="session")
def encryption_keys():
    keys = {
        kid: ECKey.generate_key(
            settings.encryption_curve,
            parameters={"kid": kid, "use": "enc", "alg": settings.encryption_key_algorithm},
        )
        for kid in ("current", "previous")
    }
    return EncryptionKeys(keys, "current")


@pytest.fixture
def encrypted_client(authorized_client, encryption_keys):
    app.dependency_overrides[get_encryption_keys] = lambda: encryption_keys
    return authorized_client
