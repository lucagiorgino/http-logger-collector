import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.core.openbao import AsyncOpenBaoClient, KMSUnavailable, OpenBaoClient
from app.schemas.traffic import TrafficLogRequest
from app.schemas.workspace import StoragePolicy

ENCRYPTED = "[ENCRYPTED]"
MAX_TRANSIT_BYTES = 16_777_216
CIPHERTEXT = re.compile(r"vault:v[1-9][0-9]*:[A-Za-z0-9+/=]+")


def validate_storage_key(client: OpenBaoClient) -> None:
    client.authenticate()
    response = client.request(
        "GET", f"{settings.openbao_transit_mount}/keys/{settings.openbao_storage_key}"
    )
    check_storage_key(response)


def check_storage_key(response: dict[str, Any] | None) -> None:
    key = (response or {}).get("data", {})
    if (
        not isinstance(key, dict)
        or key.get("type") != "aes256-gcm96"
        or key.get("exportable") is not False
        or key.get("allow_plaintext_backup") is not False
        or key.get("derived", False) is not False
        or key.get("convergent_encryption", False) is not False
    ):
        raise KMSUnavailable("Storage key must be non-exportable, randomized AES-256-GCM")


class SensitiveFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str | None = Field(default=None, max_length=1_000_000)
    request_headers: dict[str, str] = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(default_factory=dict)


class StorageEncryption:
    def __init__(self, client: AsyncOpenBaoClient | None = None) -> None:
        self._async_client = client

    def client(self) -> OpenBaoClient:
        return OpenBaoClient()

    @asynccontextmanager
    async def async_client(self) -> AsyncIterator[AsyncOpenBaoClient]:
        if self._async_client is not None:
            yield self._async_client
        else:
            async with AsyncOpenBaoClient() as client:
                yield client

    def validate(self) -> None:
        with self.client() as client:
            validate_storage_key(client)

    @staticmethod
    def associated_data(document: dict[str, Any]) -> str:
        value = {
            "purpose": "traffic-sensitive-fields-v1",
            "record_id": str(document["_id"]),
            "device_id": document["metadata"]["device_id"],
            "batch_id": document["batch_id"],
            "fields": document["storage_encryption"]["fields"],
        }
        return base64.b64encode(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).decode()

    async def encrypt(self, entries: list[dict[str, str]]) -> list[str]:
        async with self.async_client() as client:
            await client.authenticate()
            check_storage_key(
                await client.request(
                    "GET", f"{settings.openbao_transit_mount}/keys/{settings.openbao_storage_key}"
                )
            )
            response = await client.request(
                "POST",
                f"{settings.openbao_transit_mount}/encrypt/{settings.openbao_storage_key}",
                {"batch_input": entries},
                max_response_bytes=MAX_TRANSIT_BYTES,
            )
        try:
            results = response["data"]["batch_results"]
            if not isinstance(results, list) or len(results) != len(entries):
                raise ValueError()
            ciphertexts = []
            for result in results:
                ciphertext = result["ciphertext"]
                if (
                    result.get("error")
                    or not isinstance(ciphertext, str)
                    or not CIPHERTEXT.fullmatch(ciphertext)
                ):
                    raise ValueError()
                ciphertexts.append(ciphertext)
            return ciphertexts
        except (KeyError, TypeError, ValueError):
            raise KMSUnavailable("Invalid OpenBao storage encryption response") from None

    def prepare(
        self,
        documents: list[dict[str, Any]],
        payloads: list[TrafficLogRequest],
        policy: StoragePolicy,
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        entries = []
        protected = []
        for document, payload in zip(documents, payloads, strict=True):
            values: dict[str, Any] = {}
            fields = []
            if not policy.retain_request_body:
                document.pop("body", None)
            elif payload.body is not None:
                if policy.encrypt_request_body:
                    values["body"] = payload.body
                    fields.append("body")
                else:
                    document["body"] = payload.body
            for name, source in (
                ("request_headers", payload.request_headers),
                ("response_headers", payload.response_headers),
            ):
                for header, value in source.items():
                    lowered = header.lower()
                    category = (
                        "authorization"
                        if lowered in {"authorization", "proxy-authorization"}
                        else "cookies"
                        if lowered in {"cookie", "set-cookie"}
                        else None
                    )
                    if category is None:
                        continue
                    retain = (
                        policy.retain_authorization_header
                        if category == "authorization"
                        else policy.retain_cookies
                    )
                    if not retain:
                        document[name].pop(header, None)
                        continue
                    enabled = (
                        policy.encrypt_authorization_header
                        if category == "authorization"
                        else policy.encrypt_cookies
                    )
                    if enabled:
                        values.setdefault(name, {})[header] = value
                        document[name][header] = ENCRYPTED
                        if category not in fields:
                            fields.append(category)
                    else:
                        document[name][header] = value
            if values:
                document["storage_encryption"] = {"version": 1, "fields": sorted(fields)}
                entries.append(
                    {
                        "plaintext": base64.b64encode(
                            json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
                        ).decode(),
                        "associated_data": self.associated_data(document),
                    }
                )
                protected.append(document)
        return entries, protected

    async def protect(
        self,
        documents: list[dict[str, Any]],
        payloads: list[TrafficLogRequest],
        policy: StoragePolicy,
    ) -> None:
        entries, protected = await asyncio.to_thread(self.prepare, documents, payloads, policy)
        if entries:
            ciphertexts = await self.encrypt(entries)
            for document, ciphertext in zip(protected, ciphertexts, strict=True):
                document["storage_encryption"]["ciphertext"] = ciphertext

    def decrypt(self, document: dict[str, Any]) -> dict[str, Any]:
        envelope = document.get("storage_encryption")
        if envelope is None:
            return document
        try:
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"version", "fields", "ciphertext"}
                or envelope["version"] != 1
                or not isinstance(envelope["fields"], list)
                or not envelope["fields"]
                or any(
                    field not in {"body", "authorization", "cookies"}
                    for field in envelope["fields"]
                )
                or not isinstance(envelope["ciphertext"], str)
                or len(envelope["ciphertext"]) > MAX_TRANSIT_BYTES
                or not CIPHERTEXT.fullmatch(envelope["ciphertext"])
            ):
                raise ValueError()
            associated_data = self.associated_data(document)
        except (KeyError, TypeError, ValueError):
            raise KMSUnavailable("Invalid storage encryption envelope") from None
        with self.client() as client:
            validate_storage_key(client)
            response = client.request(
                "POST",
                f"{settings.openbao_transit_mount}/decrypt/{settings.openbao_storage_key}",
                {"ciphertext": envelope["ciphertext"], "associated_data": associated_data},
                max_response_bytes=MAX_TRANSIT_BYTES,
            )
        try:
            encoded = response["data"]["plaintext"]
            if not isinstance(encoded, str) or len(encoded) > MAX_TRANSIT_BYTES:
                raise ValueError()
            values = SensitiveFields.model_validate_json(base64.b64decode(encoded, validate=True))
            result = {**document}
            if "body" in values.model_fields_set:
                result["body"] = values.body
            for name in ("request_headers", "response_headers"):
                result[name] = {**document.get(name, {}), **getattr(values, name)}
            return result
        except (KeyError, TypeError, ValueError, ValidationError):
            raise KMSUnavailable("Invalid OpenBao storage plaintext") from None


async def get_storage_encryption(request: Request) -> StorageEncryption:
    return request.app.state.storage_encryption
