import json
import warnings

import google_crc32c
import pytest
from google.api_core.exceptions import NotFound, PermissionDenied, ServiceUnavailable
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import secretmanager

from infra.gcp import bootstrap_secrets as bootstrap


class FakeSecrets:
    def __init__(self):
        self.names = {
            key: f"projects/example-project/secrets/logger-staging-{suffix}"
            for key, suffix in bootstrap.SECRET_SUFFIXES.items()
        }
        self.versions = {name: [] for name in self.names.values()}
        self.payloads = {}
        self.ambiguous_key = None

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False

    def get_secret(self, *, request, **kwargs):
        if request["name"] not in self.versions:
            raise NotFound("private diagnostic")

    def list_secret_versions(self, *, request, **kwargs):
        return iter(self.versions[request["parent"]])

    def seed(self, key, number=1, state=secretmanager.SecretVersion.State.ENABLED):
        name = self.names[key]
        version = secretmanager.SecretVersion(
            name=f"{name}/versions/{number}",
            state=state,
            client_specified_payload_checksum=True,
        )
        self.versions[name].append(version)
        return version

    def add_secret_version(self, *, request, retry, timeout):
        assert retry is None and timeout > 0
        name = request["parent"]
        value = request["payload"]["data"]
        assert request["payload"]["data_crc32c"] == google_crc32c.value(value)
        key = next(key for key, parent in self.names.items() if parent == name)
        self.payloads[key] = value
        version = self.seed(key)
        if self.ambiguous_key == key:
            raise ServiceUnavailable("private diagnostic")
        return version


@pytest.fixture
def secure_inputs(monkeypatch):
    calls = []

    def prompt(label):
        calls.append(label)
        return "private-mongo-uri" if label.startswith("Cloud Mongo") else "private-auth0-secret"

    monkeypatch.setattr(bootstrap.getpass, "getpass", prompt)
    return calls


def test_bootstrap_initializes_empty_containers_and_rerun_preserves_everything(
    monkeypatch, capsys, secure_inputs
):
    client = FakeSecrets()
    versions = bootstrap.bootstrap(client, "example-project", "staging")
    assert versions == {key: "1" for key in client.names}
    document = json.loads(client.payloads["ec_keyset"])
    from app.core.encryption import parse_keyset

    assert parse_keyset(document).active_key_id == document["active_key_id"]
    assert len(bytes.fromhex(client.payloads["auth0_session"].decode())) == 32
    assert client.payloads["mongo_uri"] == b"private-mongo-uri"
    assert client.payloads["auth0_client_secret"] == b"private-auth0-secret"
    assert len(secure_inputs) == 2
    saved = client.payloads.copy()

    def forbidden(*args, **kwargs):
        pytest.fail("Rerun must not read inputs, generate keys or create versions")

    monkeypatch.setattr(bootstrap, "read_input", forbidden)
    monkeypatch.setattr(bootstrap, "generate_keyset", forbidden)
    monkeypatch.setattr(bootstrap.secrets, "token_hex", forbidden)
    monkeypatch.setattr(client, "add_secret_version", forbidden)
    assert bootstrap.bootstrap(client, "example-project", "staging") == versions
    assert client.payloads == saved
    output = capsys.readouterr()
    for value in [b"private-mongo-uri", b"private-auth0-secret", document["keys"][0]["d"].encode()]:
        assert value.decode() not in output.out + output.err


def test_bootstrap_fills_only_missing_values(secure_inputs):
    client = FakeSecrets()
    client.seed("mongo_uri", 4)
    client.seed("ec_keyset", 2)
    client.seed("ec_keyset", 3)
    versions = bootstrap.bootstrap(client, "example-project", "staging")
    assert versions["mongo_uri"] == "4"
    assert versions["ec_keyset"] == "3"
    assert set(client.payloads) == {"auth0_client_secret", "auth0_session"}
    assert len(secure_inputs) == 1
    assert secure_inputs[0].startswith("Auth0")


@pytest.mark.parametrize(
    "state",
    [secretmanager.SecretVersion.State.DISABLED, secretmanager.SecretVersion.State.DESTROYED],
)
def test_disabled_or_destroyed_newest_version_is_not_replaced(state, secure_inputs):
    client = FakeSecrets()
    client.seed("ec_keyset", 1)
    client.seed("ec_keyset", 2, state)
    with pytest.raises(bootstrap.BootstrapError, match="resolve it explicitly"):
        bootstrap.bootstrap(client, "example-project", "staging")
    assert not secure_inputs
    assert not client.payloads


