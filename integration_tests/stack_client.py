import gzip
import json
import os
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx2 as httpx
from dotenv import dotenv_values
from joserfc import jwe
from joserfc.jwk import ECKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

LOGS_PER_DEVICE = 3
DEFAULT_BASE_URL = "http://localhost:8080"


class StackTestFailure(AssertionError):
    pass


class DeviceCredentials(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    client_id: str = Field(min_length=1)
    client_secret: SecretStr = Field(min_length=1)
    device_id: str | None = Field(default=None, min_length=1, max_length=255)
    encrypted: bool


class StackSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    base_url: str = DEFAULT_BASE_URL
    token_url: str
    audience: str = Field(min_length=1)
    plaintext: DeviceCredentials
    encrypted: DeviceCredentials
    http_timeout: float = Field(default=10, gt=0, le=120, allow_inf_nan=False)
    visibility_timeout: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    ca_cert: str = ""

    @field_validator("base_url", "token_url")
    @classmethod
    def validate_url(cls, value: str, info: ValidationInfo) -> str:
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or (
                parsed.scheme == "http"
                and (
                    info.field_name == "token_url"
                    or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError("Use HTTPS; only the loopback backend may use HTTP")
        return value.rstrip("/") if info.field_name == "base_url" else value

    def tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.ca_cert:
            context.load_verify_locations(cafile=self.ca_cert)
        return context


@dataclass
class DeviceResult:
    device_id: str
    record_ids: list[str]
    token: SecretStr = field(repr=False)


def load_settings() -> StackSettings | None:
    profile = Path(os.getenv("STACK_TEST_ENV_FILE", ".env.stack-test"))
    if "STACK_TEST_ENV_FILE" in os.environ and not profile.is_file():
        raise ValueError("STACK_TEST_ENV_FILE does not exist")
    values = {**(dotenv_values(profile) if profile.is_file() else {}), **os.environ}
    enabled = values.get("STACK_TEST_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("STACK_TEST_ENABLED must be true or false")
    if enabled == "false":
        return None

    def required(name: str) -> str:
        value = values.get("STACK_TEST_" + name)
        if not value or not value.strip():
            raise ValueError(f"STACK_TEST_{name} is required")
        return value

    def credentials(name: str, encrypted: bool) -> DeviceCredentials:
        return DeviceCredentials(
            client_id=required(name + "_CLIENT_ID"),
            client_secret=SecretStr(required(name + "_CLIENT_SECRET")),
            device_id=values.get(f"STACK_TEST_{name}_DEVICE_ID") or None,
            encrypted=encrypted,
        )

    settings = StackSettings(
        base_url=values.get("STACK_TEST_BASE_URL", DEFAULT_BASE_URL),
        token_url=required("TOKEN_URL"),
        audience=required("AUDIENCE"),
        plaintext=credentials("PLAINTEXT", False),
        encrypted=credentials("ENCRYPTED", True),
        http_timeout=values.get("STACK_TEST_HTTP_TIMEOUT", "10"),
        visibility_timeout=values.get("STACK_TEST_VISIBILITY_TIMEOUT", "30"),
        ca_cert=values.get("STACK_TEST_CA_CERT", ""),
    )
    if settings.plaintext.client_id == settings.encrypted.client_id:
        raise ValueError("Each device must have a different client ID")
    if (
        settings.plaintext.device_id is not None
        and settings.plaintext.device_id == settings.encrypted.device_id
    ):
        raise ValueError("Each test device must have a different device ID")
    return settings


def call(client: httpx.Client, method: str, path: str, *, expected: int = 200, **kwargs) -> dict:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError:
        raise StackTestFailure(f"{method} request failed; check connectivity and TLS") from None
    return document(response, expected)


def document(response: httpx.Response, expected: int) -> dict[str, Any]:
    if response.status_code != expected:
        raise StackTestFailure(
            f"{response.request.method} {response.request.url.path} returned HTTP "
            f"{response.status_code}; expected {expected}"
        )
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError()
        return result
    except (TypeError, ValueError):
        raise StackTestFailure("The service returned an invalid JSON object") from None


def retrieve_token(
    client: httpx.Client, settings: StackSettings, credentials: DeviceCredentials
) -> SecretStr:
    try:
        response = client.post(
            settings.token_url,
            json={
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret.get_secret_value(),
                "audience": settings.audience,
            },
        )
    except httpx.HTTPError:
        raise StackTestFailure("OAuth token request failed; check connectivity and TLS") from None
    result = document(response, 200)
    if (
        not isinstance(result.get("access_token"), str)
        or not result["access_token"]
        or str(result.get("token_type", "")).lower() != "bearer"
    ):
        raise StackTestFailure("Authorization server did not return a bearer access token")
    return SecretStr(result["access_token"])


def run_device(
    settings: StackSettings,
    credentials: DeviceCredentials,
    run_id: str,
    barrier: Barrier,
    transport: httpx.BaseTransport | None,
) -> DeviceResult:
    barrier.wait(timeout=settings.http_timeout)
    mode = "encrypted" if credentials.encrypted else "plaintext"
    with httpx.Client(
        base_url=settings.base_url + "/",
        timeout=settings.http_timeout,
        verify=settings.tls_context(),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as client:
        token = retrieve_token(client, settings, credentials)
        client.headers["Authorization"] = "Bearer " + token.get_secret_value()
        identity = call(client, "GET", "api/v1/me")
        device_id = identity.get("device_id")
        if not isinstance(device_id, str) or not device_id or len(device_id) > 255:
            raise StackTestFailure("Identity endpoint did not return an authenticated device")
        if credentials.device_id is not None and credentials.device_id != device_id:
            raise StackTestFailure(
                "Configured test device ID does not match the JWT-bound identity from /me"
            )
        config_path = f"api/v1/devices/{quote(device_id, safe='')}/configuration"
        configuration = call(client, "GET", config_path)
        allowed_modes = {"optional", "required"} if credentials.encrypted else {"tls_only"}
        if configuration["encryption_mode"] not in allowed_modes:
            raise StackTestFailure(
                f"The {mode} test device {device_id!r} has an incompatible policy. "
                "Configure its JWT-bound device policy before running the test; "
                "the test never changes device policies."
            )
        suite = configuration["encryption"]
        key_id = suite["public_key"]["kid"] if credentials.encrypted else None
        call(
            client,
            "PUT",
            config_path + "-status",
            json={
                "version": configuration["version"],
                "status": "applied",
                "supports_encryption": credentials.encrypted,
                "key_id": key_id,
            },
        )
        started_at = datetime.now(UTC)
        logs = [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "method": "get",
                "url": f"https://example.test/stack/{run_id}/{mode}/{index}",
                "status_code": 200,
                "headers": {"Authorization": "test-placeholder", "Cookie": "test-cookie"},
                "destination_ip": "203.0.113.50",
                "destination_port": 443,
                "http_version": "2",
                "response_size_bytes": 1024,
                "duration_ms": 12.5,
                "process_name": "stack-test-agent",
                "body": f"synthetic-{run_id}-{mode}-{index}",
            }
            for index in range(LOGS_PER_DEVICE)
        ]
        compressed = gzip.compress(json.dumps({"logs": logs}).encode())
        headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
        body = compressed
        if credentials.encrypted:
            if configuration["encryption_mode"] == "required":
                call(
                    client,
                    "POST",
                    "api/v1/traffic-log-uploads",
                    expected=403,
                    content=compressed,
                    headers=headers,
                )
            registry = jwe.JWERegistry(algorithms=[suite["algorithm"], suite["encryption"]])
            registry.max_ciphertext_length = 1_500_000
            body = jwe.encrypt_compact(
                {
                    "alg": suite["algorithm"],
                    "enc": suite["encryption"],
                    "kid": key_id,
                    "cty": "application/gzip",
                },
                compressed,
                ECKey.import_key(suite["public_key"]),
                registry=registry,
            ).encode()
            headers = {"Content-Type": "application/jose"}
        receipt = call(
            client,
            "POST",
            "api/v1/traffic-log-uploads",
            expected=202,
            content=body,
            headers=headers,
        )
        upload_id = receipt["upload_id"]
        deadline = monotonic() + settings.visibility_timeout
        while True:
            status = call(client, "GET", f"api/v1/traffic-log-uploads/{upload_id}")
            if status["status"] in {"parse_failed", "load_failed"}:
                raise StackTestFailure(f"Upload ended in {status['status']}; inspect receipt/DLQ")
            if status["status"] == "stored":
                if status["accepted_count"] != len(logs):
                    raise StackTestFailure("Stored upload has an unexpected accepted count")
                break
            if monotonic() >= deadline:
                raise StackTestFailure(
                    "Upload did not reach stored before timeout; ensure the parser and loader "
                    "workers are running and inspect the receipt/DLQs"
                )
            sleep(0.1)
        while True:
            records = []
            params = {
                "device_id": device_id,
                "limit": 100,
                "start_time": started_at.isoformat(),
            }
            while True:
                page = call(client, "GET", "api/v1/traffic-logs", params=params)
                records.extend(
                    record for record in page["items"] if record["batch_id"] == upload_id
                )
                cursor = page.get("next_cursor")
                if not cursor or len(records) >= len(logs) or monotonic() >= deadline:
                    break
                params["cursor"] = cursor
            if len(records) >= len(logs):
                break
            if monotonic() >= deadline:
                raise StackTestFailure("Acknowledged logs did not become visible through analysis")
            sleep(0.1)
        assert len(records) == len(logs), "Unexpected duplicate or additional logs"
        expected = {log["url"]: log for log in logs}
        assert {record["url"] for record in records} == set(expected)
        assert len({record["id"] for record in records}) == len(logs)
        for record in records:
            original = expected[record["url"]]
            assert record["device_id"] == device_id
            assert "user_id" not in record
            assert record["batch_id"] == upload_id
            assert record["method"] == "GET"
            for field in (
                "destination_ip",
                "destination_port",
                "http_version",
                "response_size_bytes",
                "duration_ms",
                "process_name",
            ):
                assert record[field] == original[field]
            assert record["headers"]["Authorization"] == "[REDACTED]"
            assert record["headers"]["Cookie"] == "[REDACTED]"
            assert "body" not in record
            assert record["body_length"] == len(original["body"].encode())
            assert record["body_sha256"] == sha256(original["body"].encode()).hexdigest()
        return DeviceResult(device_id, [record["id"] for record in records], token)


def run_stack(settings: StackSettings, *, transport: httpx.BaseTransport | None = None) -> None:
    run_id = uuid4().hex
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="stack-device") as executor:
        futures = [
            executor.submit(run_device, settings, credentials, run_id, barrier, transport)
            for credentials in (settings.plaintext, settings.encrypted)
        ]
        first, second = [future.result() for future in as_completed(futures)]
    if first.device_id == second.device_id:
        raise StackTestFailure("Dedicated OAuth clients must resolve to distinct devices")
    for device, other in ((first, second), (second, first)):
        with httpx.Client(
            base_url=settings.base_url + "/",
            headers={"Authorization": "Bearer " + device.token.get_secret_value()},
            timeout=settings.http_timeout,
            verify=settings.tls_context(),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            call(
                client,
                "GET",
                "api/v1/traffic-logs",
                params={"device_id": other.device_id},
                expected=403,
            )
            call(
                client,
                "GET",
                f"api/v1/devices/{quote(other.device_id, safe='')}/configuration",
                expected=403,
            )
            call(client, "GET", f"api/v1/traffic-logs/{other.record_ids[0]}", expected=404)
