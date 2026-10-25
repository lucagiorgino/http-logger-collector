import gzip
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx
import pytest


@pytest.fixture
def proxy_log():
    with tempfile.TemporaryFile() as log:
        yield log


@pytest.mark.skipif(
    os.getenv("PROXY_TEST_ENABLED") != "true",
    reason="Set PROXY_TEST_ENABLED=true for Docker NGINX integration",
)
def test_nginx_routes_preserves_uploads_and_replaces_untrusted_forwarding_headers(proxy_log):
    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            if self.path == "/auth/login?large_cookies=true":
                cookies = [
                    f"transaction.{index}={'x' * 2000}; Path=/; HttpOnly; SameSite=Lax"
                    for index in range(3)
                ]
                self.send_response(307)
                self.send_header("Location", "https://auth.example.test/authorize")
                for cookie in cookies:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("X-Middleware-Set-Cookie", ",".join(cookies))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("Upgrade") == "websocket":
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            result = json.dumps(
                {
                    "upstream": self.server.upstream_name,
                    "path": self.path,
                    "host": self.headers.get("Host"),
                    "forwarded_host": self.headers.get("X-Forwarded-Host"),
                    "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
                    "forwarded_for": self.headers.get("X-Forwarded-For"),
                    "forwarded": self.headers.get("Forwarded"),
                    "content_encoding": self.headers.get("Content-Encoding"),
                    "body_sha256": sha256(body).hexdigest(),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *_):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    frontend = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    backend.upstream_name = "backend"
    frontend.upstream_name = "frontend"
    for server in (backend, frontend):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        proxy_port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    desktop = "Docker Desktop" in subprocess.check_output(
        ["docker", "info", "--format", "{{.OperatingSystem}}"], text=True
    )
    compose_files = ["-f", str(root / "docker/docker-compose.yml")]
    if not desktop:
        compose_files += ["-f", str(root / "docker/docker-compose.host.yml")]
    name = "logger-proxy-test-" + uuid4().hex
    environment = {
        **os.environ,
        "APP_PORT": str(backend.server_port),
        "FRONTEND_PORT": str(frontend.server_port),
        "PROXY_PORT": str(proxy_port),
        "PROXY_BIND_HOST": "127.0.0.1",
        "PROXY_EXTERNAL_SCHEME": "https",
    }
    log = proxy_log
    process = subprocess.Popen(
        [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(root / ".env.example"),
            *compose_files,
            "run",
            "--rm",
            "--no-deps",
            "--service-ports",
            "--name",
            name,
            "proxy",
        ],
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{proxy_port}", timeout=5) as client:
            deadline = time.monotonic() + 45
            while True:
                assert process.poll() is None, "NGINX exited during startup"
                try:
                    if client.get("/proxy-health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    log.seek(0)
                    pytest.fail("NGINX did not become ready:\n" + log.read().decode())
                time.sleep(0.1)
            for path in (
                "/api/v1/me",
                "/api/v1/devices/literal%2540device",
                "/docs",
                "/docs/oauth2-redirect",
                "/redoc",
                "/openapi.json",
                "/health",
            ):
                response = client.get(path)
                assert response.status_code == 200
                assert response.json()["upstream"] == "backend"
                assert response.json()["path"] == path
            for path in ("/", "/overview", "/auth/login", "/_next/static/app.js", "/api/v10"):
                assert client.get(path).json()["upstream"] == "frontend"
            login = client.get("/auth/login?large_cookies=true")
            assert login.status_code == 307
            assert login.headers["location"] == "https://auth.example.test/authorize"
            assert login.headers.get_list("set-cookie") == [
                f"transaction.{index}={'x' * 2000}; Path=/; HttpOnly; SameSite=Lax"
                for index in range(3)
            ]
            for path in ("/api/v1/me", "/auth/login"):
                data = client.get(
                    path,
                    headers={
                        "Host": "public.example:8080",
                        "X-Forwarded-Host": "untrusted.example",
                        "X-Forwarded-Proto": "http",
                        "X-Forwarded-For": "198.51.100.99",
                        "Forwarded": "for=198.51.100.99;proto=http",
                    },
                ).json()
                assert data["host"] == data["forwarded_host"] == "public.example:8080"
                assert data["forwarded_proto"] == "https"
                assert str(ip_address(data["forwarded_for"])) != "198.51.100.99"
                assert data["forwarded"] is None
            body = gzip.compress(b'{"logs":[{"body":"preserved"}]}')
            data = client.post(
                "/api/v1/traffic-log-batches",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            ).json()
            assert data["content_encoding"] == "gzip"
            assert data["body_sha256"] == sha256(body).hexdigest()
            envelope = b"protected..iv.ciphertext.tag"
            data = client.post(
                "/api/v1/traffic-log-batches",
                content=envelope,
                headers={
                    "Content-Type": "application/jose",
                },
            ).json()
            assert data["body_sha256"] == sha256(envelope).hexdigest()
            assert (
                client.post("/api/v1/traffic-log-batches", content=b"x" * 1_500_001).status_code
                == 413
            )
            response = client.get(
                "/_next/webpack-hmr",
                headers={
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                },
            )
            assert response.status_code == 101
            client.get("/auth/callback?code=private-test-code")
    finally:
        subprocess.run(
            ["docker", "stop", "--timeout", "5", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            for server in (backend, frontend):
                server.shutdown()
                server.server_close()
        log.seek(0)
        output = log.read().decode()
        assert "private-test-code" not in output
