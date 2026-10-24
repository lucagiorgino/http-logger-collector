import base64
import json

import httpx2 as httpx
import pytest

from app.core.config import settings
from app.core.encryption import get_encryption_keys
from app.core.openbao import KMSUnavailable, OpenBaoClient
from app.kms import ensure_engines
from app.main import app


def test_runtime_reads_only_wrapped_kv_and_transit_decrypt(encryption_keys):
    keyset = {
        "active_key_id": "current",
        "keys": [key.as_dict(private=True) for key in encryption_keys.keys.values()],
    }
    calls = []

    def respond(request):
        assert request.headers["X-Vault-Token"] == "test-runtime-token"
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "data": {"ciphertext": "vault:v1:test-wrapped"},
                        "metadata": {"version": 1},
                    }
                },
            )
        assert json.loads(request.content) == {"ciphertext": "vault:v1:test-wrapped"}
        return httpx.Response(
            200,
            json={"data": {"plaintext": base64.b64encode(json.dumps(keyset).encode()).decode()}},
        )

    with OpenBaoClient("test-runtime-token", transport=httpx.MockTransport(respond)) as client:
        assert client.load_keyset()["active_key_id"] == "current"
    assert calls == [
        ("GET", f"/v1/{settings.openbao_kv_mount}/data/{settings.openbao_kv_path}"),
        ("POST", f"/v1/{settings.openbao_transit_mount}/decrypt/{settings.openbao_transit_key}"),
    ]


def test_kubernetes_login_uses_bound_jwt_then_runtime_token(tmp_path, monkeypatch):
    jwt_file = tmp_path / "projected-token"
    jwt_file.write_text("test-kubernetes-jwt")
    monkeypatch.setattr(settings, "openbao_kubernetes_role", "test-role")
    monkeypatch.setattr(settings, "openbao_jwt_file", str(jwt_file))
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/login"):
            assert "X-Vault-Token" not in request.headers
            assert json.loads(request.content) == {
                "role": "test-role",
                "jwt": "test-kubernetes-jwt",
            }
            return httpx.Response(200, json={"auth": {"client_token": "test-runtime-token"}})
        assert request.headers["X-Vault-Token"] == "test-runtime-token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {"ciphertext": "vault:v1:test"},
                    "metadata": {"version": 1},
                }
            },
        )

    with OpenBaoClient(transport=httpx.MockTransport(respond)) as client:
        assert client.read_wrapped() is not None
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["forbidden", "timeout", "malformed", "bad-plaintext"])
def test_kms_failures_are_explicit_without_secret_echo(failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-service-details")
        if failure == "forbidden":
            return httpx.Response(403, json={"errors": ["private-service-details"]})
        if failure == "malformed":
            return httpx.Response(200, json={"data": None})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {"data": {"ciphertext": "vault:v1:test"}, "metadata": {"version": 1}}
                },
            )
        return httpx.Response(200, json={"data": {"plaintext": "invalid-base64!"}})

    with (
        OpenBaoClient("test-token", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(KMSUnavailable) as exc,
    ):
        client.load_keyset()
    assert "private-service-details" not in str(exc.value)


def test_kms_failure_cannot_fall_back_to_local_material(authorized_client):
    def unavailable():
        raise KMSUnavailable("private-service-details")

    app.dependency_overrides[get_encryption_keys] = unavailable
    response = authorized_client.get("/api/v1/devices/device-456/configuration")
    assert response.status_code == 503
    assert "private-service-details" not in response.text


def test_production_kms_requires_https_and_rejects_credential_urls(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    for address in ("http://localhost:28200", "https://user:password@bao.test"):
        monkeypatch.setattr(settings, "openbao_addr", address)
        with pytest.raises(ValueError, match="OPENBAO_ADDR"):
            OpenBaoClient("test-token")


def test_kv_updates_wrap_keyset_before_cas_write():
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if "/encrypt/" in request.url.path:
            assert json.loads(base64.b64decode(payload["plaintext"])) == {"keys": []}
            return httpx.Response(200, json={"data": {"ciphertext": "vault:v2:wrapped"}})
        assert payload == {"options": {"cas": 7}, "data": {"ciphertext": "vault:v2:wrapped"}}
        return httpx.Response(200, json={"data": {"version": 8}})

    with OpenBaoClient("test-admin", transport=httpx.MockTransport(respond)) as client:
        client.store_keyset({"keys": []}, 7)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "mismatch", ["kv-v1", "wrong-engine", "exportable", "wrong-key-type", "plaintext-backup"]
)
def test_bootstrap_refuses_incompatible_or_exportable_kms(mismatch):
    mounts = {
        settings.openbao_kv_mount + "/": {"type": "kv", "options": {"version": "2"}},
        settings.openbao_transit_mount + "/": {"type": "transit"},
    }
    key = {"type": "aes256-gcm96", "exportable": False, "allow_plaintext_backup": False}
    if mismatch == "kv-v1":
        mounts[settings.openbao_kv_mount + "/"]["options"]["version"] = "1"
    elif mismatch == "wrong-engine":
        mounts[settings.openbao_transit_mount + "/"]["type"] = "kv"
    else:
        field, value = {
            "exportable": ("exportable", True),
            "wrong-key-type": ("type", "ecdsa-p384"),
            "plaintext-backup": ("allow_plaintext_backup", True),
        }[mismatch]
        key[field] = value

    def respond(request):
        assert request.method == "GET"
        return httpx.Response(
            200, json={"data": mounts if request.url.path == "/v1/sys/mounts" else key}
        )

    with (
        OpenBaoClient("test-admin", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(KMSUnavailable),
    ):
        ensure_engines(client)
