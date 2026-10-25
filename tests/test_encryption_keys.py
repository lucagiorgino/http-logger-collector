import gzip
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from joserfc import jwe
from joserfc.jwk import ECKey

from app.core import encryption as encryption_module
from app.core.config import settings
from app.core.encryption import EncryptionKeys, InvalidEnvelope, get_encryption_keys
from app.core.openbao import KMSUnavailable
from app.deps import get_request_encryption_keys
from app.kms import generate_key, write_credential
from app.main import app


@pytest.fixture
def key_document(encryption_keys, monkeypatch):
    document = {
        "active_key_id": "current",
        "keys": [key.as_dict(private=True) for key in encryption_keys.keys.values()],
    }
    client = MagicMock()
    client.__enter__.return_value = client
    client.load_keyset.return_value = document
    monkeypatch.setattr(encryption_module, "OpenBaoClient", lambda: client)
    get_encryption_keys.cache_clear()
    try:
        yield document
    finally:
        get_encryption_keys.cache_clear()


def test_private_key_set_supports_rotation_without_exposing_private_fields(
    key_document, encryption_keys
):
    loaded = get_encryption_keys()
    assert set(loaded.keys) == {"current", "previous"}
    assert loaded.public_key == encryption_keys.public_key


@pytest.mark.parametrize("invalid", ["missing", "public", "duplicate", "wrong-active"])
def test_invalid_key_configuration_fails_explicitly(key_document, encryption_keys, invalid):
    key = encryption_keys.keys["current"].as_dict(private=invalid != "public")
    key_document["keys"] = [key, key] if invalid == "duplicate" else [key]
    if invalid == "missing":
        del key_document["keys"]
    if invalid == "wrong-active":
        key_document["active_key_id"] = "unknown"
    with pytest.raises(KMSUnavailable, match="Invalid OpenBao encryption key set"):
        get_encryption_keys()


def test_development_credentials_are_private_and_root_token_is_preserved(tmp_path):
    path = tmp_path / "private" / "token"
    write_credential(str(path), "test-first-token", create_only=True)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    write_credential(str(path), "test-second-token", create_only=True)
    assert path.read_text() == "test-first-token"
    write_credential(str(path), "test-refreshed-token")
    assert path.read_text() == "test-refreshed-token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    ("curve", "algorithm", "encryption"),
    [
        ("P-256", "ECDH-ES+A128KW", "A128GCM"),
        ("P-521", "ECDH-ES+A192KW", "A192GCM"),
    ],
)
def test_configured_suite_drives_keys_client_configuration_and_ingestion(
    key_document,
    encrypted_client,
    repository,
    traffic_payload,
    monkeypatch,
    curve,
    algorithm,
    encryption,
):
    monkeypatch.setattr(settings, "encryption_curve", curve)
    monkeypatch.setattr(settings, "encryption_key_algorithm", algorithm)
    monkeypatch.setattr(settings, "encryption_content_algorithm", encryption)
    key_document["keys"] = [generate_key("current")]
    keys = get_encryption_keys()
    app.dependency_overrides[get_request_encryption_keys] = lambda: keys
    repository.get_device_configuration.return_value = {"version": 1, "encryption_mode": "required"}
    response = encrypted_client.get("/api/v1/devices/device-456/configuration")
    parameters = response.json()["encryption"]
    assert parameters["algorithm"] == algorithm
    assert parameters["encryption"] == encryption
    assert parameters["curve"] == parameters["public_key"]["crv"] == curve
    public = ECKey.import_key(parameters["public_key"])
    body = jwe.encrypt_compact(
        {"alg": algorithm, "enc": encryption, "kid": "current", "cty": "application/gzip"},
        gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
        public,
        registry=keys.registry,
    )
    response = encrypted_client.post(
        "/api/v1/traffic-log-batches",
        content=body,
        headers={"Content-Type": "application/jose"},
    )
    assert response.status_code == 200
    repository.insert_traffic_logs.assert_awaited_once()
    wrong_encryption = "A256GCM" if encryption != "A256GCM" else "A128GCM"
    wrong = jwe.JWERegistry(algorithms=[algorithm, wrong_encryption])
    unsupported = jwe.encrypt_compact(
        {"alg": algorithm, "enc": wrong_encryption, "kid": "current", "cty": "application/gzip"},
        b"data",
        public,
        registry=wrong,
    )
    with pytest.raises(InvalidEnvelope):
        keys.decrypt(unsupported.encode())


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("encryption_key_algorithm", "RSA-OAEP-256"),
        ("encryption_content_algorithm", "A128CBC-HS256"),
        ("encryption_curve", "secp256k1"),
    ],
)
def test_non_approved_suite_configuration_fails_explicitly(monkeypatch, setting, value):
    monkeypatch.setattr(settings, setting, value)
    with pytest.raises(ValueError, match="ENCRYPTION_"):
        EncryptionKeys({})


def test_private_keys_must_match_configured_curve(key_document, monkeypatch):
    curve = "P-256" if settings.encryption_curve != "P-256" else "P-384"
    monkeypatch.setattr(settings, "encryption_curve", curve)
    with pytest.raises(KMSUnavailable, match="Invalid OpenBao encryption key set"):
        get_encryption_keys()


def test_production_defaults_to_https_without_local_env(tmp_path):
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "APP_ENV": "production",
        "APP_RELOAD": "false",
        "MONGO_ALLOW_PRIMARY_READS": "false",
    }
    env.pop("REQUIRE_HTTPS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.config import settings; print(settings.require_https)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True"
