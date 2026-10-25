"""Handle HTTPS and client attribution at the direct Cloud Run ingress."""

from ipaddress import ip_address

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.main import app as application

from .configuration import validate_cloud_connections


class CloudRunHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            validate_cloud_connections()
        if scope["type"] == "http" and scope["path"].startswith("/api/v1/"):
            protocols = [value for key, value in scope["headers"] if key == b"x-forwarded-proto"]
            forwarded = [value for key, value in scope["headers"] if key == b"x-forwarded-for"]
            try:
                if protocols != [b"https"] or len(forwarded) != 1:
                    raise ValueError()
                # Cloud Run appends the connecting address; earlier entries can be caller supplied.
                address = str(ip_address(forwarded[0].decode("ascii").rsplit(",", 1)[-1].strip()))
            except (UnicodeError, ValueError):
                await JSONResponse(
                    status_code=400, content={"detail": "Cloud Run HTTPS ingress is required"}
                )(scope, receive, send)
                return
            scope = {**scope, "scheme": "https", "client": (address, 0)}

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"strict-transport-security", b"max-age=31536000"),
                ]
            await send(message)

        await self.app(scope, receive, secured_send)


app = CloudRunHeaders(application)
