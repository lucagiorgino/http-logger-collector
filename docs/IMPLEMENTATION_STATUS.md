# Implementation status

## Current status
- Root-level `frontend/` implements a Next.js SSR management dashboard using
  JavaScript/JSX, Tailwind and shadcn/ui. Auth0 sessions/API tokens stay server-side.
  Screens cover sample overview metrics, filtered/paginated traffic, request
  details, exact-device policy management and acknowledgement, and verified owner.
  It preserves current owner boundaries; cross-owner fleet management and inventory
  require new backend contracts and are not implied by management permission.
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
- An opt-in HTTP-only whole-stack test exchanges two dedicated OAuth client
  credentials on device threads, uploads encrypted/plain batches, and verifies
  persisted analysis, redaction, policy enforcement and cross-owner isolation.
  It uses stable configured device IDs, reads pre-existing policies and reports
  agent status without changing policy or requesting management permission.
  Harness unit tests cover concurrent identities, advertised JWE suites, safe
  configuration and explicit OAuth/storage/visibility failures. A live Auth0 run
  requires externally provisioned test clients and is not part of offline validation.

## Architecture and operation diagrams

These Mermaid diagrams describe the **current implementation**, not a proposed
fleet-management or queue deployment. Keep this section synchronized when changing
routes, authentication, publisher boundaries, deployment profiles, or KMS commands.
Ports shown are defaults; environment variables and Helm values remain authoritative.

### Development architecture

Compose runs only MongoDB and OpenBao. Python and the optional management frontend
are separate host processes with live development reload. A development agent may
use the loopback HTTP API; remote traffic requires HTTPS.

```mermaid
flowchart LR
    Browser["Operator browser"]
    Agent["Device agents"]
    IdP["Auth0 tenant<br/>Authorization, token, discovery and JWKS"]

    subgraph Host["Development host"]
        FrontEnv["frontend/.env.local<br/>Web application credentials and session secret"]
        Env[".env<br/>Parameterized backend and Compose settings"]
        Front["Next.js SSR frontend<br/>127.0.0.1:3000<br/>Server-side Auth0 session and API adapter"]
        API["FastAPI / Uvicorn<br/>127.0.0.1:8001<br/>Auth0FastAPI, validation and owner checks"]
        Keys["EC key set cached in API RAM<br/>Local JWE decryption"]
        Publisher["IngestionPublisher interface"]
        Direct["MongoIngestionPublisher<br/>Direct-write MVP adapter"]
        CLI["app.kms administrative CLI"]
        Credentials[".secrets/<br/>Separate root and runtime credential files"]

        subgraph Compose["Docker Compose"]
            Mongo[("MongoDB single-member replica set<br/>Loopback 28017<br/>Time-series logs and device policies")]
            Volume[("Persistent MongoDB volume")]
            Bao["OpenBao dev instance<br/>Loopback 28200<br/>Ephemeral, auto-unsealed"]
            Transit["Transit engine<br/>Non-exportable AES KEK"]
            KV[("KV v2<br/>Wrapped EC key set only")]
        end
    end

    Browser -->|"Session cookie and HTML/forms"| Front
    Browser -->|"Interactive authorization"| IdP
    Front -->|"Server-side code and token exchange"| IdP
    Agent -->|"Client credentials or external user login"| IdP
    Front -->|"Bearer token, server to server"| API
    Agent -->|"Bearer token and gzip JSON or JWE"| API
    API -->|"Discovery and JWKS, SDK-cached"| IdP
    API -->|"Unwrap once at process startup"| Bao
    Bao --> Transit
    Bao --> KV
    Bao -->|"Unwrapped EC keys"| Keys
    Keys --> API
    API --> Publisher --> Direct
    Direct -->|"Majority inserts to primary"| Mongo
    API -->|"Policy reads/writes on primary"| Mongo
    API -->|"Analysis on primary: explicit development-only exception"| Mongo
    Mongo --> Volume
    FrontEnv -.-> Front
    Env -.-> API
    Env -.-> Compose
    Credentials -.->|"Runtime token only"| API
    Credentials -.->|"Administrative credential"| CLI
    Credentials -.->|"Development root token"| Bao
    CLI -->|"Provision, wrap, rotate and configure ACL"| Bao
```

There is no broker or consumer process in the MVP. The replaceable publisher
receives normalized/redacted documents **after** decryption, validation and policy
checks. Development OpenBao restart loses the KEK and EC key set; it does not
erase already normalized MongoDB logs.

### Deployment architecture

Solid dependency connections below describe the default external-dependency
profile. Dotted connections labeled as alternatives describe the bundled Helm
development profile; the API does not use both database providers simultaneously.
The frontend and TLS gateway must be deployed separately: the chart currently
packages the Python API and optional development dependencies, not the frontend,
ingress, certificates, or production dependency operations.

