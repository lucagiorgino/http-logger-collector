# Project blueprint

Implementation map and architectural rationale for [Amazing HTTP Logger](README.md).
Detailed contracts live in the [API reference](docs/API.md),
[ingestion design](docs/INGESTION.md) and [encryption protocol](docs/ENCRYPTION.md);
commands belong in the [runbook](docs/RUNBOOK.md).

## Component boundaries

The Python/FastAPI API authenticates and authorizes, bounds input and orchestrates
services. The parser handles expensive cryptography/validation; the independent
loader controls Mongo throughput. Native async PyMongo handles persistence;
HTTPX2 handles outbound HTTP. Next.js is a JavaScript/JSX SSR console, not a SPA
holding API credentials.

| Code | Responsibility |
| --- | --- |
| [`app/main.py`](app/main.py), [`app/deps.py`](app/deps.py) | Application lifecycle, routes and dependency wiring |
| [`app/api/`](app/api/) | Identity, uploads, devices, workspace, analysis and Status HTTP handlers |
| [`app/schemas/`](app/schemas/) | Closed request schemas and explicit response models |
| [`app/core/auth.py`](app/core/auth.py), [`security.py`](app/core/security.py) | Auth0 validation, identity/permission and HTTPS boundaries |
| [`app/core/encryption.py`](app/core/encryption.py), [`storage_encryption.py`](app/core/storage_encryption.py) | JWE and field protection |
| [`app/core/openbao.py`](app/core/openbao.py), [`app/kms.py`](app/kms.py) | KMS adapter and administrative key lifecycle |
| [`app/services/upload_pipeline.py`](app/services/upload_pipeline.py) | Parser/loader orchestration, retry, replay and cleanup |
| [`upload_cloud.py`](app/services/upload_cloud.py), [`upload_queue.py`](app/services/upload_queue.py) | Object storage and Pub/Sub adapters |
| [`upload_state.py`](app/services/upload_state.py), [`queued_batch.py`](app/services/queued_batch.py) | Receipt leases/state and bounded silver encoding |
| [`traffic_service.py`](app/services/traffic_service.py), [`batch_codec.py`](app/services/batch_codec.py) | Batch validation, normalization and protection |
| [`analysis.py`](app/services/analysis.py), [`pipeline_status.py`](app/services/pipeline_status.py) | Analysis representation/cursors and portable health snapshot |
| [`app/db/mongo.py`](app/db/mongo.py) | Collections/indexes, primary operations and secondary analysis |
| [`app/spool_worker.py`](app/spool_worker.py), [`app/spool_dev.py`](app/spool_dev.py) | Worker/operator entry point and local event bootstrap |
| [`frontend/`](frontend/README.md) | Auth0 sessions, private API adapter, SSR pages and forms |
| [`docker/`](docker/) | Deployment image and profile-based local dependency stack |
| [`helm/amazing-http-logger/`](helm/amazing-http-logger/) | Optional Kubernetes workloads; cloud resources remain external |
| [`infra/gcp/`](infra/gcp/README.md) | Terraform, direct Cloud Run services, cloud-only KMS overlay and secret bootstrap |

Tests import the application, never vice versa. Add HTTP contracts with matching
schemas and refusal-path tests; keep business logic out of route handlers.
Use [contributor guidance](AGENT.md) for repository rules, not copied scaffolding.

## Trust and availability decisions

- Auth0, not this service, issues tokens. Globally unique device identity and
  fleet permission are trusted JWT claims, not uploaded/user-editable fields.
- Gateways terminate TLS and sanitize forwarding metadata; only trusted ingress
  can supply upload peer identity. Captured traffic is untrusted observation.
- Raw durability precedes acknowledgement; minimization/protection precedes
  silver publication. Raw is a deliberate, restricted privacy/replay boundary.
- Parser and loader have different privileges. The loader has no upload EC/KMS
  credentials. Neither worker has an unbounded in-memory backlog.
