import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_collector_command_does_not_start_containers_or_workers():
    result = subprocess.run(
        ["make", "--dry-run", "local-collector-run", "ENV_FILE=.env.example"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    assert "-m app" in result.stdout
    assert "docker" not in result.stdout
    assert "spool_worker" not in result.stdout


@pytest.fixture(scope="module")
def compose():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required for local configuration checks")
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / ".env.example"),
        "-f",
        str(ROOT / "docker/docker-compose.yml"),
    ]
    if subprocess.run([*command, "version"], capture_output=True, check=False).returncode:
        pytest.skip("Docker Compose plugin is required for local configuration checks")

    def render(*profiles):
        result = subprocess.run(
            [
                *command,
                *(argument for profile in profiles for argument in ("--profile", profile)),
                "config",
                "--no-env-resolution",
                "--format",
                "json",
            ],
            env={**os.environ, "ENV_FILE": str(ROOT / ".env.example")},
            capture_output=True,
            check=True,
            text=True,
        )
        return json.loads(result.stdout)["services"]

    return render


def test_full_profile_contains_pipeline_and_frontend_but_not_maintenance(compose):
    services = compose("full")
    assert {"collector", "parser", "loader", "frontend", "proxy-containers"} <= services.keys()
    assert not {"proxy", "proxy-host", "cleaner"} & services.keys()
    assert services["proxy-containers"]["environment"]["PROXY_BACKEND_HOST"] == "collector"
    assert services["proxy-containers"]["environment"]["PROXY_FRONTEND_HOST"] == "frontend"
    for stage in ("collector", "parser", "loader"):
        assert services[stage]["depends_on"]["pubsub-init"]["condition"] == (
            "service_completed_successfully"
        )
        assert services[stage]["depends_on"]["minio-init"]["condition"] == (
            "service_completed_successfully"
        )
    for name in ("loader", "cleaner", "pubsub-init"):
        if name in services:
            assert "openbao-token" not in {secret["source"] for secret in services[name]["secrets"]}
    assert "openbao-dev-root-token" not in {
        secret["source"] for secret in services["collector"]["secrets"]
    }
    for service in services.values():
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1"


def test_worker_profile_does_not_start_collector_or_frontend(compose):
    services = compose("infra", "workers")
    assert {"parser", "loader", "mongo", "minio", "pubsub"} <= services.keys()
    assert not {"collector", "frontend", "proxy-containers", "cleaner"} & services.keys()


def test_cleaner_is_an_explicit_one_shot_with_required_receipt(compose):
    services = compose("infra", "maintenance")
    cleaner = services["cleaner"]
    assert cleaner["restart"] == "no"
    assert 'test -n "$$UPLOAD_ID"' in cleaner["command"][-1] or (
        'test -n "$UPLOAD_ID"' in cleaner["command"][-1]
    )
    assert not {"collector", "parser", "loader", "frontend"} & services.keys()
