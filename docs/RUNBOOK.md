# Runbook

## Prerequisites
- Python 3.11+
- Docker and Docker Compose for development MongoDB, OpenBao, Redis and NGINX
- For Kubernetes deployment only: Kubernetes 1.27+, kubectl, Helm 3 or 4, and a
  StorageClass when using bundled MongoDB/Redis
- Redis >=7.2 with persistent AOF and `maxmemory-policy noeviction`

For HTTPS proxy configuration, device encryption policies, OpenBao provisioning, and
rotation, see [Log encryption and device configuration](ENCRYPTION.md).

## Local setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run the live development stack

```bash
make dev
```

Compose starts one MongoDB container as a one-member `rs0` replica set. Its
healthcheck initializes it and waits for primary readiness. The backend runs in
the host virtual environment through `python -m app` with Uvicorn reload.
Python code changes do not require a Docker build, Helm, or Kubernetes.

Compose also starts OpenBao and bootstraps its Transit KEK, wrapped EC key set,
and a least-privilege runtime credential. It also starts persistent Redis 7.4 on
loopback port 28379. `make dev` waits for all three dependencies and starts the
NGINX proxy, then starts Python. Backend and Compose share `.env`, preserving
Auth0 settings. The backend listens on `127.0.0.1:9090`; the separately started
frontend listens on `127.0.0.1:3000`. Open the public development origin
`http://127.0.0.1:8080`: NGINX routes API/docs/health paths to the backend and
all other paths, including `/auth/*`, to the frontend without stripping prefixes.
Configure frontend `APP_BASE_URL` and `BACKEND_API_URL` for that same origin in
`frontend/.env.local`; keep frontend Auth0 Web application credentials there.

Compose publishes proxy port 8080 by default and uses the Docker Desktop host
gateway. No override is needed on Docker Desktop. On native Linux, use the
host-network override to reach loopback-bound host applications:

```bash
make dev DEV_COMPOSE_OVERRIDE=docker/docker-compose.host.yml
# Use the same override for subsequent Compose targets, including shutdown:
make dev-down DEV_COMPOSE_OVERRIDE=docker/docker-compose.host.yml
```

The override publishes `127.0.0.1:8080` and reaches the host applications through
`host.docker.internal`; keep the host backend/frontend bound to loopback.

To run the components independently:

```bash
make dev-up
make run
# In a separate terminal:
make worker
# In another terminal, after frontend setup:
make frontend-dev
```

`make run` uses the MongoDB URI in `.env`, which interpolates `MONGO_HOST`,
`MONGO_PORT`, and `MONGO_REPLICA_SET`, normally resolving to
`mongodb://localhost:28017/?replicaSet=rs0`. MongoDB publishes only port 28017 on
loopback and stores data in one named volume. MongoDB requires no hosts-file edits,
shared network namespaces, or extra database services. OpenBao uses loopback port
28200; Python loads `OPENBAO_ADDR` and `OPENBAO_TOKEN_FILE` from the same `.env`.
The generated `.secrets/` credentials are private and ignored by Git and Docker builds.
The application never receives the development root credential.

The local profile sets `MONGO_ALLOW_PRIMARY_READS=true`, explicitly reading analysis
from its only member. This setting is allowed only with `APP_ENV=development`;
enabling it in another environment fails startup. Deployments continue to read
strictly from secondaries, without primary fallback.

Run `make worker` in a separate terminal even when using `make dev`. Both Python
processes load `.env`; the worker does not use Auth0/OpenBao. Restart it explicitly
after code/config changes. Stop API and worker with Ctrl+C. `make dev-down` stops
the proxy and all three dependencies and retains MongoDB/Redis volumes.
Individual targets are `mongo-up/down`, `openbao-up/down`, `redis-up/down`, and
`proxy-up/down`. Use the same
`COMPOSE_PROJECT` value for start/stop if overriding `amazing-http-logger-dev`.
These non-TLS services are for local development only.

OpenBao runs in **ephemeral dev mode**, not persistent production storage.
Restarting/recreating it loses the KEK and EC key set; buffered encrypted uploads
using the previous keys become undecryptable. Run `make openbao-up` to bootstrap
again, then restart Python. Already stored MongoDB logs remain readable.
The runtime token lasts 24 hours; rerun `make openbao-up` before starting Python
after expiry. Do not print tokens, commit `.secrets`, or use the root token as
the application's runtime credential.

## Environment profiles and ports

`ENV_FILE` selects the local configuration file for both Compose and Python:

```bash
make dev ENV_FILE=.env
```

Copy `.env.example` for another profile and keep it git-ignored. Explicit shell or
Make command-line variables override its values, including URI interpolation:

```bash
make dev MONGO_PORT=29017 APP_PORT=8101
```

