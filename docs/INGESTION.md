<!-- Purpose: Define the durable upload pipeline, processing responsibilities, policy boundaries, retry and delivery guarantees, and recovery constraints. -->

# Ingestion pipeline

This is the canonical pipeline design. See the [API reference](API.md) for wire
schemas and permissions, [encryption protocol](ENCRYPTION.md) for cryptography,
and [runbook](RUNBOOK.md#upload-ingestion) for setup and recovery commands.

For visual review, see the [component/deployment diagrams](IMPLEMENTATION_STATUS.md#architecture-diagrams)
and the [upload acceptance](IMPLEMENTATION_STATUS.md#durable-upload-acceptance),
[parser](IMPLEMENTATION_STATUS.md#parsing-and-protection),
[loader/cleanup](IMPLEMENTATION_STATUS.md#mongo-loading-and-raw-cleanup) and
[recovery](IMPLEMENTATION_STATUS.md#operator-recovery) sequences.

## Flow and responsibilities

```text
POST /api/v1/traffic-log-uploads
  -> authenticate / device policy / wire-size limit / policy snapshot
  -> immutable original bytes in GCS (MinIO locally)
  -> HTTP 202 {upload_id, status: "received", duplicate: false}
              |
       GCS OBJECT_FINALIZE -> object-event Pub/Sub
       Local: MinIO ObjectCreated -> private bridge -> Pub/Sub emulator
              |
  worker #1: JWE decrypt -> bounded gzip / JSON validation -> normalize
             -> snapshot discard rules / field encryption
              |                                \-> parse DLQ
       separate silver Pub/Sub (normalized, protected documents)
              |
  worker #2: per-upload lease / replay checks -> rate-limited Mongo writes
              |                                \-> load DLQ
       majority stored receipt -> delete raw object -> Pub/Sub ACK
```

| Stage | Tier | Responsibility |
| --- | --- | --- |
| Collector | Ingress | Authenticate, bound input, reserve receipt, persist original bytes before acknowledgement |
| GCS / local MinIO | Temporary bronze | Immutable replay source until Mongo persistence |
| `app.spool_worker parse` | Bronze to silver | Decrypt, decompress, validate, normalize, minimize and protect fields |
| Silver Pub/Sub | Transport | Managed backlog, delivery leases, redelivery, ACK and load DLQ |
| `app.spool_worker load` | Silver to gold | Throttled Mongo loading and post-persistence raw cleanup |
| MongoDB `traffic_logs` | Gold | Hot analysis store with workspace-controlled retention |

Parsing/cryptography and Mongo I/O scale and fail independently. The loader has
no EC keys or field-encryption credentials and never reparses the original upload.
There is no new enrichment/classification stage or historical cold tier.

## Acknowledgement and policy boundaries

The collector hashes bounded wire bytes, reads primary-backed device/workspace
policies, reserves a Mongo receipt and waits for a successful raw object write.
It does not decompress, decrypt, count records or encrypt captured fields.
Authentication, transfer policy, format and wire-size checks are synchronous;
content and KMS failures are asynchronous before silver publication.

`202` confirms raw durability, not validation or traffic-log persistence.
The POST response has no event count. Poll the upload resource for:

```text
uploading -> received -> parsing -> queued -> loading -> stored
                        \-> parse_failed     \-> load_failed
```

`accepted_count` remains nullable until known; failures expose a sanitized
`error_code`. A fast worker can advance state before POST returns.
`stored` confirms majority Mongo persistence; `raw_deleted=true` confirms cleanup.
Receipt reads are operational primary reads, not replica-backed analysis.

The parser applies the **workspace policy snapshot taken at acceptance**.
Edits during a backlog do not change already accepted uploads. Discarded fields
are removed before field encryption/silver publication, but **original raw bytes
temporarily include them**. Gzip alone is not encryption. GCS uses encryption at
rest, public-access prevention and scoped IAM; local MinIO is for synthetic data
and does not promise production encryption at rest. Tokens are never saved with
objects; device/source attribution comes from the collector, not uploaded fields.

## Object format and retry protection

Raw keys are partitioned as:

```text
raw/tenantId=<deployment-hash>/date=<YYYY-MM-DD>/agentId=<device-hash>/<upload_id>.upload
```

`SPOOL_TENANT` is a deployment namespace, not authentication multi-tenancy.
One atomic object contains a versioned JSON header, newline and original wire
bytes. The header holds trusted context, policy, checksum and receipt ID.
GCS uses conditional create (`if_generation_match=0`); MinIO uses
`If-None-Match: *`. Object events, not bucket listing or manifest polling,
trigger normal processing.

| Boundary | Enforcement |
| --- | --- |
| Collector | Optional device-scoped `X-Batch-Id`; matching bytes/format return `200` with `duplicate=true`, conflicting reuse returns `409` |
| Raw object / parser | Unique object key, conditional write, atomic stage claims, expiring leases, bounded attempts and terminal duplicate skips |
| Loader | Stable `<upload_id>:<index>` event identities, deterministic record IDs, leases and majority-read missing-ID checks before unordered insertion |

Without `X-Batch-Id`, each request is a new upload. Retry IDs accept 1-128 ASCII
letters/digits/`._:-`. Operational state lives in `ingestion_uploads`; receipts
have no TTL, so completed retry keys do not silently become reusable.

Delivery is **at least once, not exactly once**. Time-series measurements cannot
have unique-ID enforcement or share a transaction with the receipt. Leases
prevent ordinary competing writes, but a network-ambiguous Mongo insert can
still duplicate records. `insert_many(..., ordered=False)` and replay checks
reduce, rather than eliminate, this risk.

## Buffering and backpressure

Object-event and silver subscriptions are separate. Consumers pull one message
at a time, explicitly ACK completed work and recover through delivery-deadline
expiry. Transient load failures release delivery for retry. Managed Pub/Sub
backlog is outside worker memory; there is no unbounded process-local queue.

Silver is normalized, field-protected **gzip BSON**, not raw JWE or JSON.
Encoding/decoding enforce **9,500,000 wire bytes / 32 MiB expanded**, allowing
encrypted-field expansion under Pub/Sub's 10 MB request limit.
Oversized protected output fails parsing with `silver_payload_too_large` and
retains raw. This transport decompression is not reparsing the agent upload.

`SPOOL_MONGO_DOCUMENTS_PER_SECOND` defaults to 500 **per loader replica**, with
at most a one-batch (500-document) initial burst. Multiply by replica count for
total deployment capacity. Loaders wait for capacity **before pulling**, avoiding
expired deliveries while throttled.

GCP subscription retention defaults to 14 days, configurable through
`spool_message_retention_seconds`. If notifications expire or the local emulator
restarts, recover from raw after restoring capacity. Monitor backlog age, quota,
cost and Mongo drain capacity; the emulator is not production durability evidence.

## Dead letters, cleanup and raw retention

Both stages default to three application attempts. Native GCP redrive uses five
delivery attempts (the platform minimum) and exponential retry backoff.
Application DLQs contain sanitized pointers/error codes. Native redrive forwards
the original message, so the load DLQ can include normalized documents with
fields deliberately plaintext by policy. Restrict inspection access and never
dump bodies into logs or tickets. Failed DLQ publication leaves work unacknowledged.

Raw deletion follows majority Mongo persistence **and** a majority `stored`
receipt. Cleanup retries do not repeat log insertion. An object recreated by
an ambiguous late collector PUT triggers cleanup-only work.

Pending/failed raw has **no automatic age TTL**. It remains until success or an
explicitly approved operator deletion. The dedicated GCS bucket disables
versioning and soft deletion; do not add lifecycle expiry, replication or backup
copies without reviewing privacy and replay requirements. Notification expiry
does not delete raw. Mongo hot retention is independent; this spool is not a
months-to-years archive.

Follow the [targeted replay/cleanup procedure](RUNBOOK.md#retry-backpressure-and-recovery)
after repairing dependencies; do not replay active leases or drain DLQs before
recovery. Retain all upload/storage keys required by raw backlog and stored logs.

## Observability and latency

Metrics are emitted as `metric=<name> value=<number>` log records:
`ingest.batch.accepted`, `ingest.batch.duplicate`, `ingest.parse.success`,
`ingest.parse.dlq`, `ingest.load.success`, `ingest.load.dlq`,
`mongo.write.latency_ms` and `pipeline.e2e.latency_ms`.

Pub/Sub provides `subscription/num_undelivered_messages` and
`subscription/oldest_unacked_message_age`. Terraform configures both DLQ-depth
alerts and silver depth/age alerts via `notification_channels`: defaults warn
above 500 messages or 3600 seconds, below retention. Helm operators provision
equivalent cloud alerts. The [Status page](RUNBOOK.md#pipeline-status) shows
metadata checks, worker heartbeats and receipt counts, not native queue/DLQ depth.

**p50 2-10 seconds is a target, not a measured guarantee.** End-to-end latency
includes object-event delivery, consumer polling, parsing/KMS, backlog and Mongo
capacity. Raw upload and receipt writes still affect HTTP acknowledgement;
small batches may not acknowledge faster. Use the
[benchmark procedure](RUNBOOK.md#measuring-ingestion-latency) to compare upload
acknowledgement and observed persistence, rather than claiming production speedup
from unit/emulator tests.
