"""Explicit data-writing benchmark of raw ACK and observed Mongo storage latency."""

import argparse
import asyncio
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from httpx2 import AsyncClient

from integration_tests.stack_client import DEFAULT_BASE_URL


def summarize(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    return {
        f"p{percentile}_ms": round(values[max(0, math.ceil(len(values) * percentile / 100) - 1)], 2)
        for percentile in (50, 95, 99)
    }


async def benchmark(args: argparse.Namespace) -> None:
    token = os.environ.get("INGEST_BENCH_TOKEN")
    if not token:
        raise ValueError("Set INGEST_BENCH_TOKEN to a dedicated test-device access token")
    origin = urlsplit(args.base_url)
    if origin.scheme != "https" and not (
        origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Use HTTPS except on loopback")
    if (
        not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
    ):
        raise ValueError("Supply an origin without credentials, path prefix, query or fragment")
    wire = Path(args.wire_file).read_bytes()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/jose" if args.jwe else "application/json",
    }
    if not args.jwe:
        headers["Content-Encoding"] = "gzip"
    upload_slots = asyncio.Semaphore(args.concurrency)
    polling_slots = asyncio.Semaphore(args.concurrency)
    async with AsyncClient(
        base_url=args.base_url, headers=headers, timeout=30, follow_redirects=False
    ) as client:
        started = monotonic()
        last_ack = started

        async def post_one():
            nonlocal last_ack
            async with upload_slots:
                start = monotonic()
                response = await client.post("/api/v1/traffic-log-uploads", content=wire)
                if response.status_code not in {200, 202}:
                    raise RuntimeError(f"Upload HTTP {response.status_code}")
                last_ack = monotonic()
                return {
                    "start": start,
                    "ack_ms": (last_ack - start) * 1000,
                    "ack_at": datetime.now(UTC).isoformat(),
                    **response.json(),
                }

        async def await_stored(result):
            while monotonic() - result["start"] < args.completion_timeout:
                async with polling_slots:
                    response = await client.get(
                        f"/api/v1/traffic-log-uploads/{result['upload_id']}"
                    )
                if response.status_code != 200:
                    raise RuntimeError(f"Status HTTP {response.status_code}")
                status = response.json()["status"]
                if status == "stored":
                    result["stored_observed_ms"] = (monotonic() - result["start"]) * 1000
                    result["stored_observed_at"] = datetime.now(UTC).isoformat()
                    return
                if status in {"parse_failed", "load_failed"}:
                    # Print only canonical state, never raw server error messages.
                    raise RuntimeError(f"Upload ended in {status}; inspect receipt/DLQ")
                await asyncio.sleep(0.2)
            raise TimeoutError("Upload completion timeout; inspect its receipt/DLQ")

        async def upload_and_observe():
            result = await post_one()
            try:
                async with asyncio.timeout_at(result["start"] + args.completion_timeout):
                    await await_stored(result)
            except TimeoutError:
                raise TimeoutError("Upload completion timeout; inspect its receipt/DLQ") from None
            return result

        results = await asyncio.gather(*(upload_and_observe() for _ in range(args.requests)))
        elapsed = last_ack - started
        print(
            json.dumps(
                {
                    "requests": args.requests,
                    "concurrency": args.concurrency,
                    "wire_bytes": len(wire),
                    "acknowledgement": summarize([result["ack_ms"] for result in results]),
                    "stored_observed": summarize(
                        [result["stored_observed_ms"] for result in results]
                    ),
                    "acknowledged_batches_per_second": round(args.requests / elapsed, 2),
                    "duplicates": sum(bool(result.get("duplicate")) for result in results),
                    "dlq": 0,
                    "uploads": [
                        {key: value for key, value in result.items() if key != "start"}
                        for result in results
                    ],
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--wire-file", required=True, help="Existing gzip batch or compact JWE file"
    )
    parser.add_argument("--jwe", action="store_true")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--completion-timeout", type=float, default=120)
    args = parser.parse_args()
    if min(args.requests, args.concurrency, args.completion_timeout) <= 0 or not math.isfinite(
        args.completion_timeout
    ):
        parser.error("Counts, concurrency and timeout must be positive and finite")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
