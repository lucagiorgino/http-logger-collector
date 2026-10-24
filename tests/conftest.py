from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth import require_bearer_token
from app.deps import get_mongo_repository
from app.main import app


@pytest.fixture
def repository():
    repository = AsyncMock()
    repository.list_traffic_logs.return_value = []
    repository.get_traffic_log.return_value = None
    return repository


@pytest.fixture
def authorized_client(repository):
    app.dependency_overrides[require_bearer_token] = lambda: {"sub": "user-123"}
    app.dependency_overrides[get_mongo_repository] = lambda: repository
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
