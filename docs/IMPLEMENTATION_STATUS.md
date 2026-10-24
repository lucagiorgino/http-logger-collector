# Implementation status

## Current status
- Project structure initialized.
- Core FastAPI app skeleton created.
- API, service, publisher, and persistence layers are implemented.
- Auth0 token verification delegates to the official `Auth0FastAPI.require_auth()`
  dependency, using configured tenant/audience and SDK discovery/JWKS caching.
  Bearer challenges and sanitized errors are preserved; unexpected SDK failures return 503.
- Persistence uses native async PyMongo with awaited client shutdown. Runtime/test
  HTTP clients use HTTPX2; Motor is not required. The requested Auth0 SDK currently
  depends on deprecated `authlib.jose`; this upstream import warning remains visible.
- Versioned identity, gzip-batch ingestion, and log collection/item analysis endpoints
  use verified Auth0 bearer tokens. Login/token issuance remains delegated to Auth0.
- Traffic logs are normalized, redacted, and persisted to MongoDB with authenticated ownership checks.
- MongoDB time-series storage uses stable user/device metadata and supports indexed
  event-time queries with keyset pagination.
- Writes use primary with majority acknowledgement; deployment analysis uses strict
  secondary reads. Single-node local development explicitly enables primary analysis,
  guarded against use outside development.
- A replaceable ingestion publisher decouples the HTTP boundary from storage; the MVP
  writes directly to MongoDB rather than running a broker.
- Local development uses MongoDB/OpenBao Compose and host Python with Uvicorn reload;
  backend edits do not require image rebuilds. One self-initializing MongoDB container
  uses loopback port 28017; Python uses 8001. Both load the same parameterized `.env`.
- Development OpenBao uses loopback port 28200, generated private credentials,
  automatic bootstrap, and a separate least-privilege runtime token. Its dev-mode
  storage is ephemeral: restarting it loses keys, unlike MongoDB's persistent volume.
- Kubernetes API/MongoDB pod ports and the separate local port-forward are configurable.
- Helm deploys the API to Kubernetes with hardened pod settings, resource limits,
  health probes, and a ClusterIP Service.
- Default chart values require an existing Secret for external MongoDB; development
  values opt into a persistent three-member MongoDB StatefulSet and bootstrap Job.
- The bundled development database has a headless Service and ingress NetworkPolicy;
  it has no authentication/TLS and must not be used in production.
- Raw request bodies are not stored; body length and SHA-256 are retained for correlation.
- Security-oriented defaults and redaction rules have been documented.
- Device policy/status APIs implement owner-scoped configuration, permission-gated
  changes, atomic versions, and client capability/error reporting.
- JWE application encryption uses configured ECDH/AES suites (default NIST P-384,
  ECDH-ES+A256KW and A256GCM) with bounded decrypt/gzip
  processing, retained rotation keys, and policy enforcement before publishing.
- HTTPS enforcement and trusted-proxy configuration are implemented.
- OpenBao Transit protects generated EC key sets with a non-exportable KEK; only
  ciphertext is stored in KV v2. Backend RAM holds unwrapped EC keys for local
  JWE/ECDH decryption. Plaintext EC files/private EC Secrets are not used.
- CLI operations support initialization, staged EC rotation/activation, retirement,
  and KEK rotation/rewrapping, with compare-and-swap updates and no private-key output.
- Helm supports external HTTPS OpenBao with Kubernetes auth or a runtime token
  Secret and optional custom CA. The development profile includes ephemeral OpenBao,
  TokenReview RBAC, bootstrap Job, and root credentials isolated from API pods.

## Open items
- replace placeholder Auth0 domain and audience values with a real tenant configuration
- validate the token flow against a real Auth0 API and permission grant
- validate MongoDB behavior against production-scale retention and query requirements
- implement durable broker/consumer infrastructure and external idempotency before
  enabling queue retries; partial bulk-write failures and client retries can duplicate records
- provision production MongoDB credentials, TLS, networking, and operational monitoring
- refine logging and redaction policies for production traffic data
- implement the documented polling/encryption protocol in external agent clients;
  provision keys and grant Auth0 `manage:devices` only to intended administrators
- provision persistent production OpenBao, TLS, audit devices, backups, recovery,
  and namespace/ServiceAccount/audience-bound authentication
- if EC keys must never enter backend memory, select a remote ECDH-capable service
  or redesign the client protocol; OpenBao Transit does not offer ECDH JWE decryption

## Test inventory
- API surface and normalized/redacted batch persistence
- gzip batch validation, size limits, ownership checks, and publisher replacement
- owner-scoped analysis, filter validation, and timestamp/ID pagination
- explicit ingestion/read failures and replica-set connection constraints
- time-series initialization, startup races, invalid collection options, and routing
- optional real replica-set integration test: time-series indexes and inserts,
  primary/secondary command routing, replica visibility, pagination, and no primary fallback
- Signed JWT checks exercise the real Auth0 SDK against mocked discovery/JWKS,
  including signature/algorithm and claim rejection, cached keys, bearer challenges,
  and sanitized unexpected SDK failures (real tenant validation pending)
- Deprecation and pending-deprecation warnings fail tests, except the exact upstream
  Auth0/Authlib JOSE import warning, which is reported in the warning summary
- Helm lint/render validation for external and bundled MongoDB profiles, plus a
  Helm smoke-test pod checking the deployed Service and OpenAPI routes
- Isolated Kubernetes validation covers development install/upgrade, external-Secret
  deployment, real time-series/replica integration, and PVC/Secret retention on uninstall
- Local development validation covers MongoDB/OpenBao Compose initialization, host-run
  single-node replica integration, development-only primary reads, and live Python
  reload without rebuilding an image
- Startup tests cover selected environment files, MongoDB URI interpolation,
  environment overrides, missing-file errors, and deployment reload protection
- Device/encryption tests cover authorization, version conflicts, public-only key
  responses, capability reports, policy downgrade rejection, JWE integrity and
  size bounds, retained keys, private credential permissions, and HTTPS enforcement
- Suite tests cover configured P-256/P-521 alternatives, client-advertised algorithms,
  EC key generation/curve validation, non-configured cipher rejection, and unsafe
  configuration or ephemeral-private-key rejection
- Real single-node integration covers persistent policies/status, concurrent
  compare-and-swap, encrypted and plaintext ingestion, and owner isolation
- OpenBao unit tests cover token/Kubernetes authentication, sanitized failures,
  HTTPS enforcement, wrapped-only CAS writes, and incompatible/exportable KEK rejection
- Real OpenBao tests cover Transit unwrap, staged EC rotation/activation/retirement,
  KEK rewrapping, CAS conflicts, denied runtime administrative actions, and
  encrypted ingestion through the API into MongoDB using KMS-managed EC keys
- Isolated Kubernetes OpenBao validation covers two API replicas, real projected-JWT
  authentication and Transit unwrap, install/upgrade bootstrap, root/key retention,
  privileged-credential log checks, and the Helm smoke test
