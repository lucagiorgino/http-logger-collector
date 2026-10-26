import argparse
import json
import shutil
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest
from google.api_core.exceptions import PermissionDenied
from google.cloud import secretmanager

from infra.gcp import bootstrap_secrets, deploy


def arguments(**overrides):
    return argparse.Namespace(
        **{
            "gcloud": "gcloud",
            "check": False,
            "yes": True,
            "external_ready": True,
            "project_id": None,
            "region": None,
            "environment": None,
            "state_bucket": None,
            "release": "test-release",
            **overrides,
        }
    )


@pytest.fixture
def infrastructure(tmp_path, monkeypatch):
    original = deploy.INFRA
    monkeypatch.setattr(deploy, "INFRA", tmp_path)
    (tmp_path / "variables.tf").write_text((original / "variables.tf").read_text())
    return tmp_path


@pytest.fixture
def settings():
    return {
        "project_id": "example-project",
        "region": "europe-west2",
        "environment": "production",
        "deploy_services": False,
        "backend_image_tag": "release-old",
        "frontend_image_tag": "release-old",
        "secret_versions": {key: "1" for key in bootstrap_secrets.SECRET_SUFFIXES},
    }


def test_local_env_reuse_without_shell_execution_or_emulator_settings(tmp_path, monkeypatch):
    backend = tmp_path / ".env"
    frontend = tmp_path / "frontend.env"
    backend.write_text(
        "AUTH0_DOMAIN=tenant.example.com\nAUTH0_AUDIENCE=https://api.example.test/api\n"
        'MONGO_HOST=localhost\nMONGO_URI="mongodb://${MONGO_HOST}:28017"\n'
        "GOOGLE_CLOUD_PROJECT=logger-local\nPUBSUB_EMULATOR_HOST=localhost:28681\n"
    )
    frontend.write_text('AUTH0_CLIENT_ID=web-client\nAUTH0_CLIENT_SECRET="$(not-a-command)"\n')
    for key in (
        "AUTH0_DOMAIN",
        "AUTH0_AUDIENCE",
        "AUTH0_CLIENT_ID",
        "AUTH0_CLIENT_SECRET",
        "GCP_MONGO_URI",
        "MONGO_URI",
        "GCP_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    values = deploy.local_environment(arguments(env_file=backend, frontend_env_file=frontend))
    assert values["AUTH0_CLIENT_SECRET"] == "$(not-a-command)"
    assert values["MONGO_URI"] == "mongodb://localhost:28017"
    assert "GOOGLE_CLOUD_PROJECT" not in values
    assert "PUBSUB_EMULATOR_HOST" not in values
    frontend.write_text("AUTH0_DOMAIN=other.example.com\n")
    with pytest.raises(deploy.DeploymentError, match="differs"):
        deploy.local_environment(arguments(env_file=backend, frontend_env_file=frontend))


@pytest.mark.skipif(shutil.which("terraform") is None, reason="Terraform is not installed")
def test_configuration_evaluates_hcl_overrides_and_preserves_release_pins(infrastructure):
    (infrastructure / "terraform.tfvars").write_text(
        'project_id="example-project"\nregion="europe-west2-a"\n'
        'auth0_domain="tenant.example.com"\nauth0_audience="https://api.example.test/api"\n'
        'auth0_client_id="web-client"\nworker_instances=3\n'
    )
    result = deploy.configuration(arguments(region="europe-west2"), {})
    assert result["worker_instances"] == 3
    assert result["region"] == "europe-west2"
    result.update(
        deploy_services=True,
        backend_image_tag="keep-backend",
        frontend_image_tag="keep-frontend",
        secret_versions={key: "2" for key in result["secret_versions"]},
    )
    (infrastructure / deploy.GENERATED).write_text(json.dumps(result))
    restored = deploy.configuration(arguments(region="europe-west2"), {})
    assert restored["deploy_services"] is True
    assert restored["backend_image_tag"] == "keep-backend"
    assert restored["secret_versions"] == result["secret_versions"]
    with pytest.raises(deploy.DeploymentError, match="differs from the previous"):
        deploy.configuration(arguments(region="us-central1"), {})


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://localhost:28017/?replicaSet=rs0",
        "mongodb://127.0.0.1:28017/?replicaSet=rs0&tls=true",
        "mongodb://127.0.0.2:28017/?replicaSet=rs0&tls=true",
        "mongodb://mongo:27017/?replicaSet=rs0&tls=true",
        "mongodb://cluster.example.com/?replicaSet=rs0",
        "mongodb+srv://cluster.mongodb.net/?tls=false",
        "mongodb+srv://cluster.mongodb.net/?tlsAllowInvalidCertificates=true",
        "mongodb+srv://cluster.mongodb.net/?directConnection=true",
    ],
)
def test_rejects_local_or_insecure_cloud_mongo_inputs(uri):
    with pytest.raises(deploy.DeploymentError, match="cloud Mongo replica-set"):
        deploy.cloud_mongo_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb+srv://user:private@cluster.mongodb.net/?w=majority",
        "mongodb://user:private@a.example.com,b.example.com/?replicaSet=rs0&tls=true",
    ],
)
def test_accepts_tls_replica_set_inputs_without_exposing_or_changing_credentials(uri):
    assert deploy.cloud_mongo_uri(uri) == uri