| Listener | Default | Parameter |
| --- | --- | --- |
| Docker MongoDB, host and container | 28017 | `.env` `MONGO_PORT` |
| Docker OpenBao, host and container | 28200 | `.env` `OPENBAO_PORT` |
| Docker Redis, host (container 6379) | 28379 | `.env` `REDIS_PORT` |
| Public development NGINX proxy | 8080 | `.env` `PROXY_BIND_HOST` / `PROXY_PORT` |
| Host Python backend | 9090 | `.env` `APP_HOST` / `APP_PORT` |
| Host Next.js frontend | 3000 | Make `FRONTEND_PORT`; match Compose `.env` `FRONTEND_PORT` |
| Kubernetes API pod and Service | 9090 | Helm `app.port` / `service.port` |
| Kubernetes frontend pod and Service | 3000 | Helm `frontend.port` / `frontend.service.port` |
| Kubernetes MongoDB pods | 27017 | Helm `mongodb.port` |
| Kubernetes OpenBao pod | 8200 | Helm `openbao.port` |
| Kubernetes Redis pod (development) | 6379 | Helm `redis.port` |
| Kubernetes API port-forward | 9090 | Make `KUBE_LOCAL_PORT` |

Kubernetes uses ClusterIP Services, not host ports. Its API port-forward defaults
to local 9090, which conflicts with a running host backend; select, for example,
`make helm-port-forward KUBE_LOCAL_PORT=19090` when running both.
Change any occupied ports explicitly and keep proxy upstream ports synchronized.
Keep the same MongoDB port and replica-set name when reusing its development volume.
Changing `.env` requires a Python restart.

## Optional local Kubernetes deployment
```bash
minikube start
make image
make frontend-image
docker pull mongo:7
docker pull redis:7.4
docker pull ghcr.io/openbao/openbao:2.7.1
minikube image load amazing-http-logger:dev amazing-http-logger-dashboard:dev mongo:7 redis:7.4 ghcr.io/openbao/openbao:2.7.1
make helm-lint
# Provision logger-frontend-auth0 first, as described below.
make helm-up KUBE_CONTEXT=minikube
make helm-test KUBE_CONTEXT=minikube
kubectl --context=minikube -n logger get pods,jobs,pvc
make helm-port-forward KUBE_CONTEXT=minikube
```

Check `kubectl config current-context` before deployment. Make targets accept
`KUBE_CONTEXT`, `KUBE_NAMESPACE`, `HELM_RELEASE`, `HELM_VALUES`, `HELM_ARGS`,
`IMAGE_REPOSITORY`, `IMAGE_TAG`, `KUBE_SERVICE`, and `KUBE_LOCAL_PORT`. Their defaults select the `logger`
release/namespace and the **development** Helm profile; the chart's own default
values select **external** MongoDB, authenticated TLS Redis and HTTPS OpenBao.

The development profile has placeholder Auth0 settings. Set a real tenant and
audience with `HELM_ARGS='--set auth0.domain=your-tenant.auth0.com --set auth0.audience=https://your-api'`.
Do not put access tokens, database passwords, or client secrets in Helm values.
It also references a namespace-local `logger-frontend-auth0` Secret and an illustrative
`https://logger.example.test` public origin. Provision the Secret and override
`frontend.appBaseUrl` with a real HTTPS ingress/gateway origin reachable from both
browser and frontend pods before using the dashboard. The sample does not install
an Ingress controller or configure DNS/certificates. For API/worker-only deployments,
set `frontend.enabled=false`; frontend secrets/origin are then unnecessary.

The chart deploys a separate Next.js frontend Deployment/ClusterIP Service on port
3000 by default, with resource limits, non-root execution, no automatic service-account
token mount, a read-only root filesystem and a writable bounded `emptyDir` cache.
Its public `/` landing page is the startup/readiness/liveness probe; no dedicated
frontend health endpoint exists. It receives only public Auth0 domain/audience,
frontend connection settings and its own Auth0 Secret, never backend MongoDB,
Redis or OpenBao credentials.

MongoDB starts as a three-member StatefulSet with one PVC per member, a headless
Service, and stable in-cluster DNS names. An idempotent Job initializes `rs0` or
validates its existing membership, then waits for a primary and two secondaries.
The API init containers wait for primary connectivity and OpenBao key provisioning
before application startup.
The revision-named Job is an ordinary resource, not a post-install hook, so
`helm --wait --wait-for-jobs` cannot deadlock waiting for that hook to run.

MongoDB is not externally exposed. Its ingress NetworkPolicy permits only the
release's API, worker, MongoDB, and initializer pods; enforcement requires a CNI that
supports NetworkPolicy. The bundled database has no authentication or TLS and is
for development only. Do not use it for production data.

Redis runs as a single-node non-root StatefulSet with a persistent PVC, AOF
(`appendonly yes`, `appendfsync always`), `maxmemory 192mb`, a 256 MiB memory cap,
and `noeviction`. It is unauthenticated, cluster-internal, and development-only.
Its ingress NetworkPolicy permits only this release's API/worker pods.
The worker Deployment uses the same application image and
`python -m app.worker`; it receives MongoDB/Redis configuration only, without
Auth0 settings, OpenBao tokens, projected JWTs, or KMS CA mounts.

OpenBao runs as a non-root development Deployment with a ClusterIP Service and an
ingress NetworkPolicy permitting only API/bootstrap pods. A generated development
root Secret is shared only with that server and a revision-named bootstrap Job.
The Job provisions Transit, KV v2, keys, policy, and Kubernetes authentication.
OpenBao's ServiceAccount gets `system:auth-delegator` for TokenReview. API pods use
their own dedicated ServiceAccount and an audience-bound JWT, never the root token.
Installing the bundled profile therefore requires ClusterRoleBinding privileges.

