"""Local-only MinIO notifications -> Pub/Sub emulator bridge and provisioning."""

import argparse
import asyncio
import json
import logging
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from google.api_core.exceptions import AlreadyExists, GoogleAPICallError
from google.cloud import pubsub_v1

from app.core.config import settings
from app.services.upload_cloud import UploadCloud

logger = logging.getLogger(__name__)


def prepare() -> None:
    for name in (settings.spool_minio_access_key_file, settings.spool_minio_secret_key_file):
        path = Path(name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(24))


def emulator_only() -> None:
    if settings.app_env != "development" or not os.getenv("PUBSUB_EMULATOR_HOST"):
        raise ValueError("This command requires development and PUBSUB_EMULATOR_HOST")


def initialize() -> None:
    emulator_only()
    cloud = UploadCloud()
    try:
        for topic in (
            settings.spool_event_topic,
            settings.spool_silver_topic,
            settings.spool_parse_dlq_topic,
            settings.spool_load_dlq_topic,
        ):
            try:
                cloud.publisher.create_topic(request={"name": topic}, timeout=15)
            except AlreadyExists:
                pass
        for subscription, topic in (
            (settings.spool_subscription, settings.spool_event_topic),
            (settings.spool_silver_subscription, settings.spool_silver_topic),
            (settings.spool_parse_dlq_topic + "-inspection", settings.spool_parse_dlq_topic),
            (settings.spool_load_dlq_topic + "-inspection", settings.spool_load_dlq_topic),
        ):
            # Pub/Sub subscription resource paths differ from topic paths.
            subscription = subscription.replace("/topics/", "/subscriptions/")
            try:
                cloud.subscriber.create_subscription(
                    request={
                        "name": subscription,
                        "topic": topic,
                        "ack_deadline_seconds": settings.spool_lease_seconds + 30,
                        "message_retention_duration": {"seconds": 1209600},
                    },
                    timeout=15,
                )
            except AlreadyExists:
                pass
    finally:
        asyncio.run(cloud.close())


def bridge() -> None:
    emulator_only()
    publisher = pubsub_v1.PublisherClient()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def do_POST(self) -> None:
            length = self.headers.get("Content-Length", "")
            if self.path != "/events" or not length.isdigit() or not 0 < int(length) <= 65536:
                self.send_error(400)
                return
            try:
                event = json.loads(self.rfile.read(int(length)))
                if not isinstance(event, dict) or not isinstance(event.get("Records"), list):
                    raise TypeError()
            except (ValueError, TypeError):
                self.send_error(400)
                return
            try:
                publisher.publish(
                    settings.spool_event_topic,
                    json.dumps(event).encode(),
                ).result(timeout=15)
            except (TimeoutError, GoogleAPICallError):
                logger.error("Local notification publishing timed out")
                self.send_error(503)
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args) -> None:
            # Notification bodies/credentials must not enter access logs.
            logger.info("Local notification bridge HTTP response")

    server = ThreadingHTTPServer(("0.0.0.0", 9091), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        publisher.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "init", "bridge"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    {"prepare": prepare, "init": initialize, "bridge": bridge}[args.action]()


if __name__ == "__main__":
    main()
