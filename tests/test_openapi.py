import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.devices import (
    ConfigurationPatch,
    ConfigurationStatus,
    DeviceConfiguration,
    DevicePage,
    DeviceRegistration,
    ReportedConfigurationStatus,
)
from app.schemas.traffic import BatchReceipt, StoredTrafficLog, TrafficLogBatch, TrafficLogPage
from app.schemas.workspace import WorkspaceConfiguration, WorkspacePatch

client = TestClient(app)


def media_examples(document, media):
    if "example" in media:
        return [media["example"]]
    if "examples" in media:
        return [example["value"] for example in media["examples"].values()]
    schema = media["schema"]
    while "$ref" in schema:
        schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema.get("examples", [])


def test_interactive_docs_expose_permissions_and_examples_for_every_operation():
    response = client.get("/docs")
    assert response.status_code == 200
    assert "/openapi.json" in response.text
    document = client.get("/openapi.json").json()
    management_operations = {
        ("post", "/api/v1/devices"),
        ("patch", "/api/v1/devices/{device_id}/configuration"),
        ("get", "/api/v1/workspace/configuration"),
        ("patch", "/api/v1/workspace/configuration"),
    }
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            assert operation["summary"], (method, path)
            description = operation["description"]
            if (method, path) in management_operations:
                assert "**Required permission:** `manage:devices`." in description
            elif path == "/health":
                assert "**Required permission:** None. Public endpoint" in description
                assert not operation.get("security")
            else:
                assert "**Required permission:** None beyond authentication" in description
            if path.startswith("/api/"):
                assert operation["security"] == [{"Auth0Bearer": []}]
            for code, response in operation["responses"].items():
                if code.startswith("2"):
                    assert media_examples(document, response["content"]["application/json"])
            for media in operation.get("requestBody", {}).get("content", {}).values():
                assert media_examples(document, media), (method, path)
    scheme = document["components"]["securitySchemes"]["Auth0Bearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT"
    assert "permissions array or space-separated scope" in scheme["description"]


@pytest.mark.parametrize(
    "model",
    [
        DeviceRegistration,
        ConfigurationPatch,
        ConfigurationStatus,
        ReportedConfigurationStatus,
        DeviceConfiguration,
        DevicePage,
        WorkspaceConfiguration,
        WorkspacePatch,
        TrafficLogBatch,
        BatchReceipt,
        StoredTrafficLog,
        TrafficLogPage,
    ],
)
def test_documented_json_examples_validate_against_the_actual_api_models(model):
    for example in model.model_json_schema()["examples"]:
        model.model_validate(example)


def test_batch_docs_separate_wire_formats_and_explain_precompression_example():
    document = client.get("/openapi.json").json()
    operation = document["paths"]["/api/v1/traffic-log-batches"]["post"]
    content = operation["requestBody"]["content"]
    assert set(content) == {"application/json", "application/jose"}
    assert set(content["application/json"]["schema"]["properties"]) == {"logs"}
    for example in media_examples(document, content["application/json"]):
        TrafficLogBatch.model_validate(example)
    assert content["application/jose"]["schema"]["type"] == "string"
    assert "before gzip compression" in operation["description"]
    assert (
        "Illustrative only"
        in content["application/jose"]["examples"]["compact_jwe_structure"]["summary"]
    )
    page_schema = document["components"]["schemas"]["TrafficLogPage"]
    assert "body" not in page_schema["examples"][0]["items"][0]
