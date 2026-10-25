import gzip
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import batches
from app.core.auth import require_bearer_token
from app.db.mongo import PersistenceError
from app.deps import get_ingestion_publisher
from app.main import app
from app.schemas.traffic import BatchReceipt
from app.services.traffic_service import DEVICE_ID_CLAIM

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
        assert document["metadata"] == {"device_id": "device-456"}
        assert document["client_id"] == "oauth-client-123"
        assert document["source_ip"] == "203.0.113.10"
        assert document["timestamp"].isoformat() == "2026-01-15T08:00:00+00:00"
        assert document["method"] == "GET"
        assert document["request_headers"] == {
            "Authorization": "[REDACTED]",
            "Cookie": "[REDACTED]",
            "User-Agent": "TestAgent",
        }
        assert "body" not in document
        assert document["body_length"] == len(traffic_payload["body"].encode())
        assert document["body_sha256"]


def test_complete_http_event_is_stored_and_read_back(
    authorized_client, repository, rich_traffic_payload
):
    response = authorized_client.post(
        URL, content=batch_body(rich_traffic_payload), headers=HEADERS
    )
    assert response.status_code == 202
    document = repository.insert_traffic_logs.call_args.args[0][0]
    repository.get_traffic_log.return_value = document
    repository.list_traffic_logs.return_value = [document]
    for record in (
        authorized_client.get(f"/api/v1/traffic-logs/{document['_id']}").json(),
        authorized_client.get("/api/v1/traffic-logs").json()["items"][0],
    ):
        for field in (
            "captured_source_ip",
            "destination_ip",
            "source_port",
            "destination_port",
            "http_version",
            "request_size_bytes",
            "response_size_bytes",
            "duration_ms",
            "process_name",
            "process_id",
            "request_id",
        ):
            assert document[field] == rich_traffic_payload[field]
            assert record[field] == rich_traffic_payload[field]
        assert record["source_ip"] == "203.0.113.10"
        assert (
            record["request_headers"]
            == record["headers"]
            == {
                "Authorization": "[REDACTED]",
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
            }
        )
        assert record["response_headers"] == {
            "Content-Type": "application/json",
            "Set-Cookie": "[REDACTED]",
        }
        assert "private-credential" not in repr(document)
        assert "private-cookie" not in repr(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", "data"),
        ("$where", "malicious-expression"),
        ("method", "GET\r\nInjected: value"),
        ("method", {"$ne": None}),
        ("status_code", "200"),
        ("status_code", True),
        ("status_code", 200.0),
        ("url", {"$regex": ".*"}),
        ("url", "https://user:secret@example.com/"),
        ("url", "https://example.com/\npath"),
        ("url", "javascript:alert(1)"),
        ("url", "https://example.com/" + "x" * 8192),
        ("timestamp", 1736936625),
        ("timestamp", "1736936625"),
        ("timestamp", "2025-01-15T10:23:45"),
        ("destination_ip", "203.0.113.999"),
        ("destination_ip", {"$gt": ""}),
        ("destination_ip", 1234),
        ("captured_source_ip", "fe80::1%eth0"),
        ("destination_port", 65536),
        ("source_port", "443"),
        ("http_version", "HTTP/9"),
        ("response_size_bytes", -1),
        ("request_size_bytes", 2**63),
        ("response_size_bytes", True),
        ("process_id", 1.5),
        ("duration_ms", float("nan")),
        ("duration_ms", float("inf")),
        ("duration_ms", "12.5"),
        ("process_name", "chrome.exe\nforged event"),
        ("process_name", "x" * 256),
        ("process_name", " "),
        ("process_name", "\ud800"),
        ("body", "\ud800"),
        ("body", "x" * 1_000_001),
        ("request_id", {"$ne": None}),
        ("request_id", "x" * 129),
        ("request_headers", {"$where": "payload"}),
        ("request_headers", {"x.y": "payload"}),
        ("request_headers", {"__proto__": "payload"}),
        ("request_headers", {"X": {"$ne": None}}),
        ("request_headers", {"X": "line1\r\nInjected: value"}),
        ("request_headers", {"X": "one", "x": "two"}),
        ("request_headers", {f"X-{i}": "value" for i in range(101)}),
        ("request_headers", {"X": "x" * 8193}),
        ("response_headers", {f"X-{i}": "x" * 8192 for i in range(4)}),
    ],
)
def test_strict_event_schema_rejects_whole_batch_before_publishing(
    authorized_client, repository, traffic_payload, field, value
):
    invalid = {key: item for key, item in traffic_payload.items() if key != "headers"}
    invalid[field] = value
    response = authorized_client.post(
        URL, content=batch_body(traffic_payload, invalid), headers=HEADERS
    )
    assert response.status_code == 422
    repository.get_device_configuration.assert_not_awaited()
    repository.insert_traffic_logs.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        '{"logs":[],"logs":[]}',
        '{"logs":[],"unexpected":true}',
        '{"logs":[{"request_headers":{"X":"one","X":"two"}}]}',
        '{"logs":[{"method":"GET","method":"POST"}]}',
        '{"logs":[' + "[" * 1100 + "0" + "]" * 1100 + "]}",
    ],
)
def test_ambiguous_or_unexpected_json_is_rejected(authorized_client, repository, body):
    response = authorized_client.post(URL, content=gzip.compress(body.encode()), headers=HEADERS)
    assert response.status_code == 422
    repository.insert_traffic_logs.assert_not_awaited()