def test_missing_terraform_container_fails_before_prompting_or_writing(secure_inputs):
    client = FakeSecrets()
    del client.versions[client.names["auth0_session"]]
    with pytest.raises(bootstrap.BootstrapError, match="Terraform foundation"):
        bootstrap.bootstrap(client, "example-project", "staging")
    assert not secure_inputs
    assert not client.payloads


def test_invalid_input_fails_before_any_secrets_are_created(monkeypatch):
    client = FakeSecrets()
    monkeypatch.setattr(
        bootstrap.getpass,
        "getpass",
        lambda label: "private-mongo-uri" if label.startswith("Cloud Mongo") else "",
    )
    with pytest.raises(bootstrap.BootstrapError, match="must contain"):
        bootstrap.bootstrap(client, "example-project", "staging")
    assert not client.payloads


def test_creation_requires_confirmed_payload_checksum(monkeypatch, secure_inputs):
    client = FakeSecrets()
    add = client.add_secret_version

    def unconfirmed(**kwargs):
        version = add(**kwargs)
        version.client_specified_payload_checksum = False
        return version

    monkeypatch.setattr(client, "add_secret_version", unconfirmed)
    with pytest.raises(bootstrap.BootstrapError, match="payload integrity"):
        bootstrap.bootstrap(client, "example-project", "staging")
    assert set(client.payloads) == {"mongo_uri"}


def test_rerun_recovers_partial_ambiguous_creation_without_rotating(monkeypatch, secure_inputs):
    client = FakeSecrets()
    client.ambiguous_key = "ec_keyset"
    with pytest.raises(ServiceUnavailable):
        bootstrap.bootstrap(client, "example-project", "staging")
    saved_keyset = client.payloads["ec_keyset"]
    client.ambiguous_key = None

    def forbidden():
        pytest.fail("An ambiguously created keyset must be preserved")

    monkeypatch.setattr(bootstrap, "generate_keyset", forbidden)
    versions = bootstrap.bootstrap(client, "example-project", "staging")
    assert versions == {key: "1" for key in client.names}
    assert client.payloads["ec_keyset"] == saved_keyset
    assert all(len(versions) == 1 for versions in client.versions.values())


def test_bootstrap_reports_only_numeric_versions(monkeypatch, capsys, secure_inputs):
    client = FakeSecrets()
    monkeypatch.setattr(bootstrap.secretmanager, "SecretManagerServiceClient", lambda: client)
    monkeypatch.setattr(
        "sys.argv", ["bootstrap", "--project=example-project", "--environment=staging"]
    )
    bootstrap.main()
    output = capsys.readouterr()
    assert output.out.startswith("secret_versions = {\n")
    assert output.out.count('= "1"') == 4
    assert "private-mongo-uri" not in output.out + output.err
    assert "private-auth0-secret" not in output.out + output.err


@pytest.mark.parametrize(
    "error", [PermissionDenied("private credential"), DefaultCredentialsError("private credential")]
)
def test_bootstrap_main_sanitizes_cloud_failures(monkeypatch, capsys, error):
    def fail():
        raise error

    monkeypatch.setattr(bootstrap.secretmanager, "SecretManagerServiceClient", fail)
    monkeypatch.setattr("sys.argv", ["bootstrap", "--project=example-project"])
    with pytest.raises(SystemExit) as caught:
        bootstrap.main()
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "check ADC" in output.err
    assert "private credential" not in output.err


def test_private_file_inputs_are_not_echoed(tmp_path):
    path = tmp_path / "credential"
    path.write_text("private-value\n")
    path.chmod(0o600)
    assert bootstrap.read_input("Credential", path) == b"private-value"
    path.chmod(0o644)
    with pytest.raises(bootstrap.BootstrapError, match="private regular file"):
        bootstrap.read_input("Credential", path)


@pytest.mark.parametrize("data", [b"", b" \n", b"\xff", b"x" * 65_536 + b"\nmore"])
def test_invalid_or_oversized_input_is_rejected(tmp_path, data):
    path = tmp_path / "credential"
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.read_input("Credential", path)


def test_prompt_refuses_echoing_stdin(monkeypatch):
    def insecure_prompt(label):
        warnings.warn("cannot hide input", bootstrap.getpass.GetPassWarning)
        return "private-value"

    monkeypatch.setattr(bootstrap.getpass, "getpass", insecure_prompt)
    with pytest.raises(bootstrap.BootstrapError, match="Cannot securely read"):
        bootstrap.read_input("Credential", None)