def test_runner_hides_tokens_errors_and_ambient_terraform_overrides(monkeypatch, capsys):
    monkeypatch.setenv("TF_VAR_project_id", "unexpected-project")
    monkeypatch.setenv("TF_CLI_ARGS_apply", "-destroy")
    monkeypatch.setenv("TF_WORKSPACE", "another-environment")
    captured = {}

    def subprocess_run(command, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="private-token\n", stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", subprocess_run)
    assert deploy.run(["gcloud", "auth", "print-access-token"], "Check auth") == "private-token"
    assert capsys.readouterr().out == ""
    assert "TF_VAR_project_id" not in captured
    assert "TF_CLI_ARGS_apply" not in captured
    assert captured["TF_WORKSPACE"] == "default"
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="private-token",
            stderr="private-credential",
        ),
    )
    with pytest.raises(deploy.DeploymentError) as error:
        deploy.run(["gcloud"], "Check auth")
    assert "private" not in str(error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
def test_automatic_apply_refuses_destroy_and_replacement(
    infrastructure, settings, monkeypatch, actions
):
    commands = []

    def command(arguments, label, **kwargs):
        commands.append(arguments)
        if "show" in arguments:
            return json.dumps({"resource_changes": [{"change": {"actions": actions}}]})
        return ""

    monkeypatch.setattr(deploy, "run", command)
    operation = deploy.Deployment(arguments(), settings)
    with pytest.raises(deploy.DeploymentError, match="deletion/replacement"):
        operation.apply(settings, "test")
    assert not any("apply" in command for command in commands)
    assert (infrastructure / deploy.GENERATED).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("managed", [False, True])
def test_first_and_repeat_deploy_keep_existing_services_and_persist_rollout(
    infrastructure, settings, monkeypatch, managed
):
    operation = deploy.Deployment(arguments(), settings)
    monkeypatch.setattr(operation, "status", lambda: {"backend": {}} if managed else {})
    monkeypatch.setattr(operation, "initialize_state", lambda: None)
    monkeypatch.setattr(
        deploy,
        "run",
        lambda command, *args, **kwargs: 'module.service["backend"].resource' if managed else "",
    )
    applied = []
    monkeypatch.setattr(operation, "apply", lambda config, label: applied.append(config.copy()))
    outputs = {
        "egress_ip": "192.0.2.1",
        "public_url": "https://frontend.example.test",
        "backend_url": "https://backend.example.test",
    }
    monkeypatch.setattr(operation, "outputs", lambda: outputs)
    monkeypatch.setattr(
        deploy,
        "initialize_secrets",
        lambda *args: settings["secret_versions"],
    )
    monkeypatch.setattr(
        operation,
        "images",
        lambda *args: {
            "backend_image_tag": "new-release",
            "frontend_image_tag": "new-release",
        },
    )
    verified = []
    monkeypatch.setattr(deploy, "verify_services", lambda *args: verified.append(True))
    operation.deploy({})
    assert applied[0]["deploy_services"] is managed
    assert applied[1]["deploy_services"] is True
    assert applied[1]["backend_image_tag"] == "new-release"
    assert verified == [True]


def test_read_only_check_never_bootstraps_or_builds(settings, monkeypatch):
    operation = deploy.Deployment(arguments(check=True), settings)
    monkeypatch.setattr(operation, "status", dict)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only status attempted a deployment mutation")

    monkeypatch.setattr(operation, "initialize_state", forbidden)
    monkeypatch.setattr(operation, "apply", forbidden)
    monkeypatch.setattr(operation, "images", forbidden)
    operation.deploy({})


def test_existing_unmanaged_services_are_not_implicitly_adopted(settings, monkeypatch):
    operation = deploy.Deployment(arguments(), settings)
    monkeypatch.setattr(operation, "status", lambda: {"backend": {}})
    monkeypatch.setattr(operation, "initialize_state", lambda: None)
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: "")
    with pytest.raises(deploy.DeploymentError, match="outside this Terraform state"):
        operation.deploy({})


