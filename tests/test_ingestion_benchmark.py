import argparse
import asyncio
import json
import sys

import pytest
from httpx2 import AsyncClient, MockTransport, Response

from integration_tests import benchmark_ingestion


def test_benchmark_reports_ack_and_observed_storage_timestamps(
    tmp_path,
    monkeypatch,
    capsys,
):
    wire = tmp_path / "synthetic.gz"
    wire.write_bytes(b"synthetic-wire")
    monkeypatch.setenv("INGEST_BENCH_TOKEN", "test-only-placeholder")
    calls = []

    def respond(request):
        assert request.url.scheme == "http"
        assert request.url.host == "localhost"
        assert request.url.port == 8080
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.content == b"synthetic-wire"
            assert request.url.path == "/api/v1/traffic-log-uploads"
            return Response(
                202, json={"upload_id": "upload", "status": "received", "duplicate": False}
            )
        return Response(200, json={"status": "stored"})

    monkeypatch.setattr(
        benchmark_ingestion,
        "AsyncClient",
        lambda **kwargs: AsyncClient(transport=MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_ingestion",
            "--wire-file",
            str(wire),
            "--requests",
            "2",
            "--concurrency",
            "1",
            "--completion-timeout",
            "1",
        ],
    )
    benchmark_ingestion.main()
    report = json.loads(capsys.readouterr().out)
    assert "acknowledgement" in report
    assert "stored_observed" in report
    assert report["duplicates"] == report["dlq"] == 0
    assert all(upload["ack_at"] <= upload["stored_observed_at"] for upload in report["uploads"])
    assert len(calls) == 4


def test_benchmark_fails_explicitly_on_async_validation_error(tmp_path, monkeypatch):
    wire = tmp_path / "synthetic.gz"
    wire.write_bytes(b"synthetic-wire")
    monkeypatch.setenv("INGEST_BENCH_TOKEN", "test-only-placeholder")

    def respond(request):
        return Response(
            202 if request.method == "POST" else 200,
            json={"upload_id": "upload", "status": "parse_failed"},
        )

    monkeypatch.setattr(
        benchmark_ingestion,
        "AsyncClient",
        lambda **kwargs: AsyncClient(transport=MockTransport(respond), **kwargs),
    )
    args = argparse.Namespace(
        base_url="https://example.test",
        wire_file=str(wire),
        jwe=False,
        requests=1,
        concurrency=1,
        completion_timeout=1,
    )
    with pytest.raises(RuntimeError, match="parse_failed"):
        asyncio.run(benchmark_ingestion.benchmark(args))
