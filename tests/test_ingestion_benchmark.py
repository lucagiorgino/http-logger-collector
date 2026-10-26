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


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0", "-1"])
def test_benchmark_rejects_invalid_completion_timeout_before_uploading(monkeypatch, timeout):
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark_ingestion", "--wire-file", "unused.gz", f"--completion-timeout={timeout}"],
    )
    with pytest.raises(SystemExit) as error:
        benchmark_ingestion.main()
    assert error.value.code == 2


@pytest.mark.parametrize("origin", ["https:///missing-host", "https://example.test/api"])
def test_benchmark_rejects_invalid_origins_before_reading_payload(monkeypatch, origin):
    monkeypatch.setenv("INGEST_BENCH_TOKEN", "test-only-placeholder")
    args = argparse.Namespace(base_url=origin, wire_file="unused.gz")
    with pytest.raises(ValueError, match="origin"):
        asyncio.run(benchmark_ingestion.benchmark(args))


@pytest.mark.parametrize("concurrency", [1, 2])
def test_benchmark_polls_completed_uploads_while_other_uploads_are_pending(
    tmp_path, monkeypatch, capsys, concurrency
):
    wire = tmp_path / "synthetic.gz"
    wire.write_bytes(b"synthetic-wire")
    monkeypatch.setenv("INGEST_BENCH_TOKEN", "test-only-placeholder")

    async def scenario():
        first_polled = asyncio.Event()
        uploads = 0

        async def respond(request):
            nonlocal uploads
            if request.method == "POST":
                uploads += 1
                upload_id = str(uploads)
                await asyncio.sleep(0)
                if upload_id == "2":
                    await asyncio.wait_for(first_polled.wait(), timeout=1)
                return Response(202, json={"upload_id": upload_id, "duplicate": False})
            if request.url.path.endswith("/1"):
                first_polled.set()
            return Response(200, json={"status": "stored"})

        monkeypatch.setattr(
            benchmark_ingestion,
            "AsyncClient",
            lambda **kwargs: AsyncClient(transport=MockTransport(respond), **kwargs),
        )
        await benchmark_ingestion.benchmark(
            argparse.Namespace(
                base_url="http://localhost:8080",
                wire_file=str(wire),
                jwe=False,
                requests=2,
                concurrency=concurrency,
                completion_timeout=5,
            )
        )

    asyncio.run(scenario())
    report = json.loads(capsys.readouterr().out)
    assert len(report["uploads"]) == 2
    assert report["uploads"][0]["stored_observed_at"] <= report["uploads"][1]["ack_at"]


def test_benchmark_completion_deadline_bounds_a_stalled_status_request(tmp_path, monkeypatch):
    wire = tmp_path / "synthetic.gz"
    wire.write_bytes(b"synthetic-wire")
    monkeypatch.setenv("INGEST_BENCH_TOKEN", "test-only-placeholder")

    async def respond(request):
        if request.method == "POST":
            return Response(202, json={"upload_id": "upload"})
        await asyncio.sleep(1)
        return Response(200, json={"status": "stored"})

    monkeypatch.setattr(
        benchmark_ingestion,
        "AsyncClient",
        lambda **kwargs: AsyncClient(transport=MockTransport(respond), **kwargs),
    )
    args = argparse.Namespace(
        base_url="http://localhost:8080",
        wire_file=str(wire),
        jwe=False,
        requests=1,
        concurrency=1,
        completion_timeout=0.01,
    )
    with pytest.raises(TimeoutError, match="completion timeout"):
        asyncio.run(benchmark_ingestion.benchmark(args))
