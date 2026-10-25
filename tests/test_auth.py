import gzip
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from fastapi_plugin import Auth0FastAPI
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from app.core import auth
from app.core.encryption import EncryptionKeys
from app.core.storage_encryption import get_storage_encryption
from app.deps import get_mongo_repository, get_request_encryption_keys
from app.main import app
from app.services.traffic_service import DEVICE_ID_CLAIM

client = TestClient(app)


def test_legacy_auth_endpoints_removed() -> None:
    authorize_response = client.post(
        "/auth/authorize",
        json={"user_id": "user-123", "device_id": "device-456"},
    )
    token_response = client.post(
        "/auth/token",
        json={"user_id": "user-123"},
    )

    assert authorize_response.status_code == 404
    assert token_response.status_code == 404


@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer not-a-jwt"])
def test_identity_requires_a_bearer_jwt(authorization) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    response = client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_api_exposes_only_current_resources() -> None:
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/health",
        "/api/v1/me",
        "/api/v1/status",
        "/api/v1/devices",
        "/api/v1/devices/{device_id}",
        "/api/v1/devices/{device_id}/configuration",
        "/api/v1/devices/{device_id}/configuration-status",
        "/api/v1/traffic-log-uploads",
        "/api/v1/traffic-log-uploads/{upload_id}",
        "/api/v1/traffic-logs",
        "/api/v1/traffic-logs/{record_id}",
        "/api/v1/workspace/configuration",
    }
    operation = schema["paths"]["/api/v1/me"]["get"]
    assert operation["security"] == [{"Auth0Bearer": []}]


@pytest.fixture(scope="module")
def signing_keys():
    return [RSAKey.generate_key(2048, parameters={"kid": kid}) for kid in ("first", "second")]


@pytest.fixture
def signed_tokens(monkeypatch, signing_keys):
    state = {
        "requests": [],
        "jwks": {"keys": [signing_keys[0].as_dict(private=False)]},
        "claims": {
            "iss": "https://issuer.example/",
            "aud": "https://api.example",
            "sub": "device-client@clients",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "permissions": ["manage:devices"],
        },
    }

    async def fetch(url):
        state["requests"].append(url)
        if url == "https://issuer.example/.well-known/openid-configuration":
            return {
                "issuer": "https://issuer.example/",
                "jwks_uri": "https://issuer.example/.well-known/jwks.json",
            }
        assert url == "https://issuer.example/.well-known/jwks.json"
        return state["jwks"]

    sdk = Auth0FastAPI(domain="issuer.example", audience="https://api.example", custom_fetch=fetch)
    monkeypatch.setattr(auth, "verify_with_auth0", sdk.require_auth())

    def mint(claims=None, *, key=None, kid="first", algorithm="RS256"):
        return jwt.encode(
            {"alg": algorithm, "kid": kid},
            state["claims"] if claims is None else claims,
            signing_keys[0] if key is None else key,
        )

    return mint, state


def test_auth0_sdk_verifies_signed_tokens_and_caches_discovery_and_keys(signed_tokens):
    mint, state = signed_tokens
    for _ in range(2):
        response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {mint()}"})
        assert response.status_code == 200
        assert response.json()["device_id"] == "device-client@clients"
        assert "user_id" not in response.json()
    assert state["requests"] == [
        "https://issuer.example/.well-known/openid-configuration",
        "https://issuer.example/.well-known/jwks.json",
    ]


@pytest.mark.parametrize("client_claim", ["azp", "client_id"])
def test_ingestion_attribution_comes_from_verified_jwt_and_connection(
    signed_tokens,
    repository,
    traffic_payload,
    monkeypatch,
    client_claim,
    storage_encryption,
    upload_harness,
    drain_uploads,
):
    mint, state = signed_tokens
    token = mint(
        state["claims"]
        | {DEVICE_ID_CLAIM: "issuer-bound-device", client_claim: "issuer-bound-client"}
    )
    monkeypatch.setitem(app.dependency_overrides, get_mongo_repository, lambda: repository)
    monkeypatch.setitem(
        app.dependency_overrides, get_request_encryption_keys, lambda: EncryptionKeys({})
    )
    monkeypatch.setitem(
        app.dependency_overrides, get_storage_encryption, lambda: storage_encryption
    )
    signed_client = TestClient(app, base_url="https://testserver", client=("2001:db8::1", 50000))
    response = signed_client.post(
        "/api/v1/traffic-log-uploads",
        content=gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "X-Forwarded-For": "198.51.100.1",
        },
    )
    assert response.status_code == 202
    repository.insert_queued_traffic_logs.assert_not_awaited()
    descriptor = upload_harness.store.records[response.json()["upload_id"]]["descriptor"]
    assert descriptor["device_id"] == "issuer-bound-device"
    drain_uploads()
    stored = repository.insert_queued_traffic_logs.call_args.args[0][0]
    assert stored["metadata"] == {
        "device_id": "issuer-bound-device",
        "suspicious": False,
    }
    assert stored["ingestion"]["client_id"] == "issuer-bound-client"
    assert stored["ingestion"]["source_ip"] == "2001:db8::1"
    repository.get_device_configuration.assert_awaited_once_with("issuer-bound-device")
    assert signed_client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json() == {
        "device_id": "issuer-bound-device",
        "client_id": "issuer-bound-client",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://different.example/"},
        {"aud": "different-api"},
        {"exp": 1},
        {"sub": ""},
        {"sub": 123},
        {DEVICE_ID_CLAIM: ""},
        {"azp": None},
        {"client_id": ["invalid"]},
        {"cnf": {"jkt": "proof-bound-token"}},
    ],
)
def test_auth0_sdk_rejects_invalid_signed_claims(signed_tokens, changes):
    mint, state = signed_tokens
    token = mint(state["claims"] | changes)
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "different.example" not in response.text


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
def test_required_signed_claims_cannot_be_omitted(signed_tokens, claim):
    mint, state = signed_tokens
    token = mint({key: value for key, value in state["claims"].items() if key != claim})
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_auth0_sdk_checks_signatures_key_ids_and_algorithms(signed_tokens, signing_keys):
    mint, _state = signed_tokens
    for token in (
        mint(key=signing_keys[1]),
        mint(kid="unknown"),
        mint(key=OctKey.generate_key(256), algorithm="HS256"),
    ):
        response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_unexpected_sdk_errors_are_sanitized_service_failures(monkeypatch, caplog):
    verify = AsyncMock(side_effect=RuntimeError("private-sdk-details"))
    monkeypatch.setattr(auth.auth0.api_client, "verify_request", verify)
    response = client.get("/api/v1/me", headers={"Authorization": "Bearer abc.def.ghi"})
    assert response.status_code == 503
    assert "authentication" in caplog.text
    assert "private-sdk-details" not in response.text + caplog.text