The bundled KMS is ephemeral. After an OpenBao pod restart, run `make helm-up`
to bootstrap again, then restart the API Deployment to discard its old cached
keys. This cannot recover previously buffered encrypted envelopes or retained
log fields encrypted under the lost storage key. Do not restart development
OpenBao if encrypted test logs must remain readable. The chart
does not implement persistent production OpenBao or automatic unsealing.

## External MongoDB, Redis and OpenBao / production

Provision an authenticated, TLS-enabled MongoDB replica set independently. Configure
the real URI in the git-ignored `.env`, or use your cluster's secret-management
operator to provision the Secret. Never pass the URI in `--set` or a values file:
Helm persists release values in the cluster.

Provision authenticated TLS Redis **7.2+** independently with persistent AOF,
`appendonly yes`, preferably `appendfsync always`, a finite `maxmemory` with `noeviction`,
durable disk capacity, backups and tested recovery. Startup validates
`INFO persistence` returns `aof_enabled=1`, `INFO memory` returns
`maxmemory_policy=noeviction`, and `WAITAOF` is supported. External Redis ACLs must
permit `INFO`, stream/group commands, `EVAL`, `WAITAOF`, and transactional ACK/delete
(`MULTI`/`EXEC`), with access to the configured stream/group keys. Cache-only or
non-AOF services are unsuitable.
The confirmation is a local fsync, not a replication/failover-loss guarantee;
external HA requires suitable persistence and failover policy.
Provision a namespace-local existing Secret
`logger-redis` containing `REDIS_URL=rediss://...` through your secret-management
operator. Never place its URL/credentials in values or `--set`.

Provision persistent, initialized/unsealed, TLS-enabled OpenBao and its audit,
backup, and recovery processes separately. Initialize its wrapped key set and
runtime policy with the operator CLI described in [ENCRYPTION.md](ENCRYPTION.md).
Configure Kubernetes auth with the exact namespace/API ServiceAccount/audience
binding before deploying; the default role is `logger-ingestion` and audience
`openbao`. With release `logger`, the API ServiceAccount is
`logger-amazing-http-logger-api`. The OpenBao server, not the API, needs appropriate
TokenReview credentials. For an external-to-cluster KMS, configure a reachable
API endpoint, CA, and rotating reviewer credentials explicitly.

```bash
kubectl --context=YOUR_CONTEXT create namespace logger
kubectl --context=YOUR_CONTEXT -n logger create secret generic logger-mongodb \
  --from-env-file=.env
# Populate this private, git-ignored file independently; do not put it in Helm values.
kubectl --context=YOUR_CONTEXT -n logger create secret generic logger-frontend-auth0 \
  --from-env-file=.secrets/frontend-auth0.env
docker build -f docker/Dockerfile -t registry.example.com/team/amazing-http-logger:0.1.0 .
docker build -f docker/frontend.Dockerfile -t registry.example.com/team/amazing-http-logger-frontend:0.1.0 .
docker push registry.example.com/team/amazing-http-logger:0.1.0
docker push registry.example.com/team/amazing-http-logger-frontend:0.1.0
helm upgrade --install logger helm/amazing-http-logger \
  --kube-context YOUR_CONTEXT --namespace logger \
  --set externalMongo.existingSecret=logger-mongodb \
  --set externalRedis.existingSecret=logger-redis \
  --set openbao.address=https://bao.example.com \
  --set auth0.domain=your-tenant.auth0.com \
  --set auth0.audience=https://your-api \
  --set image.repository=registry.example.com/team/amazing-http-logger \
  --set-string image.tag=0.1.0 \
  --set frontend.image.repository=registry.example.com/team/amazing-http-logger-frontend \
  --set-string frontend.image.tag=0.1.0 \
  --set frontend.auth0.existingSecret=logger-frontend-auth0 \
  --set frontend.appBaseUrl=https://logger.example.com \
  --wait --wait-for-jobs --timeout 10m
```

The Secret must be in the release namespace and contain `MONGO_URI` (or the key
selected by `externalMongo.uriKey`). Kubernetes environment variables do not reload
when a Secret changes; restart both API and worker Deployments after MongoDB/Redis
credential rotation. `externalRedis.urlKey` defaults to `REDIS_URL`. The MongoDB URI must specify
`replicaSet=<name>`, plus authentication/TLS options, and every advertised member
hostname must be resolvable and reachable from the pods. `directConnection=true`
is rejected.

The frontend Secret must contain `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, and
`AUTH0_SECRET` (a cryptographically random 32-byte session secret, encoded as
64 hex characters). Use an Auth0 Regular Web Application; configure the exact
`https://logger.example.com/auth/callback` callback and the public origin as its
allowed logout URL. The top-level `auth0.domain`/`auth0.audience` are shared by
API and frontend; private client credentials are not. Restart the frontend
Deployment after rotating this Secret. Neither lint nor rendering checks that
referenced Secrets actually exist or contain usable credentials.

The frontend image uses the repository root as build context, Node 22 Alpine,
`npm ci`, and `npm run build`; Next.js must enable `output: "standalone"`.
Its non-root production runner starts the standalone server on `0.0.0.0:3000`,
copies static assets and any optional `public/` contents, and needs no build-time
Auth0 credentials. Supply credentials only at runtime.

### Public ingress and trusted TLS termination

