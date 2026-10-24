import os
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
    auth0_domain: str | None = os.getenv("AUTH0_DOMAIN")
    auth0_audience: str = os.getenv("AUTH0_AUDIENCE", "")
    app_env: str = os.getenv("APP_ENV", "development")
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8001"))
    app_reload: bool = _env_bool("APP_RELOAD")


settings = Settings()
