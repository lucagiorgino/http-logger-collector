import re
from functools import lru_cache

from joserfc import jwe
from joserfc.errors import JoseError
from joserfc.jwk import ECKey

from app.core.config import settings
from app.core.openbao import KMSUnavailable, OpenBaoClient

KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
MAX_ENCRYPTED_BYTES = 1_500_000


class InvalidEnvelope(ValueError):
    pass


class EncryptionKeys:
    def __init__(self, keys: dict[str, ECKey], active_key_id: str = "") -> None:
        settings.validate_encryption_suite()
        if bool(keys) != bool(active_key_id) or (keys and active_key_id not in keys):
            raise ValueError("Encryption requires a key set and a matching active key ID")
        self.keys = keys
        self.active_key_id = active_key_id
        self.algorithm = settings.encryption_key_algorithm
        self.encryption = settings.encryption_content_algorithm
        self.curve = settings.encryption_curve
        if any(
            not key.is_private or key.as_dict(private=False).get("crv") != self.curve
            for key in keys.values()
        ):
            raise ValueError("Private encryption keys must match ENCRYPTION_CURVE")
        self.registry = jwe.JWERegistry(algorithms=[self.algorithm, self.encryption])
        self.registry.max_ciphertext_length = MAX_ENCRYPTED_BYTES

    @property
    def public_key(self) -> dict[str, str] | None:
        if not self.active_key_id:
            return None
        public = self.keys[self.active_key_id].as_dict(private=False)
        return {
            "kty": "EC",
            "kid": self.active_key_id,
            "use": "enc",
            "alg": self.algorithm,
            "crv": self.curve,
            "x": str(public["x"]),
            "y": str(public["y"]),
        }

    def decrypt(self, value: bytes) -> bytes:
        try:
            envelope = jwe.extract_compact(value, self.registry)
            header = envelope.protected
            if not isinstance(header, dict) or set(header) - {
                "alg",
                "enc",
                "kid",
                "cty",
                "typ",
                "epk",
            }:
                raise InvalidEnvelope()
            if (
                header.get("alg") != self.algorithm
                or header.get("enc") != self.encryption
                or header.get("cty") != "application/gzip"
                or header.get("typ", "JWE") != "JWE"
            ):
                raise InvalidEnvelope()
            ephemeral = header.get("epk")
            if (
                not isinstance(ephemeral, dict)
                or set(ephemeral) != {"kty", "crv", "x", "y"}
                or ephemeral.get("kty") != "EC"
                or ephemeral.get("crv") != self.curve
            ):
                raise InvalidEnvelope()
            kid = header.get("kid")
            if not isinstance(kid, str) or kid not in self.keys:
                raise InvalidEnvelope()
            plaintext = jwe.decrypt_compact(value, self.keys[kid], registry=self.registry).plaintext
            if not isinstance(plaintext, bytes):
                raise InvalidEnvelope()
            return plaintext
        except (JoseError, ValueError, TypeError) as exc:
            raise InvalidEnvelope("Invalid encrypted batch") from exc


def parse_keyset(document: dict) -> EncryptionKeys:
    settings.validate_encryption_suite()
    try:
        entries = document["keys"]
        if not isinstance(entries, list) or not entries:
            raise ValueError()
        keys = {}
        for entry in entries:
            kid = entry.get("kid")
            if (
                not isinstance(kid, str)
                or not KEY_ID.fullmatch(kid)
                or kid in keys
                or entry.get("kty") != "EC"
                or entry.get("crv") != settings.encryption_curve
                or entry.get("alg", settings.encryption_key_algorithm)
                != settings.encryption_key_algorithm
                or entry.get("use", "enc") != "enc"
            ):
                raise ValueError()
            operations = entry.get("key_ops", ["deriveKey"])
            if (
                not isinstance(operations, list)
                or not operations
                or set(operations) - {"deriveKey", "deriveBits"}
            ):
                raise ValueError()
            keys[kid] = ECKey.import_key(entry)
        active_key_id = document["active_key_id"]
        if not isinstance(active_key_id, str) or not KEY_ID.fullmatch(active_key_id):
            raise ValueError()
        return EncryptionKeys(keys, active_key_id)
    except (JoseError, ValueError, KeyError, TypeError, AttributeError):
        raise KMSUnavailable("Invalid OpenBao encryption key set") from None


@lru_cache
def get_encryption_keys() -> EncryptionKeys:
    settings.validate_encryption_suite()
    with OpenBaoClient() as client:
        return parse_keyset(client.load_keyset())