```mermaid
flowchart TB
    Browser["Operator browser"]
    Agent["Device agents"]
    IdP["Auth0 tenant"]
    Front["Separately hosted Next.js SSR frontend<br/>Private Web application credentials"]
    Gateway["Externally managed HTTPS gateway<br/>Trusted forwarded scheme"]
    Kube["Kubernetes control plane<br/>TokenReview"]

    subgraph Cluster["Kubernetes namespace / Helm release"]
        Service["API ClusterIP Service<br/>80 to API pod port 8000"]
        API["FastAPI Deployment replicas<br/>Init: wait for primary and KMS<br/>Per-process EC keys in RAM<br/>Publisher interface and direct MongoDB adapter"]
        Config["ConfigMap<br/>Tenant, audience, suite and dependency settings"]
        Secrets["Existing Secrets<br/>External MongoDB URI, optional KMS token and CA"]
        SA["Dedicated API ServiceAccount<br/>Projected audience-bound JWT<br/>No general-purpose automatic token mount"]

        subgraph Bundled["Alternative: bundled Helm development dependencies"]
            Init["Revision-named MongoDB initializer Job"]
            Headless["MongoDB headless Service"]
            DevPrimary[("MongoDB primary<br/>Pod port 27017")]
            DevSecondary[("Two MongoDB secondaries<br/>Pod port 27017")]
            PVC[("MongoDB PVCs")]
            DevBao["OpenBao dev Deployment and ClusterIP<br/>8200, ephemeral<br/>Transit KEK and wrapped KV key set"]
            Bootstrap["Revision-named OpenBao bootstrap Job"]
            Root["Development root Secret<br/>Only OpenBao and bootstrap Job"]
            Policy["Dependency ingress NetworkPolicies<br/>Require an enforcing CNI"]
        end
    end

    subgraph External["Externally operated production dependencies"]
        Primary[("MongoDB primary<br/>TLS and authentication")]
        Secondary[("MongoDB read replicas<br/>TLS and authentication")]
        Bao["Persistent HTTPS OpenBao<br/>Transit KEK and KV v2 wrapped keys"]
    end

    Browser -->|"HTTPS, session cookie"| Front
    Browser -->|"Interactive authorization"| IdP
    Front -->|"Code/token exchange and optional refresh"| IdP
    Agent -->|"Access-token acquisition"| IdP
    Front -->|"Server-side bearer requests"| Gateway
    Agent -->|"HTTPS API requests"| Gateway
    Gateway --> Service --> API
    API -->|"Discovery and JWKS"| IdP
    API -->|"Majority log writes; primary policy reads/writes"| Primary
    API -->|"Strict secondary analysis; no primary fallback"| Secondary
    Primary -->|"Replication"| Secondary
    API -->|"Login, wrapped-key read and Transit unwrap at startup"| Bao
    Bao -->|"Kubernetes auth: configured TokenReview access"| Kube
    Config -.-> API
    Secrets -.-> API
    SA -.-> API
    API -.->|"Alternative primary provider"| DevPrimary
    API -.->|"Alternative strict secondary provider"| DevSecondary
    DevPrimary -->|"Replication"| DevSecondary
    Init --> Headless
    Headless --> DevPrimary
    Headless --> DevSecondary
    DevPrimary --> PVC
    DevSecondary --> PVC
    API -.->|"Alternative KMS provider; Kubernetes login"| DevBao
    DevBao -->|"TokenReview using its own reviewer identity"| Kube
    Root -.-> DevBao
    Root -.-> Bootstrap
    Bootstrap -->|"Initialize keys, runtime ACL and Kubernetes auth role"| DevBao
    Policy -.-> DevPrimary
    Policy -.-> DevSecondary
    Policy -.-> DevBao
```

External OpenBao may use Kubernetes auth with operator-managed TokenReview access
or a mounted least-privilege runtime token. Bundled OpenBao uses its own
`system:auth-delegator` binding; API pods do not receive reviewer/admin privileges.
Default `replicaCount` is one, although the architecture supports multiple API
replicas. Every replica has its own startup-loaded EC key cache.

### Operation inventory

The sequences below share the authentication guard rather than repeating its
discovery/JWKS exchange on every API request. All resource paths are owner-scoped.
`manage:devices` permits policy changes within the verified owner, not impersonation
or cross-owner administration.

