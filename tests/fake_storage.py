"""In-memory AES-GCM Transit fixture; never used by application entry points."""

import base64
import json
import os

import httpx2 as httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.openbao import OpenBaoClient
from app.core.storage_encryption import StorageEncryption


class FakeStorageEncryption(StorageEncryption):
    def __init__(self):
        self.cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def client(self):
        return OpenBaoClient("test-storage-token", transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        assert request.headers["X-Vault-Token"] == "test-storage-token"
        base = f"/v1/{settings.openbao_transit_mount}"
        if (
            request.method == "GET"
            and request.url.path == f"{base}/keys/{settings.openbao_storage_key}"
        ):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "type": "aes256-gcm96",
                        "exportable": False,
                        "allow_plaintext_backup": False,
                        "derived": False,
                        "convergent_encryption": False,
                    }
                },
            )
        payload = json.loads(request.content)
        if request.url.path == f"{base}/encrypt/{settings.openbao_storage_key}":
            results = []
            for entry in payload["batch_input"]:
                nonce = os.urandom(12)
                encrypted = self.cipher.encrypt(
                    nonce,
                    base64.b64decode(entry["plaintext"]),
                    base64.b64decode(entry["associated_data"]),
                )
                results.append(
                    {"ciphertext": "vault:v1:" + base64.b64encode(nonce + encrypted).decode()}
                )
            return httpx.Response(200, json={"data": {"batch_results": results}})
        assert request.url.path == f"{base}/decrypt/{settings.openbao_storage_key}"
        encrypted = base64.b64decode(payload["ciphertext"].split(":", 2)[2])
        try:
            plaintext = self.cipher.decrypt(
                encrypted[:12], encrypted[12:], base64.b64decode(payload["associated_data"])
            )
        except InvalidTag:
            return httpx.Response(400, json={"errors": ["test authentication failure"]})
        return httpx.Response(
            200, json={"data": {"plaintext": base64.b64encode(plaintext).decode()}}
        )
