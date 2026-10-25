import os
from math import isfinite
from pathlib import Path

from dotenv import load_dotenv

env_file = os.getenv("ENV_FILE")
if env_file is not None and not Path(env_file).is_file():
    raise ValueError(f"ENV_FILE does not exist: {env_file}")
load_dotenv(env_file or ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


class Settings:
    mongo_uri: str = os.getenv(
        "MONGO_URI",
        f"mongodb://{os.getenv('MONGO_HOST', 'localhost')}:{os.getenv('MONGO_PORT', '28017')}"
        f"/?replicaSet={os.getenv('MONGO_REPLICA_SET', 'rs0')}",
    )
    mongo_db_name: str = os.getenv("MONGO_DB_NAME", "amazing_http_logger")
    mongo_server_selection_timeout_ms: int = int(
        os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")
    )
    mongo_allow_primary_reads: bool = _env_bool("MONGO_ALLOW_PRIMARY_READS")
    worker_retry_seconds: float = float(os.getenv("WORKER_RETRY_SECONDS", "1"))
    spool_bucket: str = os.getenv("SPOOL_BUCKET", "")
    spool_storage_backend: str = os.getenv("SPOOL_STORAGE_BACKEND", "gcs")
    spool_endpoint_url: str | None = os.getenv("SPOOL_ENDPOINT_URL") or None
    spool_subscription: str = os.getenv("SPOOL_SUBSCRIPTION", "")
    spool_parse_dlq_topic: str = os.getenv("SPOOL_PARSE_DLQ_TOPIC", "")
    spool_load_dlq_topic: str = os.getenv("SPOOL_LOAD_DLQ_TOPIC", "")
    spool_event_topic: str = os.getenv("SPOOL_EVENT_TOPIC", "")
    spool_minio_access_key_file: str = os.getenv(
        "SPOOL_MINIO_ACCESS_KEY_FILE", ".secrets/minio-access-key"
    )
    spool_minio_secret_key_file: str = os.getenv(
        "SPOOL_MINIO_SECRET_KEY_FILE", ".secrets/minio-secret-key"
    )
    spool_tenant: str = os.getenv("SPOOL_TENANT", "deployment")
    spool_silver_topic: str = os.getenv("SPOOL_SILVER_TOPIC", "")
    spool_silver_subscription: str = os.getenv("SPOOL_SILVER_SUBSCRIPTION", "")
    spool_lease_seconds: int = int(os.getenv("SPOOL_LEASE_SECONDS", "180"))
    spool_processing_seconds: int = int(os.getenv("SPOOL_PROCESSING_SECONDS", "60"))
    spool_max_attempts: int = int(os.getenv("SPOOL_MAX_ATTEMPTS", "3"))
    spool_mongo_documents_per_second: int = int(
        os.getenv("SPOOL_MONGO_DOCUMENTS_PER_SECOND", "500")
    )
    auth0_domain: str | None = os.getenv("AUTH0_DOMAIN")
    auth0_audience: str = os.getenv("AUTH0_AUDIENCE", "")
    app_env: str = os.getenv("APP_ENV", "development")
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "9090"))
    app_reload: bool = _env_bool("APP_RELOAD")
    require_https: bool = _env_bool("REQUIRE_HTTPS", app_env != "development")
    openbao_addr: str = os.getenv("OPENBAO_ADDR", "http://localhost:28200")
    openbao_token_file: str = os.getenv("OPENBAO_TOKEN_FILE", ".secrets/openbao-token")
    openbao_ca_cert: str = os.getenv("OPENBAO_CA_CERT", "")
    openbao_timeout_seconds: float = float(os.getenv("OPENBAO_TIMEOUT_SECONDS", "5"))
    openbao_transit_mount: str = os.getenv("OPENBAO_TRANSIT_MOUNT", "transit")
    openbao_transit_key: str = os.getenv("OPENBAO_TRANSIT_KEY", "log-keyset")
    openbao_storage_key: str = os.getenv("OPENBAO_STORAGE_KEY", "log-storage")
    openbao_kv_mount: str = os.getenv("OPENBAO_KV_MOUNT", "secret")
    openbao_kv_path: str = os.getenv("OPENBAO_KV_PATH", "log-encryption")
    openbao_kubernetes_role: str = os.getenv("OPENBAO_KUBERNETES_ROLE", "")
    openbao_auth_mount: str = os.getenv("OPENBAO_AUTH_MOUNT", "kubernetes")
    openbao_jwt_file: str = os.getenv("OPENBAO_JWT_FILE", "/run/secrets/openbao-auth/token")
    openbao_dev_root_token_file: str = os.getenv(
        "OPENBAO_DEV_ROOT_TOKEN_FILE", ".secrets/openbao-dev-root-token"
    )
    encryption_key_algorithm: str = os.getenv("ENCRYPTION_KEY_ALGORITHM", "ECDH-ES+A256KW")
    encryption_content_algorithm: str = os.getenv("ENCRYPTION_CONTENT_ALGORITHM", "A256GCM")
    encryption_curve: str = os.getenv("ENCRYPTION_CURVE", "P-384")

    def validate_spool(self) -> None:
        for name in (
            "bucket",
            "subscription",
            "parse_dlq_topic",
            "load_dlq_topic",
            "event_topic",
            "silver_topic",
            "silver_subscription",
            "tenant",
        ):
            if not getattr(self, f"spool_{name}").strip():
                raise ValueError(f"SPOOL_{name.upper()} is required")
        for name in (
            "lease_seconds",
            "processing_seconds",
            "max_attempts",
            "mongo_documents_per_second",
        ):
            if getattr(self, f"spool_{name}") <= 0:
                raise ValueError(f"SPOOL_{name.upper()} must be positive")
        if self.spool_lease_seconds <= self.spool_processing_seconds + 30:
            raise ValueError("SPOOL_LEASE_SECONDS must exceed processing timeout plus 30 seconds")
        if not isfinite(self.worker_retry_seconds) or self.worker_retry_seconds <= 0:
            raise ValueError("WORKER_RETRY_SECONDS must be positive and finite")
        if self.spool_silver_subscription == self.spool_subscription:
            raise ValueError("Object and silver subscriptions must be separate")
        if (
            len(
                {
                    self.spool_event_topic,
                    self.spool_silver_topic,
                    self.spool_parse_dlq_topic,
                    self.spool_load_dlq_topic,
                }
            )
            != 4
        ):
            raise ValueError("Object, silver and DLQ topics must be separate")
        if self.spool_storage_backend not in {"gcs", "minio"}:
            raise ValueError("SPOOL_STORAGE_BACKEND must be gcs or minio")
        if self.spool_storage_backend == "minio":
            if self.app_env != "development" or not self.spool_endpoint_url:
                raise ValueError("MinIO requires development and SPOOL_ENDPOINT_URL")
        elif self.spool_endpoint_url or os.getenv("PUBSUB_EMULATOR_HOST"):
            raise ValueError("GCS production storage must not use local emulator endpoints")
        if self.spool_lease_seconds + 30 > 600:
            raise ValueError("Pub/Sub acknowledgement deadline must not exceed 600 seconds")

    def validate_encryption_suite(self) -> None:
        if self.encryption_key_algorithm not in {
            "ECDH-ES+A128KW",
            "ECDH-ES+A192KW",
            "ECDH-ES+A256KW",
        }:
            raise ValueError("ENCRYPTION_KEY_ALGORITHM must use ECDH-ES with AES key wrapping")
        if self.encryption_content_algorithm not in {"A128GCM", "A192GCM", "A256GCM"}:
            raise ValueError("ENCRYPTION_CONTENT_ALGORITHM must use AES-GCM")
        if self.encryption_curve not in {"P-256", "P-384", "P-521"}:
            raise ValueError("ENCRYPTION_CURVE must be a supported NIST prime-field curve")


settings = Settings()