def test_header_aliases_cannot_be_supplied_together(authorized_client, repository, traffic_payload):
    response = authorized_client.post(
        URL,
        content=batch_body(traffic_payload | {"request_headers": {"X": "ambiguous"}}),
        headers=HEADERS,
    )
    assert response.status_code == 422
    repository.insert_traffic_logs.assert_not_awaited()


def test_large_valid_url_and_zero_metrics_are_not_silently_dropped(
    authorized_client, repository, traffic_payload
):
    payload = traffic_payload | {
        "url": "https://example.com/" + "a" * 3000,
        "request_size_bytes": 0,
        "response_size_bytes": 0,
        "duration_ms": 0,
        "process_id": 0,
    }
    response = authorized_client.post(URL, content=batch_body(payload), headers=HEADERS)
    assert response.status_code == 202
    document = repository.insert_traffic_logs.call_args.args[0][0]
    for field in ("url", "request_size_bytes", "response_size_bytes", "duration_ms", "process_id"):
        assert document[field] == payload[field]


@pytest.mark.parametrize(
    "field",
    ["user_id", "device_id", "client_id", "source_ip", "metadata", "received_at", "batch_id"],
)
def test_client_attribution_is_rejected_before_any_write(
    authorized_client, repository, traffic_payload, field
):
    forged = {**traffic_payload, field: "forged-metadata"}
    response = authorized_client.post(
        URL, content=batch_body(traffic_payload, forged), headers=HEADERS
    )
    assert response.status_code == 422
    repository.get_device_configuration.assert_not_awaited()
    repository.insert_traffic_logs.assert_not_awaited()


def test_missing_device_claim_uses_subject_and_ignores_untrusted_forwarding_headers(
    authorized_client, repository, traffic_payload
):
    app.dependency_overrides[require_bearer_token] = lambda: {"sub": "oauth-client@clients"}
    response = authorized_client.post(
        URL,
        content=batch_body(traffic_payload),
        headers={
            **HEADERS,
            "X-Forwarded-For": "198.51.100.1",
            "X-Real-IP": "198.51.100.2",
            "Forwarded": "for=198.51.100.3",
            "X-Device-ID": "forged-device",
            "X-Client-ID": "forged-client",
        },
    )
    assert response.status_code == 202
    document = repository.insert_traffic_logs.call_args.args[0][0]
    assert document["metadata"] == {
        "device_id": "oauth-client@clients",
    }
    assert document["client_id"] is None
    assert document["source_ip"] == "203.0.113.10"
    repository.get_device_configuration.assert_awaited_once_with("oauth-client@clients")


@pytest.mark.parametrize(
    ("peer", "expected_ip"),
    [("10.0.0.10", "198.51.100.9"), ("203.0.113.10", "203.0.113.10")],
)
def test_forwarded_ip_requires_a_trusted_proxy(
    authorized_client, repository, traffic_payload, peer, expected_ip
):
    proxied = TestClient(
        ProxyHeadersMiddleware(app, trusted_hosts=["10.0.0.0/24"]),
        client=(peer, 50000),
    )
    response = proxied.post(
        URL,
        content=batch_body(traffic_payload),
        headers={**HEADERS, "X-Forwarded-For": "198.51.100.9, 10.0.0.20"},
    )
    assert response.status_code == 202
    document = repository.insert_traffic_logs.call_args.args[0][0]
    assert document["source_ip"] == expected_ip


@pytest.mark.parametrize("peer", ["2001:db8::123", None, "invalid-network-address"])
def test_network_addresses_are_validated_without_inventing_an_ip(
    authorized_client, repository, traffic_payload, peer
):
    network_client = TestClient(app, client=(peer, 50000) if peer is not None else None)
    response = network_client.post(URL, content=batch_body(traffic_payload), headers=HEADERS)
    if peer == "invalid-network-address":
        assert response.status_code == 400
        repository.insert_traffic_logs.assert_not_awaited()
    else:
        assert response.status_code == 202
        assert repository.insert_traffic_logs.call_args.args[0][0]["source_ip"] == peer


@pytest.mark.parametrize("device_id", ["", " ", None, 123, ["forged"], "x" * 256])
def test_invalid_signed_device_identity_is_not_replaced_by_subject(
    authorized_client, repository, traffic_payload, device_id
):
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "user-123",
        DEVICE_ID_CLAIM: device_id,
    }
    response = authorized_client.post(URL, content=batch_body(traffic_payload), headers=HEADERS)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    repository.get_device_configuration.assert_not_awaited()
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
    assert (
        publisher.publish.call_args.args[1][0]["request_headers"]["Authorization"] == "[REDACTED]"
    )
    queued = publisher.publish.call_args.args[1][0]
    assert traffic_payload["body"] not in repr(queued)
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
