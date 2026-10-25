# Amazing HTTP Logger project blueprint

This document defines the repository structure and engineering conventions for the Python backend implementation. It is intentionally implementation-focused and avoids repeating the original interview brief or writing a second project description.

---

## 1. Repository structure

The repository is a Python service with a minimal, layered application structure and a clear separation between API layer, business logic, persistence, and tests.

```text
amazing-http-logger/
  AGENT.md                     # coding guidance for the repository
  README.md                    # setup, commands, API overview, local run instructions
  Makefile                     # install, test, lint, run, image, Helm lifecycle
  pyproject.toml               # optional project metadata and tooling config
  requirements.txt             # Python dependencies
  .gitignore                   # secrets first: .env, *.key, *.pem, .venv, __pycache__
  .env.example                 # sample environment variables
  docker/
    Dockerfile                 # application container image
    docker-compose.yml         # development MongoDB/OpenBao; backend runs on the host
  helm/amazing-http-logger/
    Chart.yaml                 # Kubernetes deployment chart
    values.yaml                # external MongoDB and HTTPS OpenBao by default
    values-development.yaml    # optional replica set and ephemeral OpenBao
    templates/                 # API, MongoDB, OpenBao, bootstrap, policy, smoke test
    files/init-replica-set.js   # idempotent replica-set initialization
  app/
    __init__.py
    main.py                     # FastAPI app factory and route registration
    kms.py                      # OpenBao provisioning and EC/KEK rotation CLI
    deps.py                    # shared dependencies (DB session, auth checks)
    api/
      __init__.py
      auth.py                   # /api/v1/me; login/token issuance stays with Auth0
      batches.py                # gzip/JWE /api/v1/traffic-log-batches ingestion
      devices.py                # device encryption policy and capability/status APIs
      traffic_logs.py           # owner-scoped collection/item analysis reads
    core/
      config.py                 # settings and environment loading
      encryption.py             # JWE decryption and unwrapped EC key-set validation
      openbao.py                # KMS authentication, Transit wrapping, KV access
      security.py               # JWT validation, token handling, auth helpers
      logging.py                # structured logging and audit hooks
    db/
      mongo.py                  # time-series storage, primary writes, secondary reads
      models.py                 # document models / ODM schemas
    schemas/
      traffic.py                # log payload schemas
      devices.py                # device configuration and status schemas
    services/
      traffic_service.py        # receive, validate, and persist traffic logs
      ingestion.py              # replaceable publisher and direct MongoDB adapter
      analysis.py               # cursor handling and stored-log representations
  tests/
    conftest.py                 # shared fixtures and app test setup
    test_auth.py                # auth flow tests
    test_batches.py             # compressed ingestion, validation, authorization
    test_analysis.py            # owner-scoped log reads and pagination
  integration_tests/
    stack_client.py             # HTTP-only, threaded OAuth device emulator
    test_stack.py               # opt-in encrypted/plain whole-stack scenario
  frontend/
    app/                        # JavaScript/JSX Next.js SSR pages and policy action
    components/ui/              # JavaScript shadcn/ui registry components
    lib/                        # server Auth0 session and private API adapter
    tests/                      # focused adapter tests and mocked browser flows
    .env.example                # separate private frontend configuration
    README.md                   # UI architecture, Auth0 setup and run commands
  docs/
    PLAN.md                     # design, scope, architecture, decisions
    IMPLEMENTATION_STATUS.md    # status by area, gaps, next steps, test inventory
    RUNBOOK.md                  # run, debug, and maintenance procedures
```

Rules:
- The project is a Python app, not a Go workspace, and it is not structured as several independent modules.
- `app/api` handles HTTP requests and call into `app/services`.
- `app/services` contains domain logic and orchestrates calls into persistence or security utilities.
- `app/db` is the only layer that talks to MongoDB.
- Batch ingestion hands normalized, minimized records to `IngestionPublisher`;
  Redis Streams durably accepts them before HTTP 200. A separate worker batch-inserts
  into MongoDB and acknowledges only after majority persistence, with pending-message
  recovery and best-effort replay deduplication, not exactly-once delivery.
- Deployment analysis uses strict secondary routing without fallback. The single-node
  local profile explicitly selects primary reads, guarded to development only.
- Device policy enforcement uses primary/majority reads, not analysis replicas.
  OpenBao Transit protects EC keys stored as ciphertext in KV v2. Backend processes
  unwrap keys into RAM and decrypt JWE locally; clients receive only public keys.
  No plaintext EC files or private EC Secrets are used. See `docs/ENCRYPTION.md`.
- MongoDB 7+ time-series retention uses separate partial TTL indexes on the time
  field for ordinary and suspicious metadata buckets, never a collection-wide TTL.
  Workspace policy defaults to 7/30 days and can independently disable either.
  Suspicious classification is server-owned; the analysis job is not implemented.
