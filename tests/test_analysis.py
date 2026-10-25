from datetime import UTC, datetime

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.core.auth import require_bearer_token
from app.db.mongo import PersistenceError
from app.main import app
from app.schemas.traffic import TrafficLogRequest
from app.services.analysis import decode_cursor, encode_cursor
from app.services.traffic_service import normalize_traffic_log

URL = "/api/v1/traffic-logs"


@pytest.mark.parametrize("suspicious", [None, False, True])
def test_suspicious_classification_is_returned_in_details_and_lists(
    authorized_client, repository, traffic_payload, trusted_context, suspicious
):
    document = normalize_traffic_log(
        TrafficLogRequest(**traffic_payload), "batch-123", trusted_context
    )
    if suspicious is None:
        document["metadata"].pop("suspicious")
    else:
        document["metadata"]["suspicious"] = suspicious
    repository.list_traffic_logs.return_value = [document]
    repository.get_traffic_log.return_value = document
    assert authorized_client.get(URL).json()["items"][0]["suspicious"] is (suspicious is True)
    assert authorized_client.get(f"{URL}/{document['_id']}").json()["suspicious"] is (
        suspicious is True
    )


def test_agents_cannot_assign_suspicious_classification(
    authorized_client, traffic_payload, repository, drain_uploads
):
    import gzip
    import json

    traffic_payload["suspicious"] = True
    response = authorized_client.post(
        "/api/v1/traffic-log-uploads",
        content=gzip.compress(json.dumps({"logs": [traffic_payload]}).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 202
    drain_uploads()
    assert (
        authorized_client.get(f"/api/v1/traffic-log-uploads/{response.json()['upload_id']}").json()[
            "error_code"
        ]
        == "invalid_content"
    )
    repository.insert_queued_traffic_logs.assert_not_awaited()


def test_paginated_analysis_returns_an_opaque_cursor(
    authorized_client, repository, traffic_payload, trusted_context
):
    documents = [
        normalize_traffic_log(TrafficLogRequest(**traffic_payload), "batch-123", trusted_context)
        for _ in range(3)
    ]
    documents.sort(key=lambda document: document["_id"], reverse=True)
    repository.list_traffic_logs.return_value = documents
    response = authorized_client.get(URL, params={"limit": 2, "device_id": "device-456"})
    assert response.status_code == 200
    page = response.json()
    assert [item["id"] for item in page["items"]] == [str(item["_id"]) for item in documents[:2]]
    assert page["items"][0]["device_id"] == "device-456"
    assert "user_id" not in page["items"][0]
    assert page["items"][0]["client_id"] == "oauth-client-123"
    assert page["items"][0]["source_ip"] == "203.0.113.10"
    assert "_id" not in page["items"][0]
    assert "body" not in page["items"][0]
    assert decode_cursor(page["next_cursor"]) == (documents[1]["timestamp"], documents[1]["_id"])
    repository.list_traffic_logs.assert_awaited_once_with(
        device_id="device-456", start_time=None, end_time=None, before=None, limit=3
    )

    repository.list_traffic_logs.return_value = [documents[2]]
    response = authorized_client.get(
        URL, params={"limit": 2, "device_id": "device-456", "cursor": page["next_cursor"]}
    )
    assert response.json()["next_cursor"] is None
    assert repository.list_traffic_logs.call_args.kwargs["before"] == decode_cursor(
        page["next_cursor"]
    )


def test_analysis_filters_are_validated_and_forwarded(authorized_client, repository):
    params = {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-02-01T00:00:00Z"}
    response = authorized_client.get(URL, params=params)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    args = repository.list_traffic_logs.call_args.kwargs
    assert args["start_time"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert args["end_time"] == datetime(2026, 2, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "invalid!"},
        {"start_time": "2026-01-01"},
        {"start_time": "2026-02-01T00:00:00Z", "end_time": "2026-01-01T00:00:00Z"},
        {"cursor": encode_cursor({"timestamp": datetime(2026, 1, 1), "_id": ObjectId()})},
    ],
)
def test_invalid_queries_do_not_reach_storage(authorized_client, repository, params):
    response = authorized_client.get(URL, params=params)
    assert response.status_code == 422
    repository.list_traffic_logs.assert_not_awaited()


def test_record_lookup_is_device_scoped(
    authorized_client, repository, traffic_payload, trusted_context
):
    document = normalize_traffic_log(
        TrafficLogRequest(**traffic_payload), "batch-123", trusted_context
    )
    document.update(document.pop("ingestion"))
    document.update(document.pop("logdata"))
    del document["client_id"]
    document["headers"] = document.pop("request_headers")
    for field in (
        "response_headers",
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
        del document[field]
    repository.get_traffic_log.return_value = document
    response = authorized_client.get(f"{URL}/{document['_id']}")
    assert response.status_code == 200
    assert response.json()["id"] == str(document["_id"])
    assert response.json()["client_id"] is None
    assert response.json()["request_headers"] == response.json()["headers"] == document["headers"]
    assert response.json()["response_headers"] == {}
    assert response.json()["destination_ip"] is None
    assert response.json()["process_name"] is None
    repository.get_traffic_log.assert_awaited_once_with(document["_id"], device_id="device-456")


def test_missing_or_other_devices_record_is_not_disclosed(authorized_client, repository):
    response = authorized_client.get(f"{URL}/{ObjectId()}")
    assert response.status_code == 404
    response = authorized_client.get(f"{URL}/invalid-id")
    assert response.status_code == 422
    assert repository.get_traffic_log.await_count == 1


@pytest.mark.parametrize("path", [URL, f"{URL}/{ObjectId()}"])
def test_analysis_failure_returns_503_without_primary_fallback(authorized_client, repository, path):
    repository.list_traffic_logs.side_effect = PersistenceError("secondary unavailable")
    repository.get_traffic_log.side_effect = PersistenceError("secondary unavailable")
    assert authorized_client.get(path).status_code == 503
    repository.insert_traffic_logs.assert_not_awaited()


@pytest.mark.parametrize("path", [URL, f"{URL}/{ObjectId()}", "/api/v1/me"])
def test_analysis_and_identity_require_authentication(path):
    assert TestClient(app).get(path).status_code == 401


def test_current_identity_comes_from_verified_device_claim(authorized_client):
    assert authorized_client.get("/api/v1/me").json() == {
        "device_id": "device-456",
        "client_id": "oauth-client-123",
    }
    app.dependency_overrides[require_bearer_token] = lambda: {"unrelated": "claim"}
    assert authorized_client.get("/api/v1/me").status_code == 401
    assert authorized_client.get(URL).status_code == 401


def test_agent_analysis_cannot_select_another_device(authorized_client, repository):
    assert authorized_client.get(URL, params={"device_id": "another-device"}).status_code == 403
    repository.list_traffic_logs.assert_not_awaited()
    assert authorized_client.get(URL).status_code == 200
    assert repository.list_traffic_logs.call_args.kwargs["device_id"] == "device-456"


def test_operator_can_analyze_fleet_or_filter_by_device(authorized_client, repository):
    app.dependency_overrides[require_bearer_token] = lambda: {
        "sub": "operator",
        "scope": "manage:devices",
    }
    assert authorized_client.get(URL).status_code == 200
    assert repository.list_traffic_logs.call_args.kwargs["device_id"] is None
    assert authorized_client.get(URL, params={"device_id": "another-device"}).status_code == 200
    assert repository.list_traffic_logs.call_args.kwargs["device_id"] == "another-device"
    assert authorized_client.get(f"{URL}/{ObjectId()}").status_code == 404
    assert repository.get_traffic_log.call_args.kwargs["device_id"] is None