For private image registries, configure shared `imagePullSecrets`. Both Services
are ClusterIP-only (backend 9090, frontend 3000). The chart bundles **no NGINX or
other reverse-proxy workload**. Either configure an external TLS gateway or enable
the chart's optional Ingress resource against an independently installed controller:

```yaml
frontend:
  appBaseUrl: https://logger.example.com
  auth0:
    existingSecret: logger-frontend-auth0
app:
  requireHttps: true
  # Example only: replace with the actual ingress controller source IPs/CIDRs.
  forwardedAllowIps: "10.244.4.0/24"
ingress:
  enabled: true
  className: nginx
  host: logger.example.com
  annotations: {}
  tls:
    - secretName: logger-tls
      hosts: [logger.example.com]
```

Provision that TLS Secret separately. The Ingress sends `/api/v1` and `/docs`
(`Prefix`), `/redoc`, `/openapi.json`, and `/health` (`Exact`) to the backend;
`/` (`Prefix`) goes to the frontend, including `/auth/*` and Next.js assets.
Do **not** strip prefixes or add blanket rewrite annotations. With
`frontend.enabled=false`, only the backend paths are generated.

TLS may terminate at the controller/gateway and use HTTP to the pods, but it must
overwrite untrusted forwarded headers and send `X-Forwarded-Proto: https`.
Restrict `app.forwardedAllowIps` to its **actual** source IPs/CIDRs, never `*` or
an all-address CIDR; the loopback default is not sufficient for a remote ingress.
Restrict direct backend access with deployment/network controls so clients cannot
impersonate that proxy. Keep backend `requireHttps=true`; do not disable it to
work around an incorrect trust configuration. If an upstream external TLS gateway
precedes an HTTP Ingress, configure the controller to trust forwarded scheme
headers **only** from that gateway and preserve the verified HTTPS scheme.

`BACKEND_API_URL` defaults to `frontend.appBaseUrl`, so SSR bearer API requests
loop through the same public HTTPS ingress `/api/v1` route. Frontend pods must
resolve and reach that public origin, including any gateway hairpin/NAT path.
`frontend.backendApiUrl` may override it with a separately reachable **HTTPS**
API origin. A plain HTTP backend Service DNS URL is intentionally rejected by
the frontend (HTTP is allowed only for loopback development); do not weaken
that check. HTTP loopback app origins are accepted only in the development
profile, not production. Configure the gateway/controller to accept the documented
compressed batch size without stripping `Content-Encoding`. Provision certificates,
database backups, access controls, and monitoring outside this chart.

For a private KMS CA set `openbao.ca.existingSecret` (key `ca.crt` by default).
Alternatively mount a least-privilege static runtime token using
`openbao.auth.existingSecret` and `openbao.auth.tokenKey`; provision it independently,
never through values. TLS verification cannot be disabled, and production rejects
an HTTP OpenBao address. Root/provisioning credentials must not be mounted into API pods.

## Deployment lifecycle

```bash
make helm-up KUBE_CONTEXT=minikube
make helm-down KUBE_CONTEXT=minikube
```

Use immutable image tags for upgrades; changing public app settings updates the pod
checksum and rolls the Deployment. Reapplying an unchanged development chart checks
existing replica membership without reinitializing or deleting data.
OpenBao bootstrap also preserves an existing key set and the development root
Secret on upgrade. EC key rotation/retirement changes cached runtime state only
after an explicit API rollout; use the staged procedure in [ENCRYPTION.md](ENCRYPTION.md).

Uninstall removes workloads, Services, Jobs, and the bundled dev root Secret, but
**retains MongoDB and Redis PVCs**. Bundled OpenBao state is lost.
Deleting the namespace or manually deleting those PVCs can destroy data. Use a stable
release name and namespace for the development database.

For externally managed dependencies, set their reachable addresses and runtime
credential path in `.env` and use `make run` plus `make worker` instead of `make dev`. Kubernetes MongoDB DNS names remain
cluster-internal; forwarding a single member port does not preserve replica discovery.

## Device-only test database

This release removes user keys from application resources. MongoDB device
configuration `_id` values become globally unique device IDs; time-series metadata
contains `device_id` and a server-owned `suspicious` flag. `/me` and log responses
no longer expose `user_id`.
`manage:devices` now grants fleet-wide management and reads, rather than a user's
namespace. Review existing Auth0 grants before deployment; do not give this
permission to ordinary agents.

The old database contained only test data and was explicitly reset for this
schema. There is no migration and startup never deletes data automatically.
Future test-data resets must target only the explicitly approved application
database, with API/ingestion writers stopped. Startup recreates the time-series
collection and global uniqueness indexes in an empty database.

Registration is create-only: existing IDs (including policy/status-only records)
or names return `400`; updates still use the versioned policy PATCH API.

