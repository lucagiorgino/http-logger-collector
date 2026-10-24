import base64
import json
import re
import ssl
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import httpx2 as httpx

from app.core.config import settings

PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")
MAX_KEYSET_BYTES = 65_536


class KMSUnavailable(RuntimeError):
    pass


def read_credential(path: str) -> str:
    try:
        value = Path(path).read_text().strip()
        if not value or len(value) > 16_384 or any(character.isspace() for character in value):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise KMSUnavailable("OpenBao credential is missing or invalid") from None


class OpenBaoClient:
    def __init__(
        self, token: str | None = None, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        address = urlsplit(settings.openbao_addr)
        if (
            address.scheme not in {"http", "https"}
            or not address.hostname
            or address.username
            or address.password
            or address.query
            or address.fragment
            or (settings.app_env != "development" and address.scheme != "https")
        ):
            raise ValueError("OPENBAO_ADDR must use HTTPS outside development")
        for value in (
            settings.openbao_transit_mount,
            settings.openbao_transit_key,
            settings.openbao_kv_mount,
            settings.openbao_kv_path,
            settings.openbao_auth_mount,
        ):
            if not PATH_COMPONENT.fullmatch(value) or value in {".", ".."}:
                raise ValueError("OpenBao mount and key names must be single safe path components")
        if settings.openbao_timeout_seconds <= 0:
            raise ValueError("OPENBAO_TIMEOUT_SECONDS must be positive")
        self.http = httpx.Client(
            base_url=settings.openbao_addr.rstrip("/") + "/v1/",
            timeout=settings.openbao_timeout_seconds,
            verify=ssl.create_default_context(cafile=settings.openbao_ca_cert or None),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self.token = token

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        try:
            response = self.http.request(
                method,
                path,
                json=payload,
                headers={"X-Vault-Token": self.token} if self.token else {},
            )
            if response.status_code == 404 and missing_ok:
                return None
            response.raise_for_status()
            if not response.content:
                return {}
            if len(response.content) > 1_048_576:
                raise ValueError()
            document = response.json()
            if not isinstance(document, dict):
                raise TypeError()
            return document
        except (httpx.HTTPError, TypeError, ValueError):
            raise KMSUnavailable("OpenBao operation failed") from None

    def authenticate(self) -> None:
        if self.token:
            return
        if settings.openbao_kubernetes_role:
            response = self.request(
                "POST",
                f"auth/{settings.openbao_auth_mount}/login",
                {
                    "role": settings.openbao_kubernetes_role,
                    "jwt": read_credential(settings.openbao_jwt_file),
                },
            )
            auth = (response or {}).get("auth")
            token = auth.get("client_token") if isinstance(auth, dict) else None
            if not isinstance(token, str) or not token:
                raise KMSUnavailable("OpenBao authentication failed")
            self.token = token
        else:
            self.token = read_credential(settings.openbao_token_file)

    @property
    def kv_path(self) -> str:
        return f"{settings.openbao_kv_mount}/data/{settings.openbao_kv_path}"

    def read_wrapped(self, *, missing_ok: bool = False) -> dict[str, Any] | None:
        self.authenticate()
        response = self.request("GET", self.kv_path, missing_ok=missing_ok)
        if response is None:
            return None
        try:
            data = response["data"]
            if not isinstance(data["data"]["ciphertext"], str):
                raise TypeError()
            return data
        except (KeyError, TypeError, ValueError):
            raise KMSUnavailable("Invalid OpenBao key-set record") from None

    def unwrap(self, record: dict[str, Any]) -> dict[str, Any]:
        self.authenticate()
        response = self.request(
            "POST",
            f"{settings.openbao_transit_mount}/decrypt/{settings.openbao_transit_key}",
            {"ciphertext": record["data"]["ciphertext"]},
        )
        try:
            plaintext = (response or {})["data"]["plaintext"]
            if not isinstance(plaintext, str) or len(plaintext) > MAX_KEYSET_BYTES * 2:
                raise ValueError()
            decoded = base64.b64decode(plaintext, validate=True)
            if len(decoded) > MAX_KEYSET_BYTES:
                raise ValueError()
            document = json.loads(decoded)
            if not isinstance(document, dict):
                raise TypeError()
            return document
        except (KeyError, TypeError, ValueError):
            raise KMSUnavailable("Invalid OpenBao key-set plaintext") from None

    def load_keyset(self) -> dict[str, Any]:
        record = self.read_wrapped()
        if record is None:
            raise KMSUnavailable("OpenBao key set has not been provisioned")
        return self.unwrap(record)

    def store_keyset(self, document: dict[str, Any], version: int) -> None:
        self.authenticate()
        plaintext = json.dumps(document).encode()
        if len(plaintext) > MAX_KEYSET_BYTES:
            raise ValueError("Encryption key set exceeds the supported size")
        response = self.request(
            "POST",
            f"{settings.openbao_transit_mount}/encrypt/{settings.openbao_transit_key}",
            {"plaintext": base64.b64encode(plaintext).decode()},
        )
        try:
            ciphertext = (response or {})["data"]["ciphertext"]
            if not isinstance(ciphertext, str) or not ciphertext.startswith("vault:v"):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise KMSUnavailable("Invalid OpenBao wrapping response") from None
        self.request(
            "POST",
            self.kv_path,
            {"options": {"cas": version}, "data": {"ciphertext": ciphertext}},
        )