- See `docs/PLAN.md` for the time-series schema and queue contract, and
  `docs/RUNBOOK.md` for replica-set setup and deployment commands.
- `tests/` validates behavior and imports the application package, not vice versa.
- Secrets live only in git-ignored files/directories such as `.env` and `.secrets/`,
  or externally provisioned Kubernetes Secrets.
- Local development uses MongoDB/OpenBao Compose and host Python with auto-reload.
- Docker builds deployment images; Kubernetes and Helm manage deployment.

---

## 2. AI layer

### 2.1 `AGENT.md`: what every session needs, nothing more

Use this skeleton:

````markdown
# AGENT.md
Guidance for the coding agent in this repository. It contains only the persistent instructions needed for this project.

## What this is
This repository implements the backend service for collecting HTTP traffic generated by client agents and persisting it for security analysis.

The success criterion is a working backend that authenticates users and agents, accepts traffic logs securely, and stores them in MongoDB without leaking sensitive data.

The core architectural invariant is that all inbound traffic is validated, authenticated, and logged with explicit security checks before it reaches persistence.

## The docs
Read the relevant file before starting a task and update it when the design changes.
- docs/PLAN.md: current design and decisions.
- docs/IMPLEMENTATION_STATUS.md: status, gaps, and test coverage.
- docs/RUNBOOK.md: local setup and operational steps.

## Always keep in mind
- Secrets belong in `.env`, never in source files or logs.
- Authentication and authorization are mandatory for every sensitive route.
- Data minimization matters: do not log raw secrets, tokens, or credentials.
- Keep the application Python-first and avoid introducing unrelated infrastructure complexity.

## Commands
Prerequisites: Python 3.11+, pip, Docker, and Docker Compose.
Kubernetes, kubectl, and Helm are needed only for deployment.

```bash
make install
make dev
make test
make lint
make run
make dev-down
make image
make helm-lint
make helm-up
make helm-test
```

## Architecture in brief
- `app/main.py` creates the API app and registers all routes.
- `app/api/` exposes endpoints for auth and traffic ingestion.
- `app/services/` contains business logic for authorization, ingestion, and log handling.
- `app/db/` persists data to MongoDB.
- `app/core/` contains config, security helpers, and structured logging.

## Change these together
- API route and schema: if the request body changes, update both the HTTP contract and validation schema.
- Service logic and tests: every auth or ingestion rule must be covered by tests.
- Storage model and persistence code: schema changes must be reflected in MongoDB models and any consumer code.

## Tests
- Unit tests cover validation, auth decisions, and service logic.
- Integration tests validate MongoDB-backed flows.
- Security tests ensure unauthorized access is rejected and sensitive data is not logged.

## Conventions
- Deliberate simplifications should be marked with a brief comment and an upgrade path.
- Documentation reflects current design and current status only; decisions go in the plan document.
````

### 2.2 Memory

Project facts that the code cannot show should live in the design and status docs instead of chat history: the chosen authentication strategy, security assumptions, environment constraints, and any decisions that affect the app design.

### 2.3 Skills: recipes for the repeated kinds of change

A skill is created per repeated change type under `.agent/skills/<verb>-<thing>/SKILL.md`, with a matching `places.md` file when needed. The front matter should describe when the skill applies.

````markdown
---
name: add-auth-flow
description: Use when someone asks to add authentication flows, token validation, or role-based access checks.
---
# Adding an auth flow
One new auth flow, with the schema, service logic, tests and docs, as a single change.

## Rules
1. Ask when there are multiple viable auth strategies.
2. Keep the change focused on the auth flow and what it needs.
3. Do not bypass security validation or silently weaken authorization.
4. Never hardcode secrets.
5. Update tests and docs in the same change.
````

Example skills: `add-auth-flow`, `add-traffic-log-handler`, `add-storage-model`.

---

## 3. Backend architecture

The backend is a Python API application built around FastAPI, with explicit layering and strong validation.

```text
app/main.py
   ↓
app/api/
   ↓
app/services/
   ↓
app/db/
   ↓
MongoDB
```

Key rules:
- `app/api` should only parse requests, validate input using schemas, and delegate to services.
- `app/services` should contain business logic: auth, authorization, traffic validation, storage orchestration.
- `app/db` should handle MongoDB access, queries, and document persistence.
- `app/core/security.py` centralizes token validation, permission checks, and sensitive data handling.
- `app/core/logging.py` should provide structured logs without leaking secrets or raw payload data.
- Every sensitive route must enforce authentication and authorization before processing the request.

The implementation centers on two concerns:
- authentication and authorization flows for users and agents
- ingestion and storage of HTTP traffic records generated on devices

A successful design supports:
- per-user and per-agent identity validation
- secure token issuance and verification
- safe handling of HTTP metadata, headers, and payload fragments
- strict validation of inbound traffic log content
- auditing and structured logging for security analysis

---

## 4. Data model and storage