- Operational settings/receipts use primary/majority; analysis uses strict
  secondary/majority, accepting replica lag instead of increasing primary load
  during replica outages. Replica-set URIs are mandatory; direct connections fail.
- OpenBao wraps EC keys at rest, but JWE private keys enter process RAM. If this
  is unacceptable, remote ECDH or a protocol redesign is needed. Cloud KMS keeps
  wrapping/storage master keys non-exportable; its envelope is not OpenBao-compatible.

No ciphertext/key authority migration is automatic. Use fresh test/cloud databases
when changing identity/storage authorities; startup never resets data.

## Stored document layout

MongoDB 7+ `traffic_logs` is a time-series collection with `timeField=timestamp`,
`metaField=metadata` and seconds granularity:

```javascript
{
  _id: ObjectId("6add2bcd3a25072164dd8cc9"),
  timestamp: ISODate("2026-10-24T22:06:05.206Z"),
  metadata: {device_id: "example-device", suspicious: false},
  ingestion: {
    batch_id: "4d737a07-51a6-4164-abb3-6ea75350b6fd",
    client_id: "example-oauth-client",
    source_ip: "127.0.0.1",
    received_at: ISODate("2026-10-24T22:06:05.211Z")
  },
  logdata: {
    method: "GET",
    url: "https://example.test/",
    status_code: 200,
    request_headers: {},
    response_headers: {},
    captured_source_ip: null,
    destination_ip: "203.0.113.50",
    http_version: "2",
    response_size_bytes: 1024,
    duration_ms: 12.5,
    process_name: "example-agent",
    body_length: 0,
    body_sha256: null
  }
}
```

Other optional observations are omitted here. Plaintext retained body is
`logdata.body`; encrypted values live in root `storage_encryption`, with
`[ENCRYPTED]` header placeholders. Discarded bodies retain length/hash.
Upload/read records are flat; database consumers use these nested paths.

`metadata` is the **bucket grouping key**, not a generic bag. High-cardinality
batch IDs, reception times and upload IPs belong in `ingestion` to avoid
fragmenting buckets/compression. Classification stays in metadata for partial TTL.
New records are server-classified `suspicious=false`; no classifier is implemented.

Indexes cover `(metadata.device_id, timestamp, _id)`, `(timestamp, _id)`,
`ingestion.received_at` and `(ingestion.batch_id, _id)` for replay checks.
Timestamp/ID keysets avoid growing offsets; current queries need no additional
HTTP-field indexes. Measurement IDs are not uniqueness-enforced by time-series
storage, so ambiguous inserts remain an at-least-once risk.

Ordinary/suspicious partial TTL indexes operate on event time, never a global
collection TTL. See [storage initialization](docs/RUNBOOK.md#storage-initialization)
and [retention operations](docs/RUNBOOK.md#retention-operations) for index
reconciliation, update restrictions and asynchronous bucket deletion.

Regular collections hold device configurations/acknowledgements, versioned
workspace settings, upload receipts/leases and per-instance worker heartbeats.
They are operational state, not measurements or alternative ingestion triggers.

## Documentation ownership

| Document | Source of truth |
| --- | --- |
| [README](README.md) | Interviewer-facing product overview and navigation |
| [This blueprint](PROJECT-BLUEPRINT.md) | Code map, boundaries and persistence rationale |
| [API](docs/API.md) / [ingestion](docs/INGESTION.md) / [encryption](docs/ENCRYPTION.md) | Detailed contracts and guarantees |
| [Runbook](docs/RUNBOOK.md) | Local/Kubernetes setup, tests and operations |
| [Frontend](frontend/README.md) / [GCP](infra/gcp/README.md) | Component-specific setup and deployment |
| [Implementation status](docs/IMPLEMENTATION_STATUS.md) | Capability/test inventory and known limitations |
| [Plan](docs/PLAN.md) | Actionable verification gates; links instead of copied design |

Keep one canonical explanation and link to it from other guides.
The README's [protected manual roadmap](README.md#future-roadmap--architecture-enhancements)
is author-maintained prose, excluded from automated editing and planning.
