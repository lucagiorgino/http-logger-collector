from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

HealthStatus = Literal["healthy", "degraded", "unavailable", "unknown"]


class PipelineNode(BaseModel):
    id: str
    label: str
    status: HealthStatus
    detail: str
    last_seen: datetime | None = None
    pending: int | None = None


class PipelineStatus(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "checked_at": "2026-10-25T21:00:00Z",
                    "deployment": "local",
                    "status": "unknown",
                    "nodes": [
                        {
                            "id": node_id,
                            "label": label,
                            "status": "unknown" if node_id in {"parse", "load"} else "healthy",
                            "detail": "Waiting for heartbeat."
                            if node_id in {"parse", "load"}
                            else "Read-only check passed.",
                        }
                        for node_id, label in [
                            ("collector", "Collector API"),
                            ("raw", "MinIO raw storage"),
                            ("events", "Object-event Pub/Sub"),
                            ("parse", "Parser / protection"),
                            ("silver", "Silver Pub/Sub"),
                            ("load", "Mongo loader"),
                            ("mongo", "MongoDB"),
                            ("cleanup", "Raw cleanup"),
                        ]
                    ],
                }
            ]
        }
    )

    checked_at: datetime
    deployment: Literal["local", "gcp"]
    status: HealthStatus
    nodes: list[PipelineNode]
