# API reference

All versioned endpoints are under `/api/v1`. Interactive [OpenAPI](http://localhost:8080/docs)
documents request/response examples and each operation's permissions. Swagger
does not gzip or encrypt the displayed batch JSON; send actual wire bytes with
an HTTP client. Operational setup belongs in the [runbook](RUNBOOK.md).

## Identity and permissions

Auth0 is the issuer; clients authenticate directly (Authorization Code with PKCE
for user-device agents). The backend never issues tokens or proxies IdP
authorization/token endpoints. All `/api/v1` requests require
`Authorization: Bearer <access-token>`; proof-bound tokens are unsupported.
Tokens require `exp`, `iat`, `iss`, `aud` and a nonempty `sub`. The official
`Auth0FastAPI.require_auth()` verifies RS256 tokens, required claims, tenant and
audience, with discovery/JWKS caching. Invalid tokens return `401`; unexpected
SDK/dependency failures return `503`. SDK failures are sanitized; the upstream
Auth0/Authlib JOSE deprecation warning remains visible. Application JWE uses
`joserfc`, independently of access-token signatures.

| Trusted field | Source |
| --- | --- |
| `device_id` | Verified `https://amazing-http-logger/device_id`, otherwise `sub` |
| Optional OAuth `client_id` | Verified `azp`, otherwise verified `client_id`, otherwise `null` |
| Upload `source_ip` | ASGI connection after the deployment's trusted ingress handling; `null` without a peer |
| Record/batch IDs, `received_at`, `suspicious` | Server-generated, never accepted from agent events |

Invalid identity claims fail with `401`, not fallback. Configure device claims
from trusted issuer-side data, never editable user metadata or token-request
values. Without a dedicated claim there is one logical device per principal;
separate physical devices need separately bound credentials/claims.

Resources use globally unique device IDs, **not user/device pairs or tenants**.
Ordinary tokens access their own device; `manage:devices` grants global fleet
reads/management. Explicit queries for another device return `403`; another
device's log ID returns `404`. Agent status reports must match the JWT-bound
device even with management permission. No user resource key or JWT is persisted.
Use `/me` to discover the correct device ID; registering another ID cannot change it.

Configure only real gateways in `FORWARDED_ALLOW_IPS`, restrict direct backend
access and sanitize forwarding headers. GCP has a
[platform-specific ingress adapter](../infra/gcp/README.md#5-deploy-services).
Uploaded fields cannot replace identity or peer IP. Event timestamps, traffic
endpoints and all captured observations remain agent-reported, not independently
verified facts; upload IP may identify a NAT/proxy rather than a physical device.

## Endpoints

| Method | Resource | Behavior / permission |
| --- | --- | --- |
| GET | `/api/v1/me` | Verified device identity and optional OAuth client |
| GET, PATCH | `/api/v1/workspace/configuration` | Workspace policies; `manage:devices`, expected version for PATCH |
| GET | `/api/v1/status` | Pipeline health; `manage:devices` |
| POST | `/api/v1/devices` | Register named device and initial policy; `manage:devices` |
| GET | `/api/v1/devices` | Accessible stored inventory, keyset pagination |
| GET | `/api/v1/devices/{id}` | Stored device, policy and acknowledgement; absent record `404` |
| GET | `/api/v1/devices/{id}/configuration` | Effective policy and active public key |
| PATCH | `/api/v1/devices/{id}/configuration` | Policy CAS; `manage:devices` |
| PUT | `/api/v1/devices/{id}/configuration-status` | Own-device capability/version acknowledgement |
| POST | `/api/v1/traffic-log-uploads` | Durable raw upload |
| GET | `/api/v1/traffic-log-uploads/{upload_id}` | Authorized receipt, validation, persistence and cleanup status |
| GET | `/api/v1/traffic-logs` | Accessible traffic, time filters and cursor |
| GET | `/api/v1/traffic-logs/{id}` | Accessible event details; inaccessible/absent `404` |
| GET | `/health` | Process liveness, not persistence/replica readiness |

## Register a device and configure its policy

```http
POST /api/v1/devices
Content-Type: application/json
Authorization: Bearer <access-token>

{"device_id":"issuer-bound-device","name":"Operator laptop","encryption_mode":"optional"}
```

Registration atomically inserts name, server-generated `registered_at` and initial
policy using primary/majority, returning `201`, `Location` and version 1.
IDs/names are globally unique and case-sensitive; frontend forms trim surrounding
whitespace. Existing IDs (including policy/status-only records), names and concurrent
duplicates return `400` without overwriting. Registration is create-only and
does not accept `expected_version`.

Names are 1-128 characters; IDs are 1-255 characters and one URL path segment.
Blank/control-containing text and extra fields are rejected. JWE modes require
available public keys before writing. Registration is inventory, not an ingestion
gate or Auth0 client/credential provisioning. Existing unregistered agents and
version-zero policies remain supported.

`GET /devices?limit=25` returns `{items, next_after}`, ordered by device ID.
Pass `next_after` unchanged as `after`; limit is 1-100. Inventory includes all
accessible persisted configuration records, including unnamed/version-zero ones.
`name`/`registered_at` can be `null`. Unknown `/configuration` lookups return
effective TLS-only/version-zero defaults without creating inventory entries.
A stored record or policy does not prove an agent is online.

Policy PATCH uses `expected_version`; stale edits return `409`. Inventory,
policy enforcement and acknowledgements use primary/majority, separately from
analysis routing. Modes are `tls_only`, `optional` and `required`. Agents must
implement polling/JWE and report their applied version; the backend cannot
upgrade agents. See [configuration and acknowledgement protocol](ENCRYPTION.md#management-protocol).

## Upload a batch

Plain uploads use `Content-Type: application/json` and `Content-Encoding: gzip`.
The expanded body is `{"logs":[...]}` with 1-500 events, at most **1 MiB gzip /
8 MiB expanded**. Malformed/truncated gzip, trailing data and concatenated members
are rejected. Encrypted uploads use `application/jose` and at most **1,500,000
wire bytes**; see the [JWE protocol](ENCRYPTION.md#encrypted-ingestion).
Wrong media encoding returns `415`, wire overflow `413`, device transfer-policy
violations `403`, and unavailable raw storage/policy services `503`.

```bash
printf '%s' '{"logs":[{"timestamp":"2026-10-24T05:00:00Z","method":"GET","url":"https://example.com/","status_code":200}]}' |
  gzip -c |
  curl -X POST http://localhost:8080/api/v1/traffic-log-uploads \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -H "Content-Encoding: gzip" \
    --data-binary @-
```

Use synthetic data and a short-lived token; never paste tokens into tickets/logs.
`202` means raw durability, not content validity. Optional device-scoped
`X-Batch-Id` returns `200` for identical bytes/format, `409` for conflicts.
Poll `/traffic-log-uploads/{upload_id}` for `accepted_count`, sanitized errors,
`stored` and `raw_deleted`. See [receipt/delivery guarantees](INGESTION.md).

### Event example

Fields after `status_code` are optional:

```json
{
  "timestamp": "2025-01-15T10:23:45.123Z",
  "method": "POST",
  "url": "https://api.example.com/v1/login",
  "status_code": 200,
  "request_headers": {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
  "response_headers": {"Content-Type": "application/json"},
  "captured_source_ip": "192.0.2.10",
  "destination_ip": "203.0.113.50",
  "source_port": 49152,
  "destination_port": 443,
  "http_version": "2",
  "request_size_bytes": 256,
  "response_size_bytes": 1024,
  "duration_ms": 12.5,
  "process_name": "chrome.exe",
  "process_id": 1234,
  "request_id": "agent-request-123"
}
```

The event, not merely its hash, is retained according to workspace policy.
API `source_ip` is the trusted **upload peer**; use `captured_source_ip` for
observed traffic. Destination IPs are validated literals, not DNS-resolved or
independently verified. Missing optional observations return `null`; maps return
`{}`. Legacy input `headers` aliases `request_headers`; sending both is invalid.
Reads retain the deprecated `headers` alias and historical-record support.
HTTP records are flat; the [Mongo model](../PROJECT-BLUEPRINT.md#stored-document-layout)
is nested internally.

### Strict event validation

Gzip and decrypted JWE share one closed schema. Any invalid event rejects the
**entire batch asynchronously before silver publication**. Wire overflow is
synchronous `413`; expanded/content failures are asynchronous and never echo values.
Do not send attribution, metadata, record/batch IDs or ingestion timestamps.

| Fields | Validation |
| --- | --- |
| Batch/records | UTF-8 JSON, no duplicate keys/unknown fields; 1-500 records |
| `timestamp` | ISO 8601 with seconds/timezone, normalized UTC; no epoch numbers/strings |
| `method` | HTTP token, at most 16 characters, normalized uppercase |
| `url` | HTTP(S), at most 8192 characters; no credentials, raw controls or surrounding whitespace |
| `status_code` | Integer 100-599 |
| Each header map | At most 100 entries / 32 KiB combined UTF-8 key/value bytes; names at most 256 characters, values 8192; no controls/case-insensitive duplicates |
| Header names | HTTP tokens; dots, dollar signs and prototype-map keys rejected |
| Captured IPs / ports | IPv4/IPv6 literals, no zone IDs; integer ports 1-65535 |
| `http_version` | `1.0`, `1.1`, `2` or `3` |
| Message sizes | Integer 0 through signed BSON int64 maximum; no numeric-string/Boolean coercion |
| `duration_ms` | Finite nonnegative milliseconds, at most 24 hours; no strings/NaN/infinity |
| Process name / PID | Nonblank, control-free name at most 255 characters; integer PID 0-2147483647 |
| `request_id` | At most 128 ASCII letters/digits/`._:-`, starting alphanumeric |
| Optional `body` | Valid UTF-8 string, at most 1,000,000 characters |

Captured text remains **untrusted data**, not executable queries or HTML.
Validated traffic can contain attack attempts worth investigating. Queries use
fixed server-owned operators/typed values, never `$where`, eval or uploaded query
expressions. The dashboard escapes text; exports/analytics must preserve
output-context escaping and parameterized queries.

## Workspace configuration

GET/PATCH require `manage:devices`; PATCH uses `expected_version`, with stale
edits returning `409`. Primary/majority settings govern all devices.

PATCH requires `encrypt_request_body`, `encrypt_authorization_header` and
`encrypt_cookies`. Strict Boolean `retain_request_body`,
`retain_authorization_header` and `retain_cookies` default true and preserve
their saved value when omitted. `retain_*=false` overrides encryption.
KMS is validated only for retained encrypted categories.

Field policies apply to **future uploads**, not historical migration:
discard, retain encrypted (default), or explicitly retain plaintext.
Discarded bodies keep UTF-8 length/SHA-256; discarded credential/cookie entries
are removed from both maps. Collections omit bodies; authorized details can
decrypt retained bodies. Credential values remain redacted in all responses;
`X-Api-Key` is always discarded/redacted. See
[workspace storage encryption](ENCRYPTION.md#workspace-storage-encryption)
for algorithm, key separation, rotation and fail-closed behavior.

Retention fields are `retention_enabled`, `retention_days`,
`suspicious_retention_enabled` and `suspicious_retention_days`; omitted values
are preserved. The UI saves storage and retention together with one version.
Retention affects **existing and future records**, unlike field policies.
See [retention operations](RUNBOOK.md#retention-operations) for windows, TTL
semantics and partial failures. A `503` save can leave desired settings persisted;
reload/reconcile rather than assuming nothing changed.

## Analyze logs

`GET /traffic-logs` supports `device_id`, timezone-aware `start_time` (inclusive),
`end_time` (exclusive), `limit` (1-100, default 50) and opaque `cursor`.
Order is descending event timestamp/record ID. Pass `next_cursor` unchanged with
identical filters; `null` ends pagination. Concurrent/late events mean pages are
not a snapshot. No aggregation endpoint is implemented.

Deployments use strict `SECONDARY`/majority analysis; unavailable secondaries
return `503`, **never primary fallback**. Single-node local development explicitly
enables guarded primary analysis. Replication lag can hide a newly stored event,
including a temporary item `404`. A `stored` receipt confirms primary persistence,
not immediate secondary visibility. Agents cannot set the exposed `suspicious`
flag; classification writes are not implemented.
