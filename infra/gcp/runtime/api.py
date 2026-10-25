"""Accept attribution only from headers overwritten by the managed load balancer."""

import os
from ipaddress import ip_address

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.main import app as application

from .configuration import validate_cloud_connections


class EdgeHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            validate_cloud_connections()
        if scope["type"] == "http" and scope["path"].startswith("/api/v1/"):
            headers = dict(scope["headers"])
            try:
                address = str(ip_address(headers[b"x-ermes-client-ip"].decode("ascii")))
                if (
                    headers[b"x-ermes-edge-ip"].decode("ascii") != os.environ["GCP_EDGE_IP"]
                    or headers[b"x-forwarded-proto"] != b"https"
                ):
                    raise ValueError()
            except (KeyError, UnicodeError, ValueError):
                await JSONResponse(
                    status_code=400, content={"detail": "Trusted HTTPS ingress is required"}
                )(scope, receive, send)
                return
            scope = {**scope, "scheme": "https", "client": (address, 0)}
        await self.app(scope, receive, send)


app = EdgeHeaders(application)