## Run tests
For the JavaScript SSR management UI, see [`frontend/README.md`](../frontend/README.md).
Run `make frontend-dev` on loopback port 3000 with the backend (9090) and NGINX
proxy running; browse through the shared public development origin on port 8080.
It requires its own Auth0 Regular Web Application in `frontend/.env.local`, not
agent M2M credentials. `make frontend-test` checks adapter tests and lint;
`make frontend-test-browser` builds and runs isolated browser tests.
The **Device policies** screen now registers named devices with an initial
encryption policy and loads all accessible persisted devices from the API,
including unnamed/version-zero records created by agent acknowledgements or policy
changes. Open a row to inspect device identity, edit policy, check acknowledgement
and public encryption details, or view its traffic. A missing registration timestamp
does not hide a stored device. Use the agent's
JWT-bound ID, not an arbitrary upload-supplied ID. IDs and names are globally unique.
Registration and policy changes need `manage:devices`; credentials are provisioned
separately through the issuer. See the [registration contract](../README.md#register-a-device-and-configure-its-policy)
and [real browser/API/MongoDB test](../frontend/README.md#validation).

The **Workspace > Storage and encryption** section (`/settings`) controls original
bodies, authorization headers and cookies for all devices. Each category offers
**Do not retain**, **Retain encrypted** (default), or **Retain plaintext**.
Discard removes body content before publishing/KMS but preserves its byte length
and SHA-256 digest; discarded header entries disappear from both maps.
Changes affect future uploads only, not existing records. Discarded data cannot
be recovered; plaintext retention is a deliberate opt-out from encryption.
Both workspace API reads and versioned updates require `manage:devices`.
Existing installations can run `python -m app.kms initialize` to provision the
dedicated `OPENBAO_STORAGE_KEY` and runtime ACL without replacing existing keys.
Rotate with `python -m app.kms rotate-storage`; retain old versions and persistent
OpenBao backups throughout log retention. Follow the
[algorithm, nonce, usage-limit and retention requirements](ENCRYPTION.md#workspace-storage-encryption).

Install the current dependencies with `make install`. The backend uses native async
PyMongo, the official Auth0FastAPI SDK for token verification, and HTTPX2 for HTTP
clients and Starlette's supported test-client backend. The SDK also installs HTTPX
for its own requests. Deprecation and pending-deprecation warnings are test failures,
except the SDK's current `authlib.jose` import warning: that exact upstream warning
remains visible in pytest output. No deprecated JOSE APIs are called by application
code; application encryption uses `joserfc`.

```bash
pytest -q
ruff check app tests integration_tests
```

To exercise local time-series storage and primary analysis, point `MONGO_TEST_URI` at
the single-node replica set. Use `MONGO_REPLICA_TEST_URI` for a separate multi-member
replica set to exercise strict secondary routing. Run `pytest -q tests/test_mongo_integration.py`. Each test
creates a unique `traffic_test_*` or `device_registration_test_*` database and drops it afterward. Do not point tests
at an unapproved production cluster.

For the local Compose database:

```bash
MONGO_TEST_URI='mongodb://localhost:28017/?replicaSet=rs0' \
  .venv/bin/python -m pytest -q tests/test_mongo_integration.py
```

To include real KMS wrapping, key rotation/retirement, runtime permission denials,
and OpenBao-to-API-to-MongoDB encrypted ingestion:

```bash
make dev-up
MONGO_TEST_URI='mongodb://localhost:28017/?replicaSet=rs0' \
OPENBAO_TEST_ADDR='http://localhost:28200' \
OPENBAO_TEST_ADMIN_TOKEN_FILE='.secrets/openbao-dev-root-token' \
OPENBAO_TEST_TOKEN_FILE='.secrets/openbao-token' \
  .venv/bin/python -m pytest -q
```

These tests create unique Transit keys/KV records/policies and revoke/delete only
their own test resources afterward. They do not change the application key set.
Use only an approved development OpenBao: this optional integration requires
administrative privileges. The strict-secondary test needs the separate
`MONGO_REPLICA_TEST_URI`; it is skipped with the one-node development database.

For the optional Kubernetes database, run the integration test inside the API pod
(the NetworkPolicy allows that pod to reach MongoDB):

```bash
kubectl --context=minikube -n logger exec deployment/logger-amazing-http-logger -c api -- \
  python -c 'import os, subprocess, sys; os.environ["MONGO_REPLICA_TEST_URI"] = os.environ["MONGO_URI"]; subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_mongo_integration.py"], check=True)'
```

### Whole-stack OAuth integration test

This opt-in test uses only public HTTP endpoints: **no Auth0 dependency overrides,
in-process app transport, direct database access, or private KMS credentials**.
Run the backend with MongoDB, Redis, a worker and a provisioned OpenBao first
(`make dev` plus `make worker` in another terminal locally, or deploy Helm).
It exercises durable queued ingestion and
reads accepted records through analysis, waiting for replication/processing.
Deployment analysis must have a readable secondary; the local single-node profile
uses its existing explicit development-only primary-read setting.

Create **two different Auth0 Machine-to-Machine applications**, authorize both for
the backend API audience, and enable the `client_credentials` grant. Neither
application needs `manage:devices`: the workers act only as agents and never
PATCH a device policy. These test-only credentials should not be deployed to
ordinary agents.

Each client authenticates as its own device. The test obtains its JWT-bound
device ID from `/api/v1/me`, instead of decoding an unverified token or assuming
an identifier format. Resources no longer include a user key.

Configure device policies before the test using the ID returned by `/api/v1/me`.
Device identity comes from the signed `https://amazing-http-logger/device_id`
claim, otherwise the verified subject. Optional
`STACK_TEST_PLAINTEXT_DEVICE_ID` and `STACK_TEST_ENCRYPTED_DEVICE_ID` values are
assertions only: they must match `/me`, and cannot select a different device.
Leave them empty to discover the IDs automatically. Old `stack-test-*` policy
IDs must be bound by the issuer or their policies recreated for the JWT-bound IDs.
The plaintext
device must use `tls_only`; the encrypted device must
allow JWE (`optional` or `required`). An incompatible policy fails with a setup
message instead of being changed automatically.

Policy management remains a frontend/operator responsibility. An operator with
`manage:devices` can configure either M2M device by its exact JWT-bound ID.
Do not grant this fleet-wide permission to the agent test clients.

```bash
cp .env.stack-test.example .env.stack-test
chmod 600 .env.stack-test
# Edit .env.stack-test locally: set the backend URL, Auth0 token URL/audience,
# and the separate plaintext/encrypted client IDs and secrets.
# Configure the devices' policies before running.
make test-stack
```

Configure `STACK_TEST_TOKEN_URL` as `https://YOUR_TENANT.auth0.com/oauth/token`;
the audience must match the running backend's `AUTH0_AUDIENCE`. The example is
disabled for ordinary `pytest`; `make test-stack` explicitly enables it, so missing
credentials fail rather than silently skipping. To select another credential file:

```bash
make test-stack STACK_TEST_ENV_FILE=/secure/stack-test.env
```

Alternatively export the `STACK_TEST_*` settings and run
`STACK_TEST_ENABLED=true .venv/bin/python -m pytest -q integration_tests/test_stack.py`.
The default `.env.stack-test` is optional when all values come from the environment.
This standalone test does not load the application unit-test fixtures or `.env`.
Environment variables override profile values. The two client IDs must differ;
explicitly configured expected device IDs must also differ.

Two worker threads independently obtain fresh bearer tokens, discover their
identities, and read their existing device configurations without changing them.
Both acknowledge the advertised policy version; the encrypted device reports the
active key. It sends JSON -> gzip -> compact JWE using only the advertised suite
and public key. If its existing policy is `required`, the test also verifies
that a gzip-only upload is rejected. The plaintext device sends gzip JSON. Both
submit three synthetic traffic observations with unique per-run URLs and no
identity, IP or other attribution fields. The backend adds JWT/connection metadata.

The test waits for records from each acknowledged batch, following analysis
pagination as necessary. Older batches on the same stable device do not affect
the counts. It verifies receipt IDs, device attribution, redaction and body minimization,
then checks cross-device access denial (`403` for explicit list/configuration queries,
`404` for inaccessible record IDs). Policies are never changed or reset.

`STACK_TEST_HTTP_TIMEOUT` bounds each request (default 10 seconds);
`STACK_TEST_VISIBILITY_TIMEOUT` bounds the read-back wait (default 30 seconds).
Increase the latter for queue backlog or slower replicas. A `503` or any other
unexpected HTTP status fails explicitly; the test does not fall back to primary
or retry an upload that could have partially persisted.

Only loopback backend HTTP is allowed. The authorization server always requires
HTTPS. For a private backend CA, set `STACK_TEST_CA_CERT` to its PEM bundle;
system roots remain trusted. Certificate verification stays enabled, and redirects
and environment proxies are not followed.

The credential profile is ignored by Git and Docker builds. Keep it private; never
commit secrets or print token responses. Do not enable HTTP debug logging or
`pytest --showlocals` when using real credentials. Failure messages omit token
responses, credentials and request bodies.

Run only against an approved test stack. **Each run leaves six synthetic logs and
updates status for the JWT-bound devices**; the API
does not expose deletion. It does not modify existing device policies; do not
target production. It needs no MongoDB/OpenBao administrator credentials. Unit tests for this
client harness use an explicit mock transport; they do not claim a live Auth0 run.

## Retention operations

Use **Workspace > Retention policies** to enable/disable or change each category's
window. Ordinary logs default to 7 days; suspicious logs default to 30 days.
Disabled means indefinite retention for that category; its chosen window remains
saved. `manage:devices` is required. Retention applies to existing records too,
using event timestamps. Back up evidence before enabling or shortening a window:
deletion is permanent, and old offline uploads may immediately qualify.

MongoDB 7.0+ is required. The API's database role needs `createIndex`, `dropIndex`
and `listIndexes`, in addition to its existing collection/settings privileges.
Inspect `db.traffic_logs.getIndexes()` for `retention_ordinary` and
`retention_suspicious`; each enabled category has its own partial TTL index on
`timestamp`. Disabling a category drops only its index. Window changes rebuild
the affected index on MongoDB 7, so plan large-collection changes off peak.
Never configure a collection-wide `expireAfterSeconds`: it overrides longer
suspicious retention and causes startup to fail. Remove any pre-existing global
TTL with an authorized, reviewed MongoDB collection change before deployment.

Settings are majority-persisted before index application. A failure returns
sanitized `503`, not success, but the desired settings may already be saved and
some old indexes may remain active. Reload, fix permissions/connectivity, then
save again using the current version, or restart the backend to reconcile indexes.
Do not assume expiration was disabled after a failed save.
Startup fails explicitly if retention indexes cannot be reconciled.

MongoDB's monitor deletes whole expired time-series buckets in the background,
typically on a 60-second cycle with additional bucket-span/load delay. A seconds
granularity bucket can span an hour; retention is not a strict erasure deadline.
Disabling expiration cannot cancel a deletion already in progress.

New records store `metadata.suspicious=false`; old records without the flag use
ordinary retention. Authorized analysis responses expose the Boolean.
No analysis job, classification API, or existing-data migration is supplied.
A future trusted classification job must address time-series per-record update
restrictions and finish before ordinary TTL deletes the evidence.

Validate actual TTL deletion and index changes in an approved development replica set:

```bash
MONGO_TEST_URI='mongodb://localhost:28017/?replicaSet=rs0' \
  .venv/bin/python -m pytest -q tests/test_mongo_integration.py -k retention
```

This test creates/drops only its unique `retention_test_*` database and waits for
real TTL-monitor deletion (allow several minutes); it does not change global
MongoDB settings or the application's database/indexes.

## Storage initialization

Startup creates `traffic_logs` with `timeField=timestamp`, `metaField=metadata`, and
`granularity=seconds`, and checks that an existing collection has compatible options.
It fails rather than using a regular collection with the same name. Index creation
and collection administration happen against primary; only analysis reads use replicas.

New records use `{_id, timestamp, metadata, ingestion, logdata, storage_encryption?}`.
`metadata` contains only device/classification bucket identity. Batch/client IDs,
upload peer and reception time are under `ingestion`; captured HTTP/network/process
fields and body length/digest are under `logdata`. Do not move per-upload values
into time-series metadata. See the [document example](../README.md#stored-document-layout).
The HTTP upload/read contracts remain flat and unchanged; historical flat records
are still readable and decryptable without migration or data deletion.

Startup creates `ingestion_received_at` on `ingestion.received_at` and
`ingestion_batch_record` on `(ingestion.batch_id, _id)`. Existing
`device_timestamp`, `timestamp_id` and both retention indexes keep their paths.
List projections exclude both `logdata.body` and historical root `body`, as well
as ciphertext. Existing legacy `received_at`/`batch_record` indexes are not
automatically dropped. Keep `batch_record` while old version-1 messages can still
be retried; remove legacy indexes only after checking their usage and draining
those batches, on the explicitly selected application database.

Deploy the new workers **before** new collectors: workers accept old version-1
flat BSON envelopes and new version-2 nested envelopes. Older workers cannot consume
version 2 and leave such messages pending; do not downgrade workers until those
batches have drained. Neither deployment nor startup migrates stored records,
purges the stream or resets the database. Direct MongoDB consumers must support
both layouts while historical records remain.

## Operational semantics

- Batch uploads require one gzip member and `application/json`. Limits are 500 records,
  1 MiB compressed, and 8 MiB decompressed; size violations return `413`.
- HTTP `200`, `status=queued` acknowledges Redis `XADD` plus same-connection
  `WAITAOF 1 0 <timeout>` confirmation of local AOF fsync, **not** MongoDB processing.
  There is no direct-write fallback or guarantee of instantaneous response:
  policy/config reads and OpenBao field protection still complete before queuing,
  but MongoDB, OpenBao and Redis network I/O is async. Resource providers are async;
  collector CPU preparation uses `asyncio.to_thread` rather than FastAPI's shared
  AnyIO thread limiter. MongoDB log writes run only in the separate worker.
- Queue unavailability, capacity/memory exhaustion, or missing fsync confirmation
  returns `503`. A timeout can leave an enqueued batch despite the error.
  Delivery is at-least-once. Worker retry deduplication by stable `(batch_id, _id)` is best-effort:
  time-series has no unique IDs, and partial/ambiguous writes may duplicate records.
  Client resubmissions get new IDs; use bounded retry policies.
- Replica reads are eventually consistent. New records may briefly be missing; `404`
  does not prove that a just-accepted record was never written.
- No secondary means analysis returns `503`, even while ingestion remains healthy.
  The explicit local-development primary-read profile does not require a secondary.
- `/health` reports process liveness only; monitor Redis/AOF/worker and primary/secondary availability
  and ingestion/read error rates separately, along with OpenBao seal/auth/error metrics.
- Fresh startup requires OpenBao. EC keys stay cached in RAM for the process lifetime;
  a running backend may continue decrypting during a KMS outage. Revoking a KMS token
  does not remove cached keys. Restart all processes after EC rotation/retirement.

## Redis queue and worker operations

| Environment setting | Default | Helm value |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:28379/0` (development) | `externalRedis.existingSecret` / `urlKey` (`REDIS_URL`) |
| `REDIS_STREAM` | `traffic-log-batches` | `queue.stream` |
| `REDIS_GROUP` | `mongo-writers` | `queue.group` |
| `REDIS_MAX_BATCHES` | `10000` | `queue.maxBatches` |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `3` | `queue.socketTimeoutSeconds` |
| `REDIS_AOF_TIMEOUT_MS` | `2000` | `queue.aofTimeoutMs` |
| `WORKER_RECLAIM_IDLE_MS` | `60000` | `worker.reclaimIdleMs` |
| `WORKER_WRITE_TIMEOUT_SECONDS` | `30` | `worker.writeTimeoutSeconds` |
| `WORKER_RETRY_SECONDS` | `1` | `worker.retrySeconds` |

The socket timeout must leave room for AOF confirmation. Keep reclaim idle time
above the maximum write duration plus network/ACK margin, or another worker can
reclaim a still-active batch. Keep `worker.terminationGracePeriodSeconds` (default
60) above `worker.writeTimeoutSeconds` (default 30) plus the Redis acknowledgement
deadline (3-second socket timeout by default) and cleanup margin. SIGTERM stops new work
and permits an in-flight write to finish; a forced kill leaves pending work for
reclaim. Each worker reads **one producer batch of 1–500 events** at a time.
Scale via `worker.replicaCount`; all replicas must share the stream/group.
Increasing replicas does not guarantee ordering or exactly-once delivery.

Bundled Redis AOF uses `always` to fsync each write without waiting for an
every-second flush. The collector retains `WAITAOF` to confirm **local** fsync
before acknowledging each batch. External AOF `everysec` can work with the
2000 ms AOF timeout and 3-second socket timeout, but normally adds up to a second
to collector latency; a 1000 ms AOF timeout can race the flush schedule.
Disk/fsync delays can still cause timeouts with either policy.
Preserve its data volume/PVC across restarts, monitor
disk space/AOF rewrite headroom and fsync latency, and test AOF recovery/backups.
Redis maxmemory must stay below its container limit with headroom for AOF buffers,
rewrites and fragmentation; the development 192 MiB/256 MiB defaults are not
production sizing. Capacity limits include unprocessed/pending entries.
No `MAXLEN`/`XTRIM` or eviction may remove pending work. When the batch or memory
bound is reached, uploads fail explicitly with `503` until capacity recovers.

Monitor stream length, consumer-group lag and pending counts/oldest idle time,
worker progress/retry logs, API `503` rates, Redis memory, AOF write status,
fsync latency and disk saturation. For the unauthenticated local stack:

```bash
docker compose --project-directory . --env-file .env -f docker/docker-compose.yml exec redis redis-cli XLEN traffic-log-batches
docker compose --project-directory . --env-file .env -f docker/docker-compose.yml exec redis redis-cli XINFO GROUPS traffic-log-batches
docker compose --project-directory . --env-file .env -f docker/docker-compose.yml exec redis redis-cli XPENDING traffic-log-batches mongo-writers
docker compose --project-directory . --env-file .env -f docker/docker-compose.yml exec redis redis-cli INFO persistence
```

These commands inspect metadata, not batch contents. Avoid printing Redis URLs,
payloads, tokens or decrypted fields. Use secret-safe authenticated tooling in production.

Workers ACK/delete **only after majority MongoDB bulk insertion**. If MongoDB is
unavailable, fix connectivity/credentials, restore workers and let pending entries
be retried/reclaimed; do not delete the queue or switch to direct API writes.
Poison/version-invalid messages are logged and remain pending for operator
investigation, without automatic discard/dead-lettering. Retain the original
durable entry, diagnose without exposing payload data, deploy a compatible fix
and retry. Any exceptional manual deletion requires explicit authorization and
an audit/recovery plan; it forfeits delivery.

After a worker crash or ambiguous MongoDB response, stable `(batch_id, _id)` pairs permit best-effort
duplicate avoidance, but time-series unique indexes cannot enforce it. A record
may be inserted twice even if the worker later ACKs successfully. After Redis
failure, restore its original persistent AOF and consumer-group state; do not
recreate an empty queue and assume acknowledged batches were processed. Analysis
may lag both the queue and MongoDB replication; `queued` is not a read-after-write
guarantee.

## Troubleshooting
- For local MongoDB, inspect `docker compose -f docker/docker-compose.yml ps` and
  its logs; its healthcheck initializes the replica set. Verify `MONGO_PORT` is
  free before starting it.
- If the local API port is occupied, change `APP_PORT`; do not stop an
  unrelated backend/container.
- If MongoDB is unavailable, inspect `kubectl -n logger get pods,jobs,pvc`, the
  revision's `logger-amazing-http-logger-mongo-init-*` Job logs, pod events, DNS,
  and the external Secret reference.
- Pending MongoDB pods usually require a working StorageClass; image-pull failures
  require loading the development image or configuring registry credentials.
- For an API pod stuck in init, inspect `kubectl -n logger logs deployment/logger-amazing-http-logger -c wait-for-primary`.
- For KMS initialization failures, inspect the `wait-for-openbao` init container
  and `logger-amazing-http-logger-openbao-bootstrap-*` Job. Check OpenBao health,
  unseal state, policy, JWT audience/ServiceAccount binding, and TokenReview access.
- For local KMS errors, rerun `make openbao-up` to bootstrap/refresh credentials,
  then restart Python. Never diagnose by printing tokens or private key responses.
- If development OpenBao restarted, its previous keys are lost; re-bootstrap does
  not restore them. Use persistent external OpenBao for durable key retention.
- If analysis returns `503`, verify readable secondaries with `rs.status()`; do not
  change to `secondaryPreferred` or primary as a fallback.
- Startup requires the documented time-series collection options and fails explicitly
  if an incompatible collection is present.
- `MONGO_SERVER_SELECTION_TIMEOUT_MS` controls how long MongoDB connectivity checks wait.
- If auth requests fail, check the `.env` values and token configuration.
- If imports fail, ensure dependencies are installed in the active virtual environment.
