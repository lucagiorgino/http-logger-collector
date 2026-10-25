# Implementation status

Capability and evidence inventory; see the [README](../README.md) for the product
and the linked guides for contracts. Automated tests are not live deployment or
production performance evidence.

## Implemented

| Capability | Contract / operations | Test evidence |
| --- | --- | --- |
| Auth0 RS256 identity, device isolation and fleet permission | [API identity](API.md#identity-and-permissions) | [`test_auth.py`](../tests/test_auth.py), [`test_devices.py`](../tests/test_devices.py) |
| Durable raw uploads, retry IDs, async parsing, silver transport, throttled loading, DLQs and cleanup | [Pipeline](INGESTION.md), [recovery](RUNBOOK.md#retry-backpressure-and-recovery) | [`test_uploads.py`](../tests/test_uploads.py) |
| Named registration/inventory, versioned policy and agent acknowledgement | [Device API](API.md#register-a-device-and-configure-its-policy) | [`test_devices.py`](../tests/test_devices.py) |
| JWE suites, wrapped EC key sets, rotation and fail-closed field protection | [Encryption](ENCRYPTION.md) | [`test_encrypted_batches.py`](../tests/test_encrypted_batches.py), [`test_encryption_keys.py`](../tests/test_encryption_keys.py), [`test_openbao.py`](../tests/test_openbao.py) |
| Workspace discard/encryption and ordinary/suspicious partial TTL | [Workspace API](API.md#workspace-configuration), [retention](RUNBOOK.md#retention-operations) | [`test_workspace.py`](../tests/test_workspace.py), [`test_mongo_integration.py`](../tests/test_mongo_integration.py) |
| Strict-secondary analysis and timestamp/ID cursors | [Analysis API](API.md#analyze-logs), [data model](../PROJECT-BLUEPRINT.md#stored-document-layout) | [`test_analysis.py`](../tests/test_analysis.py), [`test_mongo_integration.py`](../tests/test_mongo_integration.py) |
| SSR dashboard/forms, traffic details, devices, workspace and portable Status diagram | [Frontend](../frontend/README.md), [probe semantics](RUNBOOK.md#pipeline-status) | [`frontend/tests/`](../frontend/tests/), [`test_status.py`](../tests/test_status.py) |
| Local Compose dependencies with host API/workers; optional hardened Helm workloads | [Runbook](RUNBOOK.md#local-setup) | [`tests/`](../tests/), [browser validation](../frontend/README.md#validation) |
| Direct Cloud Run URLs, scoped cloud IAM, KMS adapter and missing-only secret bootstrap | [GCP guide](../infra/gcp/README.md) | [`infra/gcp/tests/`](../infra/gcp/tests/) |

## Validation scope

The [test runbook](RUNBOOK.md#run-tests) separates fast tests from opt-in services:
real Mongo indexes/replica routing/CAS, isolated OpenBao key operations, MinIO
notifications through emulator/Mongo, proxy forwarding and two-device live OAuth.
GCS conditional operations use mocked SDK calls; Terraform tests use mocked
providers. Frontend mocks verify SSR/forms without JavaScript; a separate fixture
exercises real FastAPI/Mongo but replaces Auth0/KMS. Only live-tenant tests validate
actual OAuth grants. See [frontend validation](../frontend/README.md#validation).

## Known limitations

- At-least-once delivery can duplicate ambiguously acknowledged time-series
  inserts. Acknowledgement means raw receipt; stored status does not eliminate
  secondary visibility lag.
- Raw can contain subsequently discarded values. Failed/pending raw and retry
  receipts have no age TTL; define an approved abandonment procedure.
- Status probes/heartbeats are snapshots, not end-to-end guarantees or queue/DLQ
  depth. Keep native backlog and error alerts.
- Overview metrics are samples, not aggregation. Classification, raw/DLQ
  management screens, multi-tenancy, attestation and real-time SSE are absent.
- Registration does not provision Auth0 credentials; external agents must
  implement configuration polling and encryption themselves.
- Ephemeral local OpenBao loses keys on restart. Cloud/local ciphertext are not
  interchangeable; backup recovery depends on retaining the correct keys.
- No production p50/speedup claim or live GCP deployment validation is recorded.

The [verification plan](PLAN.md) owns remaining deployment gates; do not copy
contracts or derive tasks from the README's protected manual roadmap here.