| Operation | Sequence |
| --- | --- |
| Interactive dashboard login, refresh and logout | [Dashboard authentication](#dashboard-authentication) |
| Machine-to-machine agent token acquisition | [Agent authentication](#agent-authentication) |
| HTTPS and bearer verification for `/api/v1/*` | [Shared API authentication guard](#shared-api-authentication-guard) |
| `GET /api/v1/me` | [Verified identity](#verified-identity) |
| SSR overview, traffic, request details and workspace | [Server-rendered dashboard reads](#server-rendered-dashboard-reads) |
| `GET /api/v1/devices/{id}/configuration` | [Device policy lookup](#device-policy-lookup) |
| `PATCH /api/v1/devices/{id}/configuration`, including UI form save | [Device policy change](#device-policy-change) |
| `PUT /api/v1/devices/{id}/configuration-status` | [Agent acknowledgement](#agent-acknowledgement) |
| `POST /api/v1/traffic-log-batches`, plaintext or JWE | [Batch ingestion](#batch-ingestion) |
| `GET /api/v1/traffic-logs` and `GET /api/v1/traffic-logs/{id}` | [Analysis reads](#analysis-reads) |
| API startup and graceful shutdown | [API process lifecycle](#api-process-lifecycle) |
| `GET /health`, `/openapi.json`, `/docs`, `/redoc` | [Health and API documentation](#health-and-api-documentation) |
| `prepare-dev`, `bootstrap-dev`, `initialize`, `bootstrap-kubernetes` | [KMS initialization and credentials](#kms-initialization-and-credentials) |
| `stage`, `activate`, `retire`, `rotate` | [EC key lifecycle](#ec-key-lifecycle) |
| `rotate-kek` | [Transit KEK rotation](#transit-kek-rotation) |
| `make dev`, `make dev-down` | [Development stack lifecycle](#development-stack-lifecycle) |
| Helm install, upgrade, smoke test and uninstall | [Deployment lifecycle](#deployment-lifecycle) |
| `make test-stack` and frontend browser tests | [Integration validation](#integration-validation) |

Schema validation and persistence-unavailable errors apply across resource
operations, even where their shared branches are omitted. There are no local
login/token, fleet-inventory, cross-owner management, or log/device deletion APIs.

#### Dashboard authentication

```mermaid
sequenceDiagram
    actor User as Operator
    participant Browser
    participant UI as Next.js and Auth0 SDK
    participant IdP as Auth0
    User->>Browser: Open management console
    Browser->>UI: GET /
    alt Frontend Auth0 configuration incomplete
        UI-->>Browser: SSR setup guidance#59; sign-in disabled
    else Configuration complete#59; no session
        UI-->>Browser: SSR welcome with /auth/login link
        Browser->>UI: GET /auth/login
        UI-->>Browser: Redirect to Auth0 authorization
        Browser->>IdP: Authenticate and authorize Web application
        IdP-->>Browser: Redirect with authorization code
        Browser->>UI: GET /auth/callback with code and state
        UI->>IdP: Server-side authorization-code exchange
        IdP-->>UI: Validated login result and API token set
        UI-->>Browser: Encrypted HTTP-only session cookie#59; redirect /overview
    else Session already present
        UI-->>Browser: Redirect /overview
    end
    opt Later authenticated request with expired API token
        Browser->>UI: Request with session cookie
        alt Refresh is possible for this session
            UI->>IdP: Refresh token exchange
            IdP-->>UI: New API token set
            UI-->>Browser: Persist updated session cookie in proxy response
        else Refresh unavailable or access-token error
            UI-->>Browser: Redirect /auth/login
        end
    end
    opt Operator signs out
        Browser->>UI: GET /auth/logout
        UI-->>Browser: Clear local session#59; redirect through Auth0 logout
        Browser->>IdP: End provider session
        IdP-->>Browser: Return to configured application URL
    end
    Note over Browser,UI: Browser API-token endpoint is disabled#59; tokens are not client component props
```

Refresh requires a refresh token/tenant configuration that permits it; otherwise
the SDK requires fresh authentication. Live interactive Auth0 login is not implied
by mocked-session browser tests.

#### Agent authentication

```mermaid
sequenceDiagram
    participant Agent
    participant IdP as Auth0
    participant API as FastAPI
    Agent->>IdP: POST /oauth/token with client_credentials, client ID/secret and API audience
    alt Client and API grant authorized
        IdP-->>Agent: Bearer access token for this M2M subject
        Agent->>API: Authenticated configuration or upload request
        Note over Agent,API: API applies the shared authentication guard
    else Invalid client, audience, grant or requested scope
        IdP-->>Agent: OAuth error#59; no backend request for this operation
    end
    Note over Agent,IdP: Normal encrypted uploads do not require manage:devices
    Note over Agent,IdP: User-device agents may instead use an external Authorization Code with PKCE flow
```

#### Shared API authentication guard

```mermaid
sequenceDiagram
    participant Caller as Agent or frontend server
    participant API as FastAPI
    participant SDK as Auth0FastAPI
    participant IdP as Auth0
    Caller->>API: Request to /api/v1 resource
    alt HTTPS required but effective scheme is HTTP
        API-->>Caller: 400 HTTPS required
    else HTTPS accepted or explicit local-development exception
        alt Missing or malformed bearer JWT
            API-->>Caller: 401 with WWW-Authenticate Bearer
        else JWT-shaped bearer supplied
            API->>SDK: require_auth dependency with request context
            opt Discovery or JWKS not available in SDK cache
                SDK->>IdP: Fetch configured tenant discovery and JWKS
                IdP-->>SDK: Metadata and public signing keys
            end
            SDK->>SDK: Verify signature, issuer, audience and SDK-required claims
            alt SDK validation or authorization rejection
                SDK-->>API: HTTP authentication error
                API-->>Caller: Sanitized SDK status, usually 401 or 403
            else Unexpected SDK failure
                SDK-->>API: Internal authentication failure
                API-->>Caller: Sanitized 503
            else Verified bearer accepted
                SDK-->>API: Verified claims
                alt Verified bearer contains proof-binding claim
                    API-->>Caller: 401, proof-bound tokens unsupported by this API
                else Token is not proof-bound
                    API->>API: Execute resource-specific owner and permission checks
                end
            end
        end
    end
```

The SDK can classify discovery/JWKS fetch failures as token-verification failures;
not every upstream outage is guaranteed to become a 503. `/health` and API
documentation are outside this bearer guard.

#### Verified identity

```mermaid
sequenceDiagram
    participant Caller as Agent or frontend server
    participant API as FastAPI
    Caller->>API: GET /api/v1/me with verified bearer
    API->>API: Require nonempty string sub
    alt Subject available
        API-->>Caller: 200 with user_id equal to verified sub
    else Subject missing or invalid
        API-->>Caller: 401 with bearer challenge
    end
    Note over Caller,API: No local login, token-issuance or owner-selection endpoint
```

#### Server-rendered dashboard reads

```mermaid
sequenceDiagram
    participant Browser
    participant UI as Next.js server
    participant Session as Auth0 server session
    participant API as FastAPI
    Browser->>UI: GET /overview, /logs, /logs/id, /devices or /settings
    UI->>Session: Require session#59; obtain API token server-side
    alt Session absent
        UI-->>Browser: Redirect to sign-in
    else Session valid
        alt Overview or traffic collection
            UI->>API: GET /api/v1/traffic-logs with bounded filters/cursor
        else Request details
            UI->>API: GET /api/v1/traffic-logs/id
        else Device lookup
            UI->>API: GET /api/v1/devices/id/configuration
        else Workspace identity
            UI->>API: GET /api/v1/me
        end
        Note over UI,API: Server-only bearer forwarding#59; no-store#59; no redirect following
        alt Backend operation succeeds
            API-->>UI: Owner-scoped JSON
            UI-->>Browser: Private, server-rendered HTML
        else Backend denies or cannot serve request
            API-->>UI: 401, 403, 404, 409, 422 or 503
            UI-->>Browser: Explicit sanitized error, not fabricated empty data
        end
    end
    Note over Browser,UI: Overview metrics describe the latest-50-record sample, not fleet totals
```

#### Device policy lookup

```mermaid
sequenceDiagram
    participant Caller as Agent or frontend server
    participant API as FastAPI
    participant Primary as MongoDB primary
    participant Keys as API EC key cache
    Caller->>API: GET /api/v1/devices/id/configuration
    API->>Primary: Find configuration by verified sub and device ID#59; majority read
    alt No saved configuration
        Primary-->>API: No document
        API->>API: Default version 0, tls_only
    else Configuration exists
        Primary-->>API: Version, mode and last reported status
    end
    alt Mode optional or required
        API->>Keys: Read configured suite and active public key
        alt Active key available
            Keys-->>API: Public-only JWK and suite
            API-->>Caller: 200 configuration, key, status and poll interval
        else Application key unavailable
            API-->>Caller: 503 application encryption unavailable
        end
    else Mode tls_only
        API-->>Caller: 200 configuration with encryption null
    end
    Note over Caller,Primary: No management permission required#59; response is no-store
    Note over Caller,API: Default policy does not prove enrollment or online status
```

#### Device policy change

```mermaid
sequenceDiagram
    actor Operator
    participant Browser
    participant UI as Next.js Server Action
    participant API as FastAPI
    participant Primary as MongoDB primary
    Operator->>Browser: Choose tls_only, optional or required
    Browser->>UI: Submit device ID, expected_version and mode
    UI->>UI: Require session#59; validate form and safe version
    UI->>API: PATCH /api/v1/devices/id/configuration with bearer
    Note over UI,API: Direct authorized API callers can issue the same PATCH
    API->>API: Require manage:devices in verified scope or permissions
    alt Missing management permission
        API-->>UI: 403
    else Enabling JWE without available keys
        API-->>UI: 503
    else Authorized and representable policy
        API->>Primary: Owner-scoped compare-and-swap#59; increment version#59; majority write
        alt Expected version current
            Primary-->>API: Updated configuration
            API-->>UI: 200 new configuration/version
        else Stale version or concurrent create
            Primary-->>API: No matching update or duplicate owner/device ID
            API-->>UI: 409
        end
    end
    UI-->>Browser: Redirect to device view with saved or explicit error state
    Browser->>UI: GET current device view
    UI->>API: Reload current policy and acknowledgement
    API-->>UI: Current configuration
    UI-->>Browser: SSR policy#59; distinguish saved version from agent-applied version
    Note over Operator,Primary: Permission remains owner-scoped#59; human and M2M subjects are not interchangeable
```

Invalid JSON/schema values return 422 before a write. Policy changes do not
retroactively cancel uploads whose acceptance checks have already completed.

#### Agent acknowledgement

```mermaid
sequenceDiagram
    participant Agent
    participant API as FastAPI
    participant Primary as MongoDB primary
    Agent->>API: GET current configuration on startup or poll interval
    API-->>Agent: Mode, version, suite and public key when enabled
    Agent->>Agent: Apply advertised configuration or detect unsupported/error state
    Agent->>API: PUT /api/v1/devices/id/configuration-status
    Note over Agent,API: Report version, applied/unsupported/error, capability and optional key/detail
    API->>Primary: Read current owner/device policy on primary
    alt Report version is stale
        API-->>Agent: 409#59; refresh configuration
    else Applied JWE report has wrong key or lacks encryption capability
        API-->>Agent: 422
    else Report compatible
        API->>Primary: Conditional update for same version#59; majority write
        alt Version changed during update
            API-->>Agent: 409
        else Report saved
            Primary-->>API: Update acknowledged
            API-->>Agent: 200 report with server reported_at
        end
    end
    Note over Agent,Primary: No manage:devices grant required#59; only this owner's device is affected
```

Agents must refresh public key/suite data even when the policy version is
unchanged: EC rotation and device-policy versioning are independent.

#### Batch ingestion

```mermaid
sequenceDiagram
    participant Agent
    participant API as FastAPI
    participant Keys as API EC key cache
    participant Primary as MongoDB primary
    participant Publisher as IngestionPublisher
    participant Adapter as MongoIngestionPublisher
    Agent->>Agent: Build JSON batch of 1 to 500 logs#59; gzip exactly once
    alt Device sends application encryption
        Agent->>Agent: Use advertised EC public key and configured ECDH/AES suite
        Agent->>API: POST /api/v1/traffic-log-batches#59; application/jose
        API->>API: Bound encrypted wire to 1500000 bytes#59; reject outer encoding
        API->>Keys: Validate protected envelope/key/suite and decrypt JWE locally
        Keys-->>API: Gzip bytes or invalid-envelope failure
    else Device sends TLS-only payload
        Agent->>API: POST same resource#59; application/json and Content-Encoding gzip
    end
    API->>API: Bound gzip to 1 MiB and expanded JSON to 8 MiB
    API->>API: Validate exactly one gzip member, full batch schema and all owner IDs
    loop Every distinct device in batch
        API->>Primary: Majority read of current owner/device policy
        Primary-->>API: Effective encryption mode
        API->>API: Reject plaintext for required or JWE for tls_only
    end
    alt Envelope, format, limit, schema, ownership or policy check fails
        API-->>Agent: Explicit 400, 403, 413, 415, 422 or dependency 503#59; do not publish
    else Whole batch accepted
        API->>API: Normalize timestamps/methods#59; redact headers#59; hash/minimize bodies
        API->>Publisher: publish batch ID and normalized documents
        Publisher->>Adapter: Direct MVP adapter
        Adapter->>Primary: Unordered bulk insert into time-series logs#59; majority acknowledgement
        alt Write acknowledged
            Primary-->>Adapter: Insert acknowledged
            Adapter-->>API: Receipt status stored and accepted_count
            API-->>Agent: 202 receipt
        else Persistence failure
            Adapter-->>API: PersistenceError
            API-->>Agent: Sanitized 503#59; no success receipt
        end
    end
    Note over Agent,Primary: Bulk failure may partially persist#59; retries are not deduplicated
    Note over Keys,Publisher: OpenBao is not called for each batch#59; runtime EC keys are cached in RAM
```

The flow groups failure outcomes for readability; an actual failed guard
short-circuits processing immediately. A future publisher may return `queued`
after durable queue acceptance, but no broker/consumer or exactly-once processing
is implemented.

#### Analysis reads

```mermaid
sequenceDiagram
    participant Caller as Agent or frontend server
    participant API as FastAPI
    participant Replica as Configured analysis node
    Caller->>API: GET /api/v1/traffic-logs with optional device/time/cursor/limit
    API->>API: Validate filters and cursor#59; add verified-owner predicate
    API->>Replica: Majority read sorted by event timestamp and record ID#59; limit plus one
    Note over API,Replica: Deployment: strict secondary#59; local single-node development: explicit primary exception
    alt Matching records available
        Replica-->>API: Owner-filtered documents
        API-->>Caller: 200 items and optional next_cursor
        opt More records requested
            Caller->>API: GET collection with next_cursor and same filters
            API->>Replica: Fetch older owner-scoped page
            API-->>Caller: 200 next page
        end
    else No match or replication not yet visible
        Replica-->>API: Empty result
        API-->>Caller: 200 empty items
    else Required analysis node unavailable
        Replica-->>API: Driver failure after configured timeout
        API-->>Caller: Sanitized 503#59; no primary fallback
    end
    opt Fetch one record
        Caller->>API: GET /api/v1/traffic-logs/id
        API->>Replica: Find by ObjectId and verified owner
        alt Record visible for owner
            API-->>Caller: 200 normalized record
        else Missing, not yet replicated or belongs to another owner
            API-->>Caller: 404 without disclosing another owner
        end
    end

```

Invalid collection filters/cursors or record ID formats return 422 before the
corresponding database query. A just-accepted receipt does not guarantee immediate
secondary visibility.

#### API process lifecycle

```mermaid
sequenceDiagram
    participant Process as Host process or API pod
    participant Settings as Environment configuration
    participant Bao as OpenBao
    participant Kube as Kubernetes API
    participant API as FastAPI lifespan
    participant Primary as MongoDB primary
    Process->>Settings: Load selected ENV_FILE or deployed environment#59; validate settings
    Process->>API: Start process and lifespan
    alt Kubernetes-auth KMS configuration
        API->>Bao: Login with projected JWT, role and bound audience
        Bao->>Kube: TokenReview using configured reviewer access
        Kube-->>Bao: JWT validation result
        Bao-->>API: Least-privilege runtime token
    else Mounted runtime token configuration
        API->>API: Read private runtime credential file
    end
    API->>Bao: Read wrapped key-set ciphertext from exact KV v2 record
    API->>Bao: Transit decrypt using runtime-only ACL
    Bao-->>API: EC key-set plaintext
    API->>API: Validate suite and key set#59; cache keys in RAM
    API->>Primary: Validate replica-set URI#59; create/verify time-series collection and indexes
    alt KMS, configuration or storage initialization fails
        API-->>Process: Startup fails#59; do not serve as ready
    else Initialization succeeds
        API-->>Process: Ready to serve
        Note over API,Bao: Running process keeps keys during later KMS outages#59; revocation does not erase RAM
        Process->>API: Graceful shutdown signal
        API->>Primary: Await async MongoDB client close
        API-->>Process: Lifespan exits
    end

```

Kubernetes init containers additionally wait for primary reachability and
successful KMS key loading before launching the API container. The API process
still performs its own key load and storage initialization.

#### Health and API documentation

```mermaid
sequenceDiagram
    participant Caller as Browser, probe or Helm smoke test
    participant API as FastAPI
    alt Liveness or readiness request
        Caller->>API: GET /health
        API-->>Caller: 200 status ok
        Note over Caller,API: Process liveness only#59; no MongoDB, replica, Auth0 or KMS health query
    else API schema
        Caller->>API: GET /openapi.json
        API-->>Caller: OpenAPI resource contracts and bearer security metadata
    else Interactive documentation
        Caller->>API: GET /docs or /redoc
        API-->>Caller: Documentation HTML
        Caller->>API: GET /openapi.json
        API-->>Caller: API schema
    end
    Note over Caller,API: These paths do not require bearer authentication
```

#### KMS initialization and credentials

```mermaid
sequenceDiagram
    actor Operator
    participant CLI as app.kms
    participant Files as Private credential files or mounted Secrets
    participant Bao as OpenBao admin API
    participant Transit as Transit KEK
    participant KV as KV v2
    opt prepare-dev
        Operator->>CLI: Prepare development credential
        CLI->>Files: Create mode-0600 root-token file only if absent
        Note over CLI,Files: Root token is consumed by the dev OpenBao instance#59; never printed
    end
    Operator->>CLI: initialize, bootstrap-dev or bootstrap-kubernetes
    CLI->>Files: Read administrative credential
    CLI->>Bao: Ensure compatible Transit and KV v2 mounts
    CLI->>Transit: Ensure non-exportable AES-256-GCM KEK#59; plaintext backup disabled
    CLI->>KV: Read current wrapped record
    alt Key set already exists
        KV-->>CLI: Ciphertext and KV version
        CLI->>Transit: Unwrap existing document
        CLI->>CLI: Validate existing keys#59; preserve them
    else First initialization
        CLI->>CLI: Generate configured EC key in RAM and select active ID
        CLI->>Transit: Encrypt serialized key set under KEK
        Transit-->>CLI: Wrapped ciphertext
        CLI->>KV: Write ciphertext with CAS version 0
    end
    CLI->>Bao: Install runtime ACL for exact KV read and Transit decrypt only
    alt bootstrap-dev
        CLI->>Bao: Create nonrenewable 24-hour runtime token
        Bao-->>CLI: Runtime token
        CLI->>Files: Atomically replace private runtime-token file
    else bootstrap-kubernetes
        CLI->>Bao: Configure Kubernetes auth mount, reviewer host and bound role
        Note over CLI,Bao: Bind API ServiceAccount, namespace, audience and runtime policy
    else initialize
        Note over CLI,Bao: Runtime credential/auth provisioning remains an operator responsibility
    end
    CLI-->>Operator: Completion status without keys or credentials
```

Incompatible mounts/KEKs, invalid keys or CAS failures stop the operation
explicitly. Production initialization/unseal, recovery, backups and audits remain
external operational responsibilities.

#### EC key lifecycle

```mermaid
sequenceDiagram
    actor Operator
    participant CLI as app.kms with admin credential
    participant KV as OpenBao KV v2
    participant Transit as Transit KEK
    participant API as Every API process
    participant Agent
    Operator->>CLI: stage, activate, retire or rotate with key ID
    CLI->>KV: Read wrapped key set and current KV version
    KV-->>CLI: Ciphertext and version
    CLI->>Transit: Decrypt current key set
    Transit-->>CLI: Key document
    alt stage
        CLI->>CLI: Generate and add key#59; leave active ID unchanged
    else activate
        CLI->>CLI: Select an existing retained key as active
    else retire
        CLI->>CLI: Remove retained key#59; reject removal of active key
    else rotate
        CLI->>CLI: Generate/add key and make active in one update
    end
    CLI->>Transit: Wrap updated complete key set
    Transit-->>CLI: New ciphertext
    CLI->>KV: Store ciphertext with expected KV version
    alt CAS conflict or invalid command
        CLI-->>Operator: Explicit failure#59; inspect state before retry
    else Update committed
        CLI-->>Operator: Completed#59; runtime caches still unchanged
        Operator->>API: Restart or roll out every process
        API->>KV: Read updated ciphertext
        API->>Transit: Unwrap and validate updated keys
        Agent->>API: Poll device configuration
        API-->>Agent: Active public key and configured suite
        Agent->>API: Send JWE using advertised active key
    end

```

For multiple replicas, run **stage -> roll out all processes -> activate -> roll
out all processes**. This prevents a process advertising a key another process
cannot decrypt. One-step `rotate` is intended for a stopped/single-backend workflow.
Retain old EC keys for buffered uploads, then retire and restart all processes.
Retirement does not erase historical KV versions, backups, or old process memory.

#### Transit KEK rotation

```mermaid
sequenceDiagram
    actor Operator
    participant CLI as app.kms rotate-kek
    participant Transit as OpenBao Transit
    participant KV as OpenBao KV v2
    Operator->>CLI: Rotate wrapping KEK using administrative credential
    CLI->>Transit: Rotate KEK version
    Transit-->>CLI: New KEK version ready
    CLI->>KV: Read existing ciphertext and KV version
    KV-->>CLI: Wrapped EC key-set record
    CLI->>Transit: Rewrap ciphertext under latest KEK version
    Transit-->>CLI: New ciphertext with unchanged EC key material
    CLI->>KV: CAS write rewrapped ciphertext
    alt KV version unchanged
        CLI-->>Operator: Success#59; no API restart or agent reconfiguration required
    else KV CAS conflict
        CLI-->>Operator: Explicit failure#59; inspect state before retrying
    end
    Note over Transit,KV: Keep old KEK versions for retained KV history and backups
```

#### Development stack lifecycle

```mermaid
sequenceDiagram
    actor Developer
    participant Make
    participant Mongo as Compose MongoDB
    participant Bao as Compose OpenBao
    participant CLI as app.kms
    participant API as Host Python
    participant UI as Host Next.js
    Developer->>Make: make dev
    Make->>Mongo: Start and wait for health
    Mongo->>Mongo: Initiate one-member replica set only if uninitialized
    Make->>CLI: prepare-dev
    Make->>Bao: Start and wait for health
    Make->>CLI: bootstrap-dev
    CLI->>Bao: Provision/preserve key set and refresh runtime credential
    Make->>API: python -m app using selected ENV_FILE
    Note over API: Run startup sequence#59; reload backend source without image rebuild
    opt Management frontend
        Developer->>Make: make frontend-dev
        Make->>UI: Start Next.js using separate frontend/.env.local and port 3000
    end
    Developer->>API: Ctrl-C
    API->>API: Graceful lifespan shutdown and awaited MongoDB close
    Developer->>UI: Ctrl-C if frontend was started
    Developer->>Make: make dev-down
    Make->>Mongo: Remove Compose container#59; retain persistent volume
    Make->>Bao: Remove container#59; ephemeral KMS state is lost
    Note over CLI,API: After KMS restart, bootstrap again and restart API#59; old buffered JWE may be unrecoverable
```

`make run` starts only Python. `mongo-up/down` and `openbao-up/down` target the
individual dependencies. `make frontend-build` and `make frontend-start` build
and run the production SSR bundle independently of the Python image.

#### Deployment lifecycle

```mermaid
sequenceDiagram
    actor Operator
    participant Helm
    participant Kube as Kubernetes
    participant Jobs as Optional dependency initializer Jobs
    participant Dependencies as MongoDB and OpenBao
    participant API as API pod init containers and process
    Operator->>Helm: make helm-up with selected context, values and immutable image
    Helm->>Kube: Create or upgrade Service, Deployment, ConfigMap, ServiceAccount and mounts
    alt Bundled development dependencies enabled
        Helm->>Kube: MongoDB StatefulSet/PVCs, OpenBao dev Deployment, RBAC and NetworkPolicies
        Helm->>Jobs: Revision-named MongoDB and OpenBao bootstrap Jobs
        Jobs->>Dependencies: Initialize only absent state#59; preserve existing membership/keys
    else External dependency profile
        Note over Kube,Dependencies: Operator already provisioned authenticated MongoDB and persistent HTTPS OpenBao
    end
    Kube->>API: Start wait-for-primary init container
    API->>Dependencies: Wait for primary reachability
    Kube->>API: Start wait-for-openbao init container
    API->>Dependencies: Retry runtime authentication and key-set loading
    Kube->>API: Start application container
    API->>Dependencies: Perform API startup/key-load/storage initialization
    Kube->>API: Startup/readiness/liveness probes on /health
    Helm-->>Operator: Install or upgrade completes after workloads and Jobs are ready
    opt Deployment smoke test
        Operator->>Helm: make helm-test
        Helm->>Kube: Run smoke-test pod
        Kube->>API: Check Service /health and current OpenAPI routes
        Helm-->>Operator: Smoke-test result
    end
    opt Local access
        Operator->>Kube: make helm-port-forward
        Kube-->>Operator: Loopback 18000 to API Service
    end
    opt Uninstall
        Operator->>Helm: make helm-down
        Helm->>Kube: Remove release workloads, Services, Jobs and bundled dev root Secret
        Note over Kube,Dependencies: MongoDB PVCs retained#59; bundled OpenBao state lost#59; external services untouched
    end

```

ConfigMap checksum changes trigger a Deployment rollout; EC key changes require
an explicit rollout. A gateway, frontend deployment, certificates, external
dependency provisioning and production secret rotation are not installed by
this Helm chart.

#### Integration validation

```mermaid
sequenceDiagram
    actor Tester
    participant Test as make test-stack
    participant Plain as Plaintext device thread
    participant Encrypted as Encrypted device thread
    participant IdP as Real Auth0 tenant
    participant API as Running backend stack
    Tester->>Test: Load private profile with two clients and stable preconfigured device IDs
    par Plain device
        Test->>Plain: Release synchronized start
        Plain->>IdP: Client-credentials token request
        IdP-->>Plain: Access token
        Plain->>API: Read /me and existing plaintext-compatible policy#59; acknowledge
        Plain->>API: Upload three synthetic logs as gzip JSON
        API-->>Plain: 202 stored or durable queued receipt
    and Encrypted device
        Test->>Encrypted: Release synchronized start
        Encrypted->>IdP: Client-credentials token request without management scope
        IdP-->>Encrypted: Agent access token
        Encrypted->>API: Read /me and existing JWE-compatible policy
        Encrypted->>API: Acknowledge advertised suite/key
        opt Existing policy is required
            Encrypted->>API: Attempt plaintext gzip
            API-->>Encrypted: 403 required-encryption enforcement
        end
        Encrypted->>API: Upload three synthetic logs as compact JWE
        API-->>Encrypted: 202 receipt
    end
    loop Bounded visibility wait for each device
        Plain->>API: Read own batch through paginated analysis API
        Encrypted->>API: Read own batch through paginated analysis API
    end
    Test->>Test: Verify counts, IDs, redaction, body minimization and distinct owners
    Test->>API: Query each other owner's device and record IDs
    API-->>Test: Empty collections and 404 records
    Test-->>Tester: Pass or explicit failure#59; synthetic records remain
    Note over Test,Encrypted: No policy writes or management scope#59; incompatible setup fails explicitly
```

```mermaid
sequenceDiagram
    actor Developer
    participant Test as make frontend-test-browser
    participant Browser as Chromium
    participant UI as Production Next.js test server
    participant Mock as Loopback mock API
    Developer->>Test: Build SSR bundle and run Playwright
    Test->>Mock: Start loopback 38002 backend fixture
    Test->>UI: Start loopback 3301 with test-only configuration
    Test->>Browser: Supply session via public Auth0 generateSessionCookie test helper
    Browser->>UI: Render pages, including JavaScript-disabled requests
    UI->>Mock: Server-side bearer reads and policy PATCH requests
    Mock-->>UI: Fixture data, permission denials and version conflicts
    UI-->>Browser: SSR HTML and form outcomes
    Test->>Test: Assert token non-disclosure, mobile layout, disabled token endpoint and concurrency protection
    Test->>UI: Stop temporary frontend server
    Test->>Mock: Stop temporary backend fixture
    Test-->>Developer: Browser results
    Note over Test,Mock: No real OAuth exchange, MongoDB or OpenBao in this browser suite
```

The backend's opt-in database/KMS integration tests separately exercise real
time-series storage, replica command routing, policy CAS, envelope decryption and
OpenBao administrative/runtime boundaries. See the test inventory above and
the runbook for their environment selectors.
