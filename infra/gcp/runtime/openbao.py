"""Cloud-image-only implementation of the application's existing KMS interface."""

import asyncio
import base64
import json
import os
import re
from typing import Any, Self

import google_crc32c
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError
from google.cloud import kms_v1, secretmanager

from app.core.config import settings

MAX_KEYSET_BYTES = 65_536
MAX_FIELD_BYTES = 16_777_216
FORMAT = "gcp-kms-envelope-v1"


class KMSUnavailable(RuntimeError):
    pass


def checksum(value: bytes) -> int:
    return google_crc32c.value(value)


def decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_FIELD_BYTES:
        raise ValueError("Invalid encoded field")
    return base64.b64decode(value, validate=True)


def operation(method: str, path: str) -> str:
    prefix = settings.openbao_transit_mount
    key = settings.openbao_storage_key
    allowed = {
        ("GET", f"{prefix}/keys/{key}"): "metadata",
        ("POST", f"{prefix}/encrypt/{key}"): "encrypt",
        ("POST", f"{prefix}/decrypt/{key}"): "decrypt",
    }
    if (method, path) not in allowed:
        raise KMSUnavailable("Unsupported cloud KMS operation")
    return allowed[method, path]


def metadata(key: kms_v1.CryptoKey) -> dict[str, Any]:
    if key.purpose != kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT:
        raise KMSUnavailable("Cloud KMS key must be a symmetric encryption key")
    return {
        "data": {
            "type": "aes256-gcm96",
            "exportable": False,
            "allow_plaintext_backup": False,
            "derived": False,
            "convergent_encryption": False,
        }
    }


def prepare(entry: dict[str, str]) -> tuple[bytes, bytes, bytes, bytes]:
    plaintext = decode(entry["plaintext"])
    aad = decode(entry["associated_data"])
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    return dek, aad, nonce, AESGCM(dek).encrypt(nonce, plaintext, aad)


def encrypt_request(key: str, dek: bytes, aad: bytes) -> dict[str, Any]:
    return {
        "name": key,
        "plaintext": dek,
        "additional_authenticated_data": aad,
        "plaintext_crc32c": checksum(dek),
        "additional_authenticated_data_crc32c": checksum(aad),
    }