The project stores captured HTTP traffic data in MongoDB.

Key responsibilities:
- `app/db/models.py` defines the documents for users, agents, sessions, traffic logs, and access events.
- `app/schemas/traffic.py` validates incoming log payloads.
- `app/services/traffic_service.py` is responsible for transforming raw requests into valid stored records.
- Sensitive fields such as credentials, cookies, tokens, and authorization headers should be redacted or excluded by policy.

The system should support queries such as:
- the latest traffic logs for a given user or device
- logs matching a time window or suspicious pattern
- security findings associated with an agent or user session

---

## 5. Security expectations

Security is not optional in this project. The app should be implemented with strong defaults.

Required behaviors:
- validate all incoming data with Pydantic schemas
- enforce JWT or equivalent token validation for protected routes
- deny unauthenticated requests by default
- restrict authorization according to user or agent role
- redact sensitive data before logging or persistence
- avoid storing raw secrets or credentials
- use HTTPS in production and environment-based configuration
- take a least-privilege approach to database access

Security tests should ensure that:
- unauthorized access is rejected
- invalid tokens are rejected
- malformed agent payloads are rejected
- unauthenticated traffic upload attempts fail
- sensitive fields are redacted or excluded from logs and storage

---

## 6. Tests

| Level | Where | Notes |
|---|---|---|
| Unit | `tests/` next to the app modules | Validate schema parsing, service logic, validation rules, and auth decisions |
| Security | `tests/test_security.py` | Confirm denial-by-default behavior and data redaction |
| Integration | `tests/` with a test MongoDB fixture or container | Validate agent traffic ingestion and persistence end-to-end |
| API | `tests/test_auth.py`, `tests/test_agents.py` | Assert HTTP contracts and error responses |

Recommended test rules:
- write failing tests before implementing new auth or logging logic
- test both success and refusal paths
- validate JSON schema and response codes
- use realistic but sanitized sample payloads

---

## 7. Makefile

The `Makefile` should cover the standard workflows needed for the project.

Recommended targets:
- `make install` — create a virtual environment and install dependencies
- `make test` — run all unit and integration tests
- `make lint` — run formatters and linters such as ruff or flake8
- `make run` — launch `python -m app` using the selected `.env` file
- `make dev` — start MongoDB/OpenBao Compose and host Python with auto-reload
- `make dev-up/down` — start or stop both development dependencies
- `make mongo-up` — start the one-container, single-node development replica set
- `make mongo-down` — stop local MongoDB, retaining its volumes
- `make openbao-up` — start/bootstrap ephemeral OpenBao and refresh its runtime token
- `make openbao-down` — stop OpenBao, losing its in-memory key state
- `make image` — build the application OCI image
- `make helm-lint` — validate the configured Helm chart
- `make helm-up` — install or upgrade the Kubernetes release
- `make helm-test` — test the deployed Service and API schema
- `make helm-down` — uninstall the release, retaining MongoDB PVCs
- `make helm-port-forward` — expose the Kubernetes API on a separate configurable local port
- `make check` — run a structured validation pass for the project

The Makefile should reflect a Python workflow and avoid Go-specific commands such as `go mod tidy` or `go test`.

---

## 8. Docs

The documentation should stay aligned with the real project goals.

- **Plan doc:** scope, requirements, architecture, threat model, data model, API design, and decisions
- **Implementation status doc:** what is built, what is tested, what is open, and where the risks remain
- **Runbook:** setup, troubleshooting, local environment notes, and operational guidance
- **README:** prerequisites, commands, sample endpoints, and local development instructions

The project should maintain a clean separation between current state and historical notes. Decisions go into the design document, not into scattered prose.

---

## 9. Commits

One commit per significant decision. Each commit should be a single, well-scoped update that explains both what changed and why.

The message should read like a clear feature update, for example:

```bash
git commit -m "feat: add traffic log ingestion API with auth validation"
```

Keep commit scope aligned with the actual work: schemas, services, tests, or docs together when they are part of the same decision.

---

## Bootstrap checklist for a new project

1. Create the repository root and initialize the Python project files (`requirements.txt`, `pyproject.toml`, `.gitignore`, `.env.example`).
2. Create the `app/` package structure and keep the layering clear: API → services → persistence.
3. Add the FastAPI app entry point and route registration in `app/main.py`.
4. Implement auth and traffic-log endpoints using validated Pydantic schemas.
5. Add MongoDB models and persistence utilities in `app/db/`.
6. Add unit and integration tests for auth, ingestion, and authorization rejection paths.
7. Write the docs: `README.md`, `PLAN.md`, `IMPLEMENTATION_STATUS.md`, and `RUNBOOK.md`.
8. Fill in `AGENT.md` with the project-specific architecture and rules.

This project should remain Python-first, security-first, and focused on collecting and analyzing HTTP traffic data generated on end-user devices for threat detection and response workflows.