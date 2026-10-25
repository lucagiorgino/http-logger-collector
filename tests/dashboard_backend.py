"""Real API/MongoDB fixture for the opt-in dashboard registration browser test."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fake_storage import FakeStorageEncryption
from fastapi import Header, HTTPException
from joserfc.jwk import ECKey

from app.core.auth import require_bearer_token
from app.core.config import settings
from app.core.encryption import EncryptionKeys
from app.core.storage_encryption import get_storage_encryption
from app.db.mongo import MongoRepository
from app.deps import get_request_encryption_keys
from app.main import app
from app.services.ingestion import MongoIngestionPublisher

mongo_uri = os.environ["MONGO_TEST_URI"]
database_name = "dashboard_registration_test_" + uuid4().hex
settings.mongo_allow_primary_reads = True
settings.app_env = "development"
settings.require_https = False
key = ECKey.generate_key(
    settings.encryption_curve,
    parameters={"kid": "browser-test-key", "use": "enc", "alg": settings.encryption_key_algorithm},
)
keys = EncryptionKeys({"browser-test-key": key}, "browser-test-key")
storage = FakeStorageEncryption()


def test_identity(authorization: str | None = Header(default=None)):
    if authorization != "Bearer test-server-access-token":
        raise HTTPException(status_code=401, detail="Test session required")
    return {"sub": "operator-test", "permissions": ["manage:devices"]}


@asynccontextmanager
async def lifespan(application):
    repository = MongoRepository(uri=mongo_uri, db_name=database_name)
    application.state.mongo_repository = repository
    application.state.ingestion_queue = MongoIngestionPublisher(repository)
    try:
        await repository.ensure_indexes()
        for device_id in ("agent-discovered@clients", "literal%40device@clients"):
            await repository.report_device_configuration(
                device_id,
                {
                    "version": 0,
                    "status": "applied",
                    "supports_encryption": False,
                    "key_id": None,
                    "detail": None,
                    "reported_at": datetime.now(UTC),
                },
            )
        yield
    finally:
        await repository.client.drop_database(database_name)
        await repository.close()


app.router.lifespan_context = lifespan
app.dependency_overrides[require_bearer_token] = test_identity
app.dependency_overrides[get_request_encryption_keys] = lambda: keys
app.dependency_overrides[get_storage_encryption] = lambda: storage