def envelope(response: kms_v1.EncryptResponse, nonce: bytes, ciphertext: bytes) -> str:
    if (
        not response.verified_plaintext_crc32c
        or not response.verified_additional_authenticated_data_crc32c
        or checksum(response.ciphertext) != response.ciphertext_crc32c
    ):
        raise KMSUnavailable("Cloud KMS encryption integrity check failed")
    document = {
        "format": FORMAT,
        "wrapped_key": base64.b64encode(response.ciphertext).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    # Preserve the storage schema/parser, not OpenBao ciphertext compatibility.
    return (
        "vault:v1:"
        + base64.b64encode(json.dumps(document, separators=(",", ":")).encode()).decode()
    )


def parse_ciphertext(payload: dict[str, str]) -> tuple[dict[str, Any], bytes]:
    ciphertext = payload["ciphertext"]
    if not isinstance(ciphertext, str) or not ciphertext.startswith("vault:v1:"):
        raise ValueError("Unsupported ciphertext")
    document = json.loads(decode(ciphertext[len("vault:v1:") :]))
    if (
        not isinstance(document, dict)
        or set(document) != {"format", "wrapped_key", "nonce", "ciphertext"}
        or document["format"] != FORMAT
    ):
        raise ValueError("Not a GCP storage envelope")
    return document, decode(payload["associated_data"])


def decrypt_request(key: str, document: dict[str, Any], aad: bytes) -> dict[str, Any]:
    wrapped_key = decode(document["wrapped_key"])
    return {
        "name": key,
        "ciphertext": wrapped_key,
        "ciphertext_crc32c": checksum(wrapped_key),
        "additional_authenticated_data": aad,
        "additional_authenticated_data_crc32c": checksum(aad),
    }


def plaintext(response: kms_v1.DecryptResponse, document: dict[str, Any], aad: bytes) -> str:
    if checksum(response.plaintext) != response.plaintext_crc32c or len(response.plaintext) != 32:
        raise KMSUnavailable("Cloud KMS decryption integrity check failed")
    nonce = decode(document["nonce"])
    if len(nonce) != 12:
        raise ValueError("Invalid storage nonce")
    value = AESGCM(response.plaintext).decrypt(nonce, decode(document["ciphertext"]), aad)
    return base64.b64encode(value).decode()


FAILURES = (
    GoogleAPICallError,
    GoogleAuthError,
    InvalidTag,
    KeyError,
    TypeError,
    ValueError,
)


class OpenBaoClient:
    def __init__(self) -> None:
        self.key = os.environ["GCP_STORAGE_KEY"]
        self.kms: kms_v1.KeyManagementServiceClient | None = None

    def authenticate(self) -> None:
        if self.kms is None:
            try:
                self.kms = kms_v1.KeyManagementServiceClient()
            except GoogleAuthError:
                raise KMSUnavailable("Cloud workload identity is unavailable") from None

    def __enter__(self) -> Self:
        self.authenticate()
        return self

    def __exit__(self, *args: object) -> None:
        if self.kms is not None:
            self.kms.transport.close()

    def load_keyset(self) -> dict[str, Any]:
        name = os.environ["GCP_EC_KEYSET"]
        if not re.fullmatch(r"projects/[^/]+/secrets/[^/]+/versions/[1-9][0-9]*", name):
            raise KMSUnavailable("EC key set must reference a pinned Secret Manager version")
        try:
            with secretmanager.SecretManagerServiceClient() as client:
                result = client.access_secret_version(
                    request={"name": name}, retry=None, timeout=settings.openbao_timeout_seconds
                )
            data = result.payload.data
            if len(data) > MAX_KEYSET_BYTES or checksum(data) != result.payload.data_crc32c:
                raise ValueError("Invalid EC key set integrity")
            document = json.loads(data)
            if not isinstance(document, dict):
                raise TypeError("Invalid EC key set")
            return document
        except FAILURES:
            raise KMSUnavailable("Cloud EC key set is unavailable or invalid") from None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        max_response_bytes: int = 1_048_576,
    ) -> dict[str, Any]:
        self.authenticate()
        assert self.kms is not None
        try:
            action = operation(method, path)
            if action == "metadata":
                return metadata(
                    self.kms.get_crypto_key(
                        request={"name": self.key},
                        retry=None,
                        timeout=settings.openbao_timeout_seconds,
                    )
                )
            if action == "decrypt" and payload is not None:
                document, aad = parse_ciphertext(payload)
                response = self.kms.decrypt(
                    request=decrypt_request(self.key, document, aad),
                    retry=None,
                    timeout=settings.openbao_timeout_seconds,
                )
                return {"data": {"plaintext": plaintext(response, document, aad)}}
            raise KMSUnavailable("Unsupported synchronous cloud KMS operation")
        except FAILURES:
            raise KMSUnavailable("Cloud KMS operation failed") from None


class AsyncOpenBaoClient:
    def __init__(self) -> None:
        self.key = os.environ["GCP_STORAGE_KEY"]
        self.kms: kms_v1.KeyManagementServiceAsyncClient | None = None
        self.limit = asyncio.Semaphore(16)

    async def __aenter__(self) -> Self:
        try:
            self.kms = kms_v1.KeyManagementServiceAsyncClient()
        except GoogleAuthError:
            raise KMSUnavailable("Cloud workload identity is unavailable") from None
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.kms is not None:
            await self.kms.transport.close()

    async def authenticate(self) -> None:
        if self.kms is None:
            raise KMSUnavailable("Cloud KMS client is not initialized")

    async def seal(self, entry: dict[str, str]) -> dict[str, str]:
        assert self.kms is not None
        async with self.limit:
            dek, aad, nonce, ciphertext = await asyncio.to_thread(prepare, entry)
            response = await self.kms.encrypt(
                request=encrypt_request(self.key, dek, aad),
                retry=None,
                timeout=settings.openbao_timeout_seconds,
            )
            return {"ciphertext": await asyncio.to_thread(envelope, response, nonce, ciphertext)}

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        max_response_bytes: int = 1_048_576,
    ) -> dict[str, Any]:
        await self.authenticate()
        assert self.kms is not None
        try:
            action = operation(method, path)
            if action == "metadata":
                return metadata(
                    await self.kms.get_crypto_key(
                        request={"name": self.key},
                        retry=None,
                        timeout=settings.openbao_timeout_seconds,
                    )
                )
            if (
                action == "encrypt"
                and payload is not None
                and isinstance(payload.get("batch_input"), list)
                and 1 <= len(payload["batch_input"]) <= 500
            ):
                tasks = [asyncio.create_task(self.seal(entry)) for entry in payload["batch_input"]]
                try:
                    results = await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                return {"data": {"batch_results": results}}
            raise KMSUnavailable("Unsupported asynchronous cloud KMS operation")
        except FAILURES:
            raise KMSUnavailable("Cloud KMS operation failed") from None
