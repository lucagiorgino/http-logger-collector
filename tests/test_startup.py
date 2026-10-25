import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from app import __main__ as startup
from app import main as api
from app.core.config import _env_bool, settings


@pytest.mark.parametrize("reload", [True, False])
def test_python_startup_uses_configured_bind_and_reload(monkeypatch, reload):
    monkeypatch.setattr(settings, "app_host", "127.0.0.2")
    monkeypatch.setattr(settings, "app_port", 8901)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "app_reload", reload)
    run = Mock()
    monkeypatch.setattr(startup.uvicorn, "run", run)
    startup.main()
    run.assert_called_once_with(
        "app.main:app",
        host="127.0.0.2",
        port=8901,
        reload=reload,
        reload_dirs=["app"] if reload else None,
    )


def test_production_process_rejects_reload(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "app_reload", True)
    with pytest.raises(ValueError, match="restricted to development"):
        startup.main()


@pytest.mark.parametrize("value", ["yes", "1", "invalid"])
def test_invalid_boolean_settings_fail_explicitly(monkeypatch, value):
    monkeypatch.setenv("APP_RELOAD", value)
    with pytest.raises(ValueError, match="APP_RELOAD"):
        _env_bool("APP_RELOAD")


@pytest.mark.parametrize("port_override", [None, "8902"])
@pytest.mark.parametrize("mongo_port_override", [None, "29018"])
def test_dotenv_configures_python_and_interpolates_mongo_uri(
    tmp_path, port_override, mongo_port_override
):
    env_file = tmp_path / "development.env"
    env_file.write_text(
        "MONGO_HOST=localhost\nMONGO_PORT=29017\nMONGO_REPLICA_SET=test-rs\n"
        "MONGO_URI=mongodb://${MONGO_HOST}:${MONGO_PORT}/?replicaSet=${MONGO_REPLICA_SET}\n"
        "APP_HOST=127.0.0.2\nAPP_PORT=8901\n"
    )
    env = os.environ.copy()
    for key in (
        "MONGO_HOST",
        "MONGO_PORT",
        "MONGO_REPLICA_SET",
        "MONGO_URI",
        "APP_HOST",
        "APP_PORT",
    ):
        env.pop(key, None)
    env["ENV_FILE"] = str(env_file)
    if port_override:
        env["APP_PORT"] = port_override
    if mongo_port_override:
        env["MONGO_PORT"] = mongo_port_override
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from app.core.config import settings; "
                "print(json.dumps([settings.mongo_uri, settings.app_host, settings.app_port]))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        f"mongodb://localhost:{mongo_port_override or '29017'}/?replicaSet=test-rs",
        "127.0.0.2",
        int(port_override or "8901"),
    ]


def test_explicit_missing_environment_file_fails(tmp_path):
    env = {**os.environ, "ENV_FILE": str(tmp_path / "missing.env")}
    result = subprocess.run(
        [sys.executable, "-c", "import app.core.config"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ENV_FILE does not exist" in result.stderr


@pytest.fixture
def valid_spool_settings(monkeypatch):
    for name in (
        "bucket",
        "subscription",
        "parse_dlq_topic",
        "load_dlq_topic",
        "event_topic",
        "silver_topic",
        "silver_subscription",
        "tenant",
    ):
        monkeypatch.setattr(settings, f"spool_{name}", f"test-{name}")
    monkeypatch.setattr(settings, "spool_storage_backend", "gcs")
    monkeypatch.setattr(settings, "spool_endpoint_url", None)
    monkeypatch.setattr(settings, "spool_lease_seconds", 180)
    monkeypatch.setattr(settings, "spool_processing_seconds", 60)
    monkeypatch.setattr(settings, "spool_max_attempts", 3)
    monkeypatch.setattr(settings, "spool_mongo_documents_per_second", 1000)
    monkeypatch.setattr(settings, "worker_retry_seconds", 1)
    monkeypatch.delenv("PUBSUB_EMULATOR_HOST", raising=False)
    settings.validate_spool()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("spool_max_attempts", 0),
        ("spool_mongo_documents_per_second", 0),
        ("spool_processing_seconds", 0),
        ("worker_retry_seconds", float("nan")),
    ],
)
def test_invalid_spool_limits_fail_before_accepting_uploads(
    monkeypatch, valid_spool_settings, name, value
):
    monkeypatch.setattr(settings, name, value)
    with pytest.raises(ValueError, match=name.upper()):
        settings.validate_spool()


@pytest.mark.parametrize(
    "name", ["event_topic", "silver_topic", "parse_dlq_topic", "load_dlq_topic"]
)
def test_all_pipeline_topics_are_required(monkeypatch, valid_spool_settings, name):
    monkeypatch.setattr(settings, f"spool_{name}", "")
    with pytest.raises(ValueError, match=f"SPOOL_{name.upper()} is required"):
        settings.validate_spool()


def test_collector_startup_initializes_raw_receipts_and_cloud_only(
    monkeypatch, valid_spool_settings
):
    repository, cloud, store, kms = (AsyncMock() for _ in range(4))
    kms.__aenter__.return_value = kms
    storage = Mock()
    monkeypatch.setattr(api, "MongoRepository", Mock(return_value=repository))
    monkeypatch.setattr(api, "UploadCloud", Mock(return_value=cloud))
    store_factory = Mock(return_value=store)
    monkeypatch.setattr(api, "UploadStateStore", store_factory)
    monkeypatch.setattr(api, "AsyncOpenBaoClient", Mock(return_value=kms))
    monkeypatch.setattr(api, "StorageEncryption", Mock(return_value=storage))
    monkeypatch.setattr(api, "get_encryption_keys", Mock())
    application = FastAPI()

    async def scenario():
        async with api.lifespan(application):
            assert application.state.mongo_repository is repository
            assert application.state.upload_store is store
            assert application.state.upload_cloud is cloud
            repository.ensure_indexes.assert_awaited_once()
            store.initialize.assert_awaited_once()
            cloud.close.assert_not_awaited()
        cloud.close.assert_awaited_once()
        repository.close.assert_awaited_once()

    asyncio.run(scenario())
    store_factory.assert_called_once_with(repository.database)
