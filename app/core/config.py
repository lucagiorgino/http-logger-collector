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
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:28379/0")
    redis_stream: str = os.getenv("REDIS_STREAM", "traffic-log-batches")
    redis_group: str = os.getenv("REDIS_GROUP", "mongo-writers")
    redis_max_batches: int = int(os.getenv("REDIS_MAX_BATCHES", "10000"))
    redis_socket_timeout_seconds: float = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "3"))
    redis_aof_timeout_ms: int = int(os.getenv("REDIS_AOF_TIMEOUT_MS", "2000"))
    worker_reclaim_idle_ms: int = int(os.getenv("WORKER_RECLAIM_IDLE_MS", "60000"))
    worker_write_timeout_seconds: float = float(os.getenv("WORKER_WRITE_TIMEOUT_SECONDS", "30"))
    worker_retry_seconds: float = float(os.getenv("WORKER_RETRY_SECONDS", "1"))
    auth0_domain: str | None = os.getenv("AUTH0_DOMAIN")
    auth0_audience: str = os.getenv("AUTH0_AUDIENCE", "")
    app_env: str = os.getenv("APP_ENV", "development")
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8001"))
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

    def validate_ingestion(self) -> None:
        for name, value in (
            ("REDIS_MAX_BATCHES", self.redis_max_batches),
            ("REDIS_SOCKET_TIMEOUT_SECONDS", self.redis_socket_timeout_seconds),
            ("REDIS_AOF_TIMEOUT_MS", self.redis_aof_timeout_ms),
            ("WORKER_RECLAIM_IDLE_MS", self.worker_reclaim_idle_ms),
            ("WORKER_WRITE_TIMEOUT_SECONDS", self.worker_write_timeout_seconds),
            ("WORKER_RETRY_SECONDS", self.worker_retry_seconds),
        ):
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.redis_aof_timeout_ms >= self.redis_socket_timeout_seconds * 1000:
            raise ValueError("REDIS_AOF_TIMEOUT_MS must be below the Redis socket timeout")
        if (
            self.worker_reclaim_idle_ms
            <= (self.worker_write_timeout_seconds + 2 * self.redis_socket_timeout_seconds) * 1000
        ):
            raise ValueError(
                "WORKER_RECLAIM_IDLE_MS must exceed write and acknowledgement timeouts"
            )
        if not self.redis_stream.strip() or not self.redis_group.strip():
            raise ValueError("REDIS_STREAM and REDIS_GROUP must be nonblank")
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use redis:// or rediss://")

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
