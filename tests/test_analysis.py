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


def test_paginated_analysis_returns_an_opaque_cursor(
    authorized_client, repository, traffic_payload
):
    documents = [
        normalize_traffic_log(TrafficLogRequest(**traffic_payload), "batch-123") for _ in range(3)
    ]
    documents.sort(key=lambda document: document["_id"], reverse=True)
    repository.list_traffic_logs.return_value = documents
    response = authorized_client.get(URL, params={"limit": 2, "device_id": "device-456"})
    assert response.status_code == 200
    page = response.json()
    assert [item["id"] for item in page["items"]] == [str(item["_id"]) for item in documents[:2]]
    assert page["items"][0]["user_id"] == "user-123"
    assert "_id" not in page["items"][0]
    assert "body" not in page["items"][0]
    assert decode_cursor(page["next_cursor"]) == (documents[1]["timestamp"], documents[1]["_id"])
    repository.list_traffic_logs.assert_awaited_once_with(
        "user-123", device_id="device-456", start_time=None, end_time=None, before=None, limit=3
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


def test_record_lookup_is_owner_scoped(authorized_client, repository, traffic_payload):
    document = normalize_traffic_log(TrafficLogRequest(**traffic_payload), "batch-123")
    repository.get_traffic_log.return_value = document
    response = authorized_client.get(f"{URL}/{document['_id']}")
    assert response.status_code == 200
    assert response.json()["id"] == str(document["_id"])
    repository.get_traffic_log.assert_awaited_once_with("user-123", document["_id"])


def test_missing_or_other_users_record_is_not_disclosed(authorized_client, repository):
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


def test_current_identity_comes_from_verified_subject(authorized_client):
    assert authorized_client.get("/api/v1/me").json() == {"user_id": "user-123"}
    app.dependency_overrides[require_bearer_token] = lambda: {"unrelated": "claim"}
    assert authorized_client.get("/api/v1/me").status_code == 401
    assert authorized_client.get(URL).status_code == 401
