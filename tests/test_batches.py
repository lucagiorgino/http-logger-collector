import gzip
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api import batches
from app.db.mongo import PersistenceError
from app.deps import get_ingestion_publisher
from app.main import app
from app.schemas.traffic import BatchReceipt

URL = "/api/v1/traffic-log-batches"
HEADERS = {"Content-Encoding": "gzip", "Content-Type": "application/json"}


def batch_body(*logs):
    return gzip.compress(json.dumps({"logs": logs}).encode())


def test_compressed_batch_is_normalized_before_durable_acceptance(
    authorized_client, repository, traffic_payload
):
    traffic_payload["headers"]["User-Agent"] = "TestAgent"
    response = authorized_client.post(
        URL, content=batch_body(traffic_payload, traffic_payload), headers=HEADERS
    )
    assert response.status_code == 202
    receipt = response.json()
    assert receipt["accepted_count"] == 2
    assert receipt["status"] == "stored"
    repository.insert_traffic_logs.assert_awaited_once()
    documents = repository.insert_traffic_logs.call_args.args[0]
    assert len({document["_id"] for document in documents}) == 2
    for document in documents:
        assert document["batch_id"] == receipt["batch_id"]
        assert document["metadata"] == {"user_id": "user-123", "device_id": "device-456"}
        assert document["timestamp"].isoformat() == "2026-01-15T08:00:00+00:00"
        assert document["method"] == "GET"
        assert document["headers"] == {
            "Authorization": "[REDACTED]",
            "Cookie": "[REDACTED]",
            "User-Agent": "TestAgent",
        }
        assert "body" not in document
        assert document["body_length"] == len(traffic_payload["body"].encode())
        assert document["body_sha256"]


def test_batch_ownership_is_checked_before_any_write(
    authorized_client, repository, traffic_payload
):
    other_user = {**traffic_payload, "user_id": "another-user"}
    response = authorized_client.post(
        URL, content=batch_body(traffic_payload, other_user), headers=HEADERS
    )
    assert response.status_code == 403
    repository.insert_traffic_logs.assert_not_awaited()


@pytest.mark.parametrize("content", [b"not gzip", gzip.compress(b"{}")[:-1], b""])
def test_invalid_gzip_is_rejected(authorized_client, repository, content):
    response = authorized_client.post(URL, content=content, headers=HEADERS)
    assert response.status_code == 400
    repository.insert_traffic_logs.assert_not_awaited()


def test_gzip_trailing_data_and_multiple_members_are_rejected(authorized_client, traffic_payload):
    body = batch_body(traffic_payload)
    for suffix in (b"trailing", gzip.compress(b"{}")):
        response = authorized_client.post(URL, content=body + suffix, headers=HEADERS)
        assert response.status_code == 400


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json"},
        {"Content-Encoding": "br", "Content-Type": "application/json"},
        {"Content-Encoding": "gzip", "Content-Type": "text/plain"},
    ],
)
def test_batch_requires_supported_media_type(authorized_client, traffic_payload, headers):
    response = authorized_client.post(URL, content=batch_body(traffic_payload), headers=headers)
    assert response.status_code == 415


@pytest.mark.parametrize("limit", ["MAX_COMPRESSED_BYTES", "MAX_DECOMPRESSED_BYTES"])
def test_compressed_and_expanded_limits_are_enforced(
    authorized_client, repository, traffic_payload, monkeypatch, limit
):
    monkeypatch.setattr(batches, limit, 32)
    response = authorized_client.post(URL, content=batch_body(traffic_payload), headers=HEADERS)
    assert response.status_code == 413
    repository.insert_traffic_logs.assert_not_awaited()


@pytest.mark.parametrize("count", [0, 501])
def test_batch_size_is_bounded(authorized_client, repository, traffic_payload, count):
    response = authorized_client.post(
        URL, content=batch_body(*([traffic_payload] * count)), headers=HEADERS
    )
    assert response.status_code == 422
    repository.insert_traffic_logs.assert_not_awaited()


def test_invalid_record_rejects_entire_batch_without_echoing_secrets(
    authorized_client, repository, traffic_payload
):
    invalid = {**traffic_payload, "body": {"password": "private-value"}, "timestamp": "2026-01-15"}
    response = authorized_client.post(
        URL, content=batch_body(traffic_payload, invalid), headers=HEADERS
    )
    assert response.status_code == 422
    assert "private-value" not in response.text
    repository.insert_traffic_logs.assert_not_awaited()


def test_ingestion_failure_is_not_acknowledged(authorized_client, repository, traffic_payload):
    repository.insert_traffic_logs.side_effect = PersistenceError("sensitive driver details")
    response = authorized_client.post(URL, content=batch_body(traffic_payload), headers=HEADERS)
    assert response.status_code == 503
    assert "sensitive driver details" not in response.text


def test_publisher_can_be_replaced_without_changing_api(
    authorized_client, repository, traffic_payload
):
    publisher = AsyncMock()
    publisher.publish.side_effect = lambda batch_id, documents: BatchReceipt(
        batch_id=batch_id, accepted_count=len(documents), status="queued"
    )
    app.dependency_overrides[get_ingestion_publisher] = lambda: publisher
    response = authorized_client.post(URL, content=batch_body(traffic_payload), headers=HEADERS)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    publisher.publish.assert_awaited_once()
    assert publisher.publish.call_args.args[1][0]["headers"]["Authorization"] == "[REDACTED]"
    repository.insert_traffic_logs.assert_not_awaited()


def test_batch_rejects_unauthenticated_requests(traffic_payload):
    response = TestClient(app).post(URL, content=batch_body(traffic_payload), headers=HEADERS)
    assert response.status_code == 401


def test_streaming_gzip_chunks_are_supported(authorized_client, traffic_payload):
    body = batch_body(traffic_payload)
    response = authorized_client.post(
        URL, content=(body[index : index + 7] for index in range(0, len(body), 7)), headers=HEADERS
    )
    assert response.status_code == 202


def test_openapi_references_resolve_without_other_upload_routes():
    schema = TestClient(app).get("/openapi.json").json()

    def check_references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = schema
                for part in value["$ref"].removeprefix("#/").split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(schema)
