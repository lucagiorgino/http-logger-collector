import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.db.mongo import MongoRepository, PersistenceError
from app.schemas.status import HealthStatus, PipelineNode, PipelineStatus
from app.services.upload_cloud import UploadCloud


async def probe(check: Callable[[], Awaitable[Any]], name: str) -> tuple[bool, Any]:
    try:
        async with asyncio.timeout(3):
            return True, await check()
    except (PersistenceError, TimeoutError):
        logger.warning("Pipeline status check failed: %s", name)
        return False, None


async def pipeline_status(repository: MongoRepository, cloud: UploadCloud) -> PipelineStatus:
    now = datetime.now(UTC)
    snapshot_check, analysis, storage, events, silver = await asyncio.gather(
        probe(repository.pipeline_snapshot, "operational Mongo"),
        probe(repository.check_analysis_connection, "analysis Mongo"),
        probe(cloud.check_storage, "raw storage"),
        probe(lambda: cloud.check_subscription(settings.spool_subscription), "object subscription"),
        probe(
            lambda: cloud.check_subscription(settings.spool_silver_subscription),
            "silver subscription",
        ),
    )
    metadata_ok, snapshot = snapshot_check
    workers = snapshot["workers"] if metadata_ok else []
    uploads = {row["_id"]: row for row in snapshot["uploads"]} if metadata_ok else {}
    stale_seconds = max(60, settings.spool_processing_seconds + 60)

    def queue_node(id: str, label: str, ok: bool, states: tuple[str, ...]) -> PipelineNode:
        rows = [uploads[state] for state in states if state in uploads]
        pending = sum(row["count"] for row in rows) if metadata_ok else None
        stalled = any(
            (now - row["oldest"]).total_seconds() > settings.spool_lease_seconds for row in rows
        )
        return PipelineNode(
            id=id,
            label=label,
            status="unavailable" if not ok else "degraded" if stalled else "healthy",
            detail=(
                "Metadata check failed; service, resource or permissions unavailable."
                if not ok
                else "Uploads are waiting longer than one processing lease."
                if stalled
                else "Subscription reachable. Pending counts are upload receipts, not queue depth."
            ),
            pending=pending,
        )

    def worker_node(role: str, label: str) -> PipelineNode:
        reported = [worker for worker in workers if worker["role"] == role]
        active = [
            worker
            for worker in reported
            if (now - worker["seen_at"]).total_seconds() <= stale_seconds
        ]
        running = [worker for worker in active if worker["status"] == "healthy"]
        failed = uploads.get(f"{role}_failed", {}).get("count", 0)
        status: HealthStatus = (
            "degraded"
            if any(worker["status"] == "degraded" for worker in active) or failed
            else "healthy"
            if running
            else "unavailable"
            if reported
            else "unknown"
        )
        return PipelineNode(
            id=role,
            label=label,
            status=status,
            detail=(
                f"{len(running)} healthy instance(s); {failed} unresolved failed upload(s). "
                "Heartbeat reports consumer-loop health, not an end-to-end guarantee."
                if active
                else "No fresh heartbeat. The worker may be stopped or unable to report."
            ),
            last_seen=max((worker["seen_at"] for worker in reported), default=None),
        )

    cleanup = uploads.get("stored")
    cleanup_stalled = (
        cleanup is not None
        and (now - cleanup["oldest"]).total_seconds() > settings.spool_lease_seconds
    )
    nodes = [
        PipelineNode(
            id="collector",
            label="Collector API",
            status="healthy" if metadata_ok and storage[0] else "degraded",
            detail="API responding; authenticates and persists raw uploads before HTTP 202.",
        ),
        PipelineNode(
            id="raw",
            label="MinIO raw storage" if cloud.backend == "minio" else "GCS raw storage",
            status="healthy" if storage[0] else "unavailable",
            detail=(
                "Bucket metadata reachable; no synthetic object is written."
                if storage[0]
                else "Bucket check failed; verify storage connectivity and metadata permissions."
            ),
        ),
        queue_node("events", "Object-event Pub/Sub", events[0], ("uploading", "received")),
        worker_node("parse", "Parser / protection"),
        queue_node("silver", "Silver Pub/Sub", silver[0], ("queued",)),
        worker_node("load", "Mongo loader"),
        PipelineNode(
            id="mongo",
            label="MongoDB",
            status="healthy" if metadata_ok and analysis[0] else "unavailable",
            detail=(
                "Operational primary and configured analysis read target reachable."
                if metadata_ok and analysis[0]
                else "Operational or analysis read failed. No primary-read fallback."
            ),
        ),
        PipelineNode(
            id="cleanup",
            label="Raw cleanup",
            status="unknown" if not metadata_ok else "degraded" if cleanup_stalled else "healthy",
            detail="Raw objects are deleted only after Mongo persistence; pending cleanup is retried.",
            pending=cleanup["count"] if cleanup else 0 if metadata_ok else None,
        ),
    ]
    overall: HealthStatus = next(
        (
            status
            for status in ("unavailable", "degraded", "unknown")
            if any(node.status == status for node in nodes)
        ),
        "healthy",
    )
    return PipelineStatus(
        checked_at=now,
        deployment="local" if cloud.backend == "minio" else "gcp",
        status=overall,
        nodes=nodes,
    )
