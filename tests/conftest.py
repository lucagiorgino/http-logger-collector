from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import ECKey

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.core.encryption import EncryptionKeys, get_encryption_keys
from app.deps import get_mongo_repository
from app.main import app


@pytest.fixture
def repository():
    repository = AsyncMock()
    repository.list_traffic_logs.return_value = []
    repository.get_traffic_log.return_value = None
    repository.get_device_configuration.return_value = {"version": 0, "encryption_mode": "tls_only"}
    return repository


@pytest.fixture
def authorized_client(repository):
    app.dependency_overrides[require_bearer_token] = lambda: {"sub": "user-123"}
    app.dependency_overrides[get_mongo_repository] = lambda: repository
    app.dependency_overrides[get_encryption_keys] = lambda: EncryptionKeys({})
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def traffic_payload():
    return {
        "user_id": "user-123",
        "device_id": "device-456",
        "timestamp": "2026-01-15T10:00:00+02:00",
        "method": "get",
        "url": "https://example.com/path",
        "status_code": 200,
        "headers": {"Authorization": "secret", "Cookie": "session=secret"},
        "body": "sensitive request content",
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
