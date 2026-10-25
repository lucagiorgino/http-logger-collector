import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI required")
@pytest.mark.parametrize(
    ("profile", "services"),
    [
        ("dev", {"mongo", "openbao", "proxy", "pubsub", "spool-bridge", "minio"}),
        ("spool", {"pubsub", "spool-bridge", "minio"}),
        ("proxy", {"proxy"}),
        ("proxy-host", {"proxy-host"}),
        ("tools", {"pubsub", "spool-bridge", "minio", "minio-init"}),
    ],
)
def test_compose_profiles_select_valid_independent_service_groups(profile, services):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(ROOT / ".env.example"),
            "-f",
            str(ROOT / "docker/docker-compose.yml"),
            "--profile",
            profile,
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env={**os.environ, "COMPOSE_PROFILES": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    assert set(config["services"]) == services
    if profile == "proxy-host":
        proxy = config["services"]["proxy-host"]
        assert proxy["network_mode"] == "host"
        assert not proxy.get("ports")
        assert proxy["environment"]["PROXY_UPSTREAM_HOST"] == "127.0.0.1"


@pytest.mark.skipif(shutil.which("make") is None, reason="Make required")
def test_make_selects_host_proxy_and_stops_all_profiles():
    result = subprocess.run(
        [
            "make",
            "-n",
            "dev-up",
            "dev-down",
            "PROXY_SERVICE=proxy-host",
            "ENV_FILE=.env.example",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    commands = [line for line in result.stdout.splitlines() if line.startswith("docker compose")]
    assert commands
    assert all(line.count("-f docker/docker-compose.yml") == 1 for line in commands)
    assert any('up -d --wait "proxy-host"' in line for line in commands)
    assert any(line.endswith("stop proxy") for line in commands)
    assert any('--profile "*" down' in line for line in commands)
    assert any("--profile spool run --no-deps --build --rm minio-init" in line for line in commands)