def test_mismatched_backend_refuses_cloud_changes(infrastructure, settings, monkeypatch):
    metadata = infrastructure / ".terraform"
    metadata.mkdir()
    (metadata / "terraform.tfstate").write_text(
        json.dumps(
            {
                "backend": {"type": "gcs", "config": {"bucket": "other-bucket", "prefix": "other"}},
            }
        )
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Mismatched backend attempted to change cloud resources")

    monkeypatch.setattr(deploy, "run", forbidden)
    operation = deploy.Deployment(arguments(), settings)
    with pytest.raises(deploy.DeploymentError, match="backend differs"):
        operation.initialize_state()


def test_cloud_status_reports_services_before_actionable_adc_error(settings, monkeypatch, capsys):
    operation = deploy.Deployment(arguments(check=True), settings)
    commands = []

    def command(arguments, label, **kwargs):
        commands.append(arguments)
        if label == "Check ADC":
            raise deploy.DeploymentError("failed")
        if label == "Check project billing":
            return '{"billingEnabled":true}'
        if label == "Inspect enabled APIs":
            return "run.googleapis.com"
        if label == "Inspect Cloud Run services":
            return "[]"
        return ""

    monkeypatch.setattr(deploy, "run", command)
    with pytest.raises(deploy.DeploymentError, match="auth application-default login"):
        operation.status()
    assert capsys.readouterr().out.count(": absent") == 4
    assert not any("enable" in command or "update" in command for command in commands)


@pytest.mark.parametrize("published", [False, True])
def test_image_publication_builds_missing_images_and_reuses_immutable_tags(
    settings, monkeypatch, published
):
    operation = deploy.Deployment(arguments(), settings)
    repositories = {
        role: f"europe-west2-docker.pkg.dev/example-project/logger-production-{role}"
        for role in ("backend", "frontend")
    }
    commands = []

    def command(arguments, label, **kwargs):
        commands.append(arguments)
        if "artifacts" in arguments:
            if not published:
                return "[]"
            repository = arguments[5]
            image = "api" if repository.endswith("backend") else "dashboard"
            return json.dumps(
                [
                    {
                        "image": f"{repository}/{image}",
                        "tag": "projects/p/locations/r/repositories/repo/packages/image/tags/test-release",
                    }
                ]
            )
        return ""

    monkeypatch.setattr(deploy, "run", command)
    assert operation.images({"image_repositories": repositories}) == {
        "backend_image_tag": "test-release",
        "frontend_image_tag": "test-release",
    }
    builds = [command for command in commands if command[:2] == ["docker", "build"]]
    pushes = [command for command in commands if command[:2] == ["docker", "push"]]
    assert len(builds) == len(pushes) == (0 if published else 2)


def test_secret_reuse_preserves_pins_and_keeps_local_session_secret_out(
    settings, monkeypatch, capsys
):
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_secret_version.return_value.state = secretmanager.SecretVersion.State.ENABLED
    monkeypatch.setattr(deploy.secretmanager, "SecretManagerServiceClient", lambda: client)
    monkeypatch.setattr(bootstrap_secrets, "existing_version", lambda *args: "3")
    inputs = []
    monkeypatch.setattr(
        bootstrap_secrets,
        "bootstrap",
        lambda *args, **kwargs: inputs.append(kwargs["input_values"]),
    )
    operation = deploy.Deployment(arguments(), settings)
    assert (
        deploy.initialize_secrets(
            operation,
            {
                "MONGO_URI": "mongodb://localhost:28017",
                "AUTH0_CLIENT_SECRET": "private-web-secret",
                "AUTH0_SECRET": "local-session-secret",
            },
        )
        == settings["secret_versions"]
    )
    assert inputs == [{"auth0_client_secret": b"private-web-secret"}]
    assert all(
        call.kwargs["request"]["name"].endswith("/versions/1")
        for call in client.get_secret_version.call_args_list
    )
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", [None, "public-api", "unready-worker"])
def test_rollout_verification_requires_ready_workers_and_real_unauthorized_response(
    settings, monkeypatch, failure
):
    operation = deploy.Deployment(arguments(), settings)

    def command(arguments, label, **kwargs):
        ready = not (failure == "unready-worker" and "spool-load" in label)
        return json.dumps(
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                        }
                    ]
                }
            }
        )

    opener = MagicMock()

    @contextmanager
    def response(request, **kwargs):
        if request.full_url.endswith("/api/v1/me") and failure != "public-api":
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        yield SimpleNamespace(status=200)

    opener.open.side_effect = response
    monkeypatch.setattr(deploy, "run", command)
    monkeypatch.setattr(deploy, "build_opener", lambda *args: opener)
    monkeypatch.setattr(deploy.time, "sleep", lambda *args: None)
    outputs = {
        "backend_url": "https://backend.example.test",
        "public_url": "https://frontend.example.test",
    }
    if failure:
        with pytest.raises(deploy.DeploymentError, match="probe failed|not ready"):
            deploy.verify_services(operation, outputs)
    else:
        deploy.verify_services(operation, outputs)
        assert opener.open.call_count == 3


def test_main_sanitizes_google_failures(monkeypatch, capsys):
    monkeypatch.setattr(deploy.shutil, "which", lambda tool: "/bin/tool")
    monkeypatch.setattr(deploy, "local_environment", lambda arguments: {})
    monkeypatch.setattr(deploy, "configuration", lambda *args: {})

    def failed(*args):
        raise PermissionDenied("private diagnostic")

    monkeypatch.setattr(deploy, "Deployment", failed)
    monkeypatch.setattr("sys.argv", ["deploy", "--check"])
    with pytest.raises(SystemExit) as error:
        deploy.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "check ADC" in output.err
    assert "private diagnostic" not in output.err
