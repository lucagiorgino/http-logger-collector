import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from app import __main__ as startup
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
