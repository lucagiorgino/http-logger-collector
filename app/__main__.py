import uvicorn

from app.core.config import settings


def main() -> None:
    if settings.app_reload and settings.app_env != "development":
        raise ValueError("APP_RELOAD is restricted to development")
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_reload,
        reload_dirs=["app"] if settings.app_reload else None,
    )


if __name__ == "__main__":
    main()
