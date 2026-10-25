# Amazing HTTP Logger

A proof-of-concept platform for collecting HTTP traffic from device agents,
storing it securely, and investigating it through an authenticated management
console. Built with **Python/FastAPI, MongoDB time-series, Next.js SSR and Auth0**.
This repository implements the backend and console, not the device interceptor;
it supports threat investigation rather than automated threat detection.

## What the product does

Agents upload HTTP events: timestamps, methods, URLs, status codes, headers,
optional bodies, network endpoints, sizes, timing and process context.
The backend binds each upload to its authenticated device rather than trusting
identity fields in the payload.

Operators use the dashboard to inspect/filter traffic and request details,
register devices, configure upload encryption, review agent acknowledgements,
choose field storage/retention policies, and check pipeline health.
Agents can access only their own device; `manage:devices` grants fleet-wide
management. Registration does not issue credentials.

Sensitive bodies, authorization headers and cookies are **encrypted by default**.
Operators can instead discard them or explicitly retain plaintext. API responses
always redact credential values; authorized detail views can display retained
bodies. Ordinary/suspicious log retention defaults to 7/30 days.

## Core ingestion flow

```text
Agent -> Collector -> GCS raw spool -> object-event Pub/Sub
            |                               |
       202 after durable write         Parser / protection worker
                                            |
                                       silver Pub/Sub
                                            |
                                       Rate-limited Mongo loader
                                            |
                                       MongoDB -> raw cleanup
                                            |
                                       Analysis API -> SSR dashboard
```

The collector durably stores original gzip/JWE bytes **before** acknowledging.
Decryption, decompression, validation and field protection happen in one worker;
a separately scalable worker buffers MongoDB writes. Pub/Sub decouples the
stages and isolates failures. Local development uses MinIO and the Pub/Sub emulator.

**Important boundaries:** `202` means raw receipt, not validated or queryable logs.
Clients poll upload status. Raw storage is a temporary replay source, not a cold
archive, and can contain fields later discarded by policy. Raw deletion follows
confirmed Mongo persistence; failures retain it for recovery. Delivery is
at least once, not exactly once. Deployment analysis reads secondaries without
primary fallback.

See the [pipeline design](docs/INGESTION.md) for contracts, medallion mapping,
idempotency, buffering, DLQs and latency tradeoffs.

## Try it locally

Requires Python 3.11+, Node.js 22.13+/24+ LTS, Docker/Compose and configured Auth0
API/frontend applications. From the repository root:

```bash
make install
cp .env.example .env
# Set the backend Auth0 tenant and API audience.
make frontend-install
cp frontend/.env.example frontend/.env.local
# Configure frontend Auth0 credentials and a random session secret.
make dev                         # API, both workers and Docker dependencies
```

In another terminal, run `make frontend-dev`, then open
**http://localhost:8080**. On native Linux, use
`make dev PROXY_SERVICE=proxy-host` instead.

[Local setup and ports](docs/RUNBOOK.md#local-setup) and
[frontend/Auth0 setup](frontend/README.md#run-locally) cover configuration.
Use synthetic data: local OpenBao is ephemeral, and restarting it loses encryption
keys even though MongoDB/MinIO volumes persist. `make dev-down` stops containers
without deleting those volumes.

## Evaluate and deploy

| Area | Guide |
| --- | --- |
| Original requirements and evaluation context | [Assignment](ASSIGNMENT.md) |
| Architecture, trust boundaries and Mongo document model | [Project blueprint](PROJECT-BLUEPRINT.md) |
| HTTP endpoints, identity, payloads and validation | [API reference](docs/API.md); live [OpenAPI](http://localhost:8080/docs) |
| Upload pipeline and delivery guarantees | [Ingestion design](docs/INGESTION.md) |
| JWE, workspace encryption and key rotation | [Encryption protocol](docs/ENCRYPTION.md) |
| Recovery, Status page, tests and latency measurement | [Runbook](docs/RUNBOOK.md) |
| UI screens, Auth0 setup and browser tests | [Frontend guide](frontend/README.md) |
| GCP: direct Cloud Run URLs, IAM, secrets and deployment | [GCP deployment](infra/gcp/README.md) |
| Optional Kubernetes/Helm deployment | [Kubernetes runbook](docs/RUNBOOK.md#optional-local-kubernetes-deployment) |
| Implemented capabilities and remaining limitations | [Implementation status](docs/IMPLEMENTATION_STATUS.md) |

Quick checks: `make test`, `make lint`, `make frontend-test` and
`make frontend-test-browser`. [Service-backed tests](docs/RUNBOOK.md#run-tests)
and [latency benchmarks](docs/RUNBOOK.md#measuring-ingestion-latency) are opt-in.
No production speedup or latency guarantee is claimed. The dashboard overview
shows sample metrics; classification, aggregation and live streaming are not
implemented. GCP deployment needs independently provisioned MongoDB Atlas.

## Future Roadmap & Architecture Enhancements

> **Manually written, protected section.** Preserve the text below verbatim.
> Do not edit it or use it as input to plans, implementation tasks or automated
> roadmap updates. These are the author's future ideas, not implemented features
> or authoritative descriptions of the current architecture.

- **Tiered Client Attestation**: Implementation of hardware-backed remote attestation (utilizing TPM 2.0 or secure enclaves) for sensitive environments, paired with lightweight cryptographic runtime checks for standard endpoints, ensuring rigorous verification of client binary integrity before log ingestion.
- **Automated Client Onboarding & OAuth Provisioning**: Transitioning from manual pre-shared keys to a programmatic client registration and OAuth 2.0 token issuance pipeline, enabling dynamic credential lifecycle management and role-based scope enforcement.
- **Privacy-Preserving Multi-Tenancy**: Strict architectural isolation across ingestion, processing, and storage to segregate individual customer device data, coupled with a privacy-aware threat intelligence layer that abstracts and reuses malicious log indicators (IoCs) to enforce proactive mitigation policies across other tenants without compromising client data privacy.
- **Real-Time SSR Telemetry via Server-Sent Events (SSE)**: Introduction of a live streaming layer leveraging SSE to push real-time updates of incoming log flows, metrics, and security flags directly to the Server-Side Rendered (SSR) frontend dashboard for low-latency operational visibility.
