# Runbook

## Prerequisites
- Python 3.11+
- Docker and Docker Compose for MongoDB/OpenBao/NGINX and MinIO/emulator/bridge
- For Kubernetes deployment only: Kubernetes 1.27+, kubectl, Helm 3 or 4, and a
  StorageClass when using bundled MongoDB

For HTTPS proxy configuration, device encryption policies, OpenBao provisioning, and
rotation, see [Log encryption and device configuration](ENCRYPTION.md).

## Upload ingestion

The [pipeline design](INGESTION.md) defines stages, acknowledgement, policy and
delivery guarantees. This section owns setup and recovery; see the
[API reference](API.md#upload-a-batch) for payloads and receipt polling.

### Local MinIO and Pub/Sub emulator

```bash
make install
make dev-up
```

`make dev-up` generates two ignored private MinIO credential files, builds
pinned MinIO community server/client releases from source, starts MinIO on
loopback **29000** (console **29001**), a Pub/Sub emulator on **28681**, and a
private unexposed MinIO-notification bridge. Archived public MinIO images/binaries
are no longer available, so the first source build can take several minutes.
The API/workers run on the host without application image builds;
the bridge/MinIO setup builds images.
The MinIO volume persists raw uploads; the Pub/Sub emulator is ephemeral.
After emulator restart rerun `make dev-up` and replay outstanding raw uploads.
`make spool-dev-up` remains a convenience setup target.
All services live in `docker/docker-compose.yml`: `spool` selects these three
services, while `tools` also enables the one-shot MinIO initializer.
Make handles credentials, topic provisioning and notification initialization;
starting a Compose profile alone does not replace these bootstrap steps.
Synthetic data only: this local MinIO volume does not provide production
encryption-at-rest guarantees.

Use the local `SPOOL_*`, `PUBSUB_EMULATOR_HOST` and `GOOGLE_CLOUD_PROJECT`
defaults in [`.env.example`](../.env.example), preserving existing Auth0/OpenBao
values in your ignored `.env`. Keep both MinIO credential-file paths.

`make dev` starts the API and both workers. To run separate host processes:

```bash
make run
# Other terminals:
make spool-parser    # decrypt / decompress / validate / minimize / encrypt
make spool-loader    # rate-limited Mongo writes / raw cleanup
```

Stop host processes with Ctrl+C; `make dev-down` stops the development
containers and retains MongoDB/MinIO volumes.
Do not delete MinIO credential files while retaining its volume.

### Production GCP and Helm

For Cloud Run, follow the [GCP deployment summary](../infra/gcp/README.md#deployment-summary),
including `notification_channels`, secret bootstrap and scoped identities.
`terraform output spool_environment` provides nonsecret connection settings.
Use workload identity/ADC, never repository-stored service-account JSON keys.

For GKE, both workers always deploy. Set `bucket`, `eventTopic`, `subscription`,
`silverTopic`, `silverSubscription`, `parseDlqTopic`, `loadDlqTopic` and
`leaseSeconds` under Helm `spool`, plus workload-identity annotations under
`spool.apiServiceAccountAnnotations` / `spool.loaderServiceAccountAnnotations`.
Provision GCS/Pub/Sub/IAM independently and bind the annotated Kubernetes service
accounts to their Google identities. Parser and API share the existing API
ServiceAccount/OpenBao role; the loader gets its own account and no OpenBao token.
Helm deploys workers, not cloud resources. Production must leave
`SPOOL_ENDPOINT_URL` and `PUBSUB_EMULATOR_HOST` unset.

Provision separate object and silver subscriptions with 14-day retention (or an
explicit reviewed policy), no subscription expiration, and retry backoff 10-600s.
Object redrive targets the parse DLQ; silver redrive targets the load DLQ, both
with five native delivery attempts. Give the Pub/Sub service agent subscriber
permission on each source subscription and publisher permission on its DLQ topic.
Give the parser object-subscriber and silver/parse-DLQ publisher roles; give the
loader silver-subscriber and load-DLQ publisher roles. Retain inspection
subscriptions on both DLQs. Configure Cloud Monitoring alerts for DLQ depth > 0,
silver undelivered messages > 500 and oldest unacknowledged age > 3600 seconds,
with the age warning strictly below retention. The GCP Terraform deployment
provisions these resources/policies; Helm needs them provisioned externally.

### Retry, backpressure and recovery

`SPOOL_PROCESSING_SECONDS=60` bounds a worker operation;
`SPOOL_LEASE_SECONDS=180` protects its atomic stage claim. The notification
ack deadline is lease + 30 seconds (Pub/Sub maximum 600). Size shutdown grace
for in-flight operations and keep the lease longer than processing/network margins.
Both hops use explicit Pub/Sub ACK and deadline-expiry redelivery; receipt leases prevent competing ordinary
Mongo writes. Ambiguous Mongo inserts remain at-least-once, not exactly-once.

Configure replica count and per-replica rate together; the
[buffering contract](INGESTION.md#buffering-and-backpressure) owns defaults/limits.
After notification expiry or emulator restart, identify nonterminal receipt IDs
and replay after restoring capacity. This is administrative receipt inspection,
not bucket listing as a normal ingestion trigger.

On parse/load DLQ depth > 0 or a persistent lag alarm:

Native redrive/expiry can leave receipts nonterminal; inspect state before
choosing replay or cleanup. Restrict DLQ access: native load messages can contain
documents, not just sanitized pointers. Never print payloads.

1. Inspect only receipt IDs and sanitized error codes; do not dump raw bodies,
   headers, token values or KMS errors into logs or tickets.
2. Fix parser/key availability, field-encryption/KMS, Pub/Sub quota/IAM or Mongo.
   Retain upload EC keys for the complete raw backlog, including failed uploads.
3. Run `python -m app.spool_worker replay <upload_id>` with the environment and
   operator credentials for that deployment. This checks raw availability,
   resets retry state and publishes an object notification; it refuses active
   leases and already stored/deleted uploads. Operator IAM needs raw read,
   receipt updates and event-topic publisher access.
4. Verify `stored` and `raw_deleted=true`, then acknowledge the corresponding
   inspected DLQ message. Do not bulk-drain a DLQ before successful replay.

For `stored` with `raw_deleted=false`, run
`python -m app.spool_worker cleanup <upload_id>` with raw-delete permission.
This also handles a lost Pub/Sub cleanup message and a prior delete whose reply was
lost. It never repeats Mongo loading.

Before production, approve an incident/privacy escalation deadline and operator
process for abandoning failed raw; automatic age expiry is not implemented.
Follow the [raw-retention constraints](INGESTION.md#dead-letters-cleanup-and-raw-retention)
before changing bucket lifecycle, versioning, soft deletion or backups.

## Pipeline status

Use **Status** in the frontend sidebar, or authenticated `GET /api/v1/status`
with `manage:devices`. The same implementation serves local development, Helm
and GCP, without contacting Docker, Kubernetes or Cloud Run control planes.
The page refreshes manually and is never cached.

Checks run concurrently with three-second per-probe limits: bucket metadata,
both Pub/Sub subscription metadata, operational Mongo receipts/heartbeats and
the configured analysis read target. They do not upload synthetic data, pull/ACK
messages, decrypt logs or fall back to primary analysis reads. A failed check
reports unavailable, including when metadata permissions are missing.

Workers publish per-instance health to the majority-backed `pipeline_workers`
collection every ten seconds, including while long-polling or rate-limited.
Reports include consumer-loop dependency errors; reporting failures are logged
without blocking ingestion. Graceful shutdown reports unavailable. A crashed
worker becomes stale after `max(60, SPOOL_PROCESSING_SECONDS + 60)` seconds
(120 seconds by default). Expired instance documents are removed after 24 hours;
a worker with no retained report is unknown, not healthy. Restart existing
parser/loader processes after installing this feature.

Outstanding receipt counts and oldest state-update times identify object/silver
waits or raw cleanup older than one processing lease. Unresolved `parse_failed`
and `load_failed` receipts degrade the corresponding worker until replayed.
Counts are **not** native Pub/Sub backlog or DLQ depth; retain Cloud Monitoring
alerts. Metadata reachability/heartbeats do not guarantee successful writes,
KMS availability during future work, event delivery, or end-to-end correctness.
The local notification bridge is covered indirectly by stalled object receipts,
not by a separate privileged control-plane probe.

GCP Terraform grants the backend `storage.buckets.get` on its raw bucket and
`roles/pubsub.viewer` on the two source subscriptions, in addition to existing
object permissions. It does not give the API permission to consume messages.
For Helm/external IAM, grant equivalent scoped metadata permissions to the API
identity. Apply those IAM changes and deploy updated workers before expecting
all nodes to be healthy. No Auth0 scopes beyond existing `manage:devices` are needed.

## Measuring ingestion latency

Use a dedicated test device, synthetic current-timestamp gzip/JWE records,
known worker capacity and a drained backlog. The benchmark measures only uploads.
Provide a short-lived test-device token via `INGEST_BENCH_TOKEN`, not a CLI argument.

```bash
.venv/bin/python -m integration_tests.benchmark_ingestion \
  --base-url http://localhost:8080 --wire-file /path/to/synthetic-batch.gz \
  --requests 100 --concurrency 4
# For an existing compact JWE file, also pass --jwe.
```

The tool reports p50/p95/p99 HTTP acknowledgement and acknowledged batches/second.
Its default base URL is `http://localhost:8080`, exercising the public NGINX path.
It also polls terminal status and reports
`stored_observed` latency, including polling/scheduling delay. This is **not**
an exact write timestamp. Compare
`pipeline.e2e.latency_ms` logs to measure actual Mongo completion, and
check replica visibility separately. Test 1/100/500 records, gzip versus JWE,
large retained bodies, several concurrency levels, warmed/cold runs and recovery
from Mongo stalls. Acknowledgement confirms only raw durability.
No production speedup is asserted.

Focused service validation after `make spool-dev-up`:

```bash
MINIO_TEST_ENDPOINT=http://localhost:29000 \
MONGO_TEST_URI='mongodb://localhost:28017/?replicaSet=rs0' \
PUBSUB_EMULATOR_HOST=localhost:28681 \
  .venv/bin/python -m pytest -q tests/test_uploads.py
```

Use the MinIO/PubSub configuration above in `.env`. Tests create/drop only unique
test Mongo databases/Pub/Sub resources and delete their raw uploads. They do not
authenticate with live Auth0 or validate live GCS IAM/event latency.

## Local setup
```bash
make install
cp .env.example .env
```

Set backend Auth0 tenant/audience in `.env`; configure the
[frontend's separate environment](../frontend/README.md#run-locally).

## Run the live development stack

```bash
make dev
```

Compose starts one MongoDB container as a one-member `rs0` replica set. Its
healthcheck initializes it and waits for primary readiness. The backend runs in
the host virtual environment through `python -m app` with Uvicorn reload.
Python code changes do not require a Docker build, Helm, or Kubernetes.

Compose also starts OpenBao and bootstraps its Transit KEK, wrapped EC key set,
and a least-privilege runtime credential. It also starts MinIO, the Pub/Sub
emulator and the private notification bridge, provisions bucket/event resources,
and starts NGINX. `make dev` then runs the API plus parser and loader host processes.
Backend and Compose share `.env`, preserving
Auth0 settings. The backend listens on `127.0.0.1:9090`; the separately started
frontend listens on `127.0.0.1:3000`. Open the public development origin
`http://localhost:8080`: NGINX routes API/docs/health paths to the backend and
all other paths, including `/auth/*`, to the frontend without stripping prefixes.
Set frontend `APP_BASE_URL=http://localhost:8080` and
`BACKEND_API_URL=http://127.0.0.1:9090` in `frontend/.env.local`: the former is
the browser/Auth0 origin, the latter is reached by the SSR server.
Keep frontend Auth0 Web application credentials there and use `localhost`
consistently in callback/logout URLs; it is not interchangeable with `127.0.0.1`.

Compose publishes proxy port 8080 by default and uses the Docker Desktop host
gateway. No selector is needed on Docker Desktop. On native Linux, select the
host-network proxy to reach loopback-bound host applications:

```bash
make dev PROXY_SERVICE=proxy-host
# Shutdown covers both proxy variants and all other profiles:
make dev-down
```

The host-network service binds `127.0.0.1:8080` and reaches applications through
`127.0.0.1`; keep the host backend/frontend bound to loopback.
The default bridge service uses `host.docker.internal` instead.
`make proxy-up PROXY_SERVICE=proxy-host` switches just the proxy, stopping the
other variant first.

To run the components independently:

```bash
make dev-up
make run
# In a separate terminal:
make spool-parser
# In another terminal:
make spool-loader
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

All host processes load `.env`; parser needs OpenBao/EC access, loader does not.
API auto-reload does not reload workers; restart workers after code/config changes.
Stop host processes with Ctrl+C. `make dev-down` stops all development containers
and retains MongoDB/MinIO volumes.
Individual targets include `mongo-up/down`, `openbao-up/down`, and
`proxy-up/down`. All use the single `docker/docker-compose.yml`.
Profiles are `dev` (dependencies with the bridge proxy), `spool`
(MinIO/emulator/bridge), `proxy`, `proxy-host`, and `tools`
(spool dependencies plus the initializer). Do not enable both proxy variants.
`make compose-config` validates all profiles, `make compose-ps` lists containers,
and `make compose-logs` follows logs. `dev-down` covers all profiles without
deleting volumes. Direct Compose commands must include `--project-directory .`
from the repository root so bind mounts/build contexts resolve correctly.
Use the same
`COMPOSE_PROJECT` value for start/stop if overriding `amazing-http-logger-dev`.
These non-TLS services are for local development only.

OpenBao runs in **ephemeral dev mode**, not persistent production storage.
Restarting/recreating it loses the KEK and EC key set; buffered encrypted uploads
using the previous keys become undecryptable. Run `make openbao-up` to bootstrap
again, then restart API and parser. Stored encrypted MongoDB fields dependent
on the lost storage key are also unreadable; only unrelated/plaintext data survives.
The runtime token lasts 24 hours; rerun `make openbao-up` before starting Python
after expiry. Do not print tokens, commit `.secrets`, or use the root token as
the application's runtime credential.

### Shared public origin

NGINX preserves paths and gzip/JWE bytes: `/api/v1`, `/docs`, `/redoc`,
`/openapi.json` and `/health` go to Python; `/auth/*`, dashboard pages,
assets and HMR go to Next.js. `/proxy-health` checks NGINX itself.
The response-header buffer is 16 KiB for Auth0/Next.js cookies; a smaller
default can cause sign-in `502` errors. For community NGINX Ingress use
`nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"` under `ingress.annotations`.

No tunnel is needed for localhost. If deliberately using a trusted HTTPS tunnel,
point it at loopback proxy port 8080 and set `PROXY_EXTERNAL_SCHEME=https` only
while that listener is exclusively behind the tunnel. NGINX replaces untrusted
forwarding headers; keep applications loopback-bound and trust only the actual
proxy peer. Update frontend `APP_BASE_URL` and Auth0 allowlists to the public
origin, keeping the reachable internal `BACKEND_API_URL` for SSR.
Restart applications and proxy after changing addresses or upstream ports.

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
| MinIO API / console, loopback | 29000 / 29001 | local spool configuration |
| Pub/Sub emulator, loopback | 28681 | `PUBSUB_EMULATOR_HOST` |
| Public development NGINX proxy | 8080 | `.env` `PROXY_BIND_HOST` / `PROXY_PORT` |
| Host Python backend | 9090 | `.env` `APP_HOST` / `APP_PORT` |
| Host Next.js frontend | 3000 | Make `FRONTEND_PORT`; match Compose `.env` `FRONTEND_PORT` |
| Kubernetes API pod and Service | 9090 | Helm `app.port` / `service.port` |
| Kubernetes frontend pod and Service | 3000 | Helm `frontend.port` / `frontend.service.port` |
| Kubernetes MongoDB pods | 27017 | Helm `mongodb.port` |
| Kubernetes OpenBao pod | 8200 | Helm `openbao.port` |
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
docker pull ghcr.io/openbao/openbao:2.7.1
minikube image load amazing-http-logger:dev amazing-http-logger-dashboard:dev mongo:7 ghcr.io/openbao/openbao:2.7.1
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
values select **external** MongoDB and HTTPS OpenBao. Configure the required
`spool` bucket, topics/subscriptions and workload identities before deployment;
Helm does not provision GCS or Pub/Sub.
`values-development.yaml` contains illustrative GCS/Pub/Sub names for linting,
not bundled cloud emulators. Override those names and configure workload identity
for real GCS/Pub/Sub. Docker provides MinIO/emulator services for host-based tests;
this Helm profile is not a self-contained cloud ingestion stack.

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
or OpenBao credentials.

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

Parser and loader always deploy with the application image and
`python -m app.spool_worker parse/load`. Parser uses scoped raw-read, object-event
subscribe, silver/parse-DLQ publish and KMS access. Loader gets Mongo, raw-delete,
silver subscribe and load-DLQ publish permissions, without EC/KMS access.

OpenBao runs as a non-root development Deployment with a ClusterIP Service and an
ingress NetworkPolicy permitting only API, spool-parse and bootstrap pods, not
the loader. A generated development
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

## External MongoDB and OpenBao / production

Provision an authenticated, TLS-enabled MongoDB replica set independently. Configure
the real URI in the git-ignored `.env`, or use your cluster's secret-management
operator to provision the Secret. Never pass the URI in `--set` or a values file:
Helm persists release values in the cluster.

Provision GCS, object-event and silver Pub/Sub subscriptions, both DLQs and
scoped workload identities as described under [Upload ingestion](#upload-ingestion).

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
when a Secret changes; restart API and both worker Deployments after MongoDB
credential rotation. The MongoDB URI must specify
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
after explicit API and parser rollouts; use the staged procedure in [ENCRYPTION.md](ENCRYPTION.md).

Uninstall removes workloads, Services, Jobs, and the bundled dev root Secret, but
**retains MongoDB PVCs**. Bundled OpenBao state is lost.
Deleting the namespace or manually deleting those PVCs can destroy data. Use a stable
release name and namespace for the development database.

For externally managed dependencies, set their reachable addresses and runtime
credential path in `.env` and use `make run`, `make spool-parser` and `make spool-loader`
instead of `make dev`. `make dev` deliberately supplies `SPOOL_DEV_ENV` local
MinIO/emulator settings; the individual commands use the configured `.env`.
Kubernetes MongoDB DNS names remain
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

Install dependencies with `make install`. For SSR adapter/lint/browser checks
and the optional real browser/API/Mongo fixture, use
[frontend validation](../frontend/README.md#validation).
Deprecation/pending-deprecation warnings fail Python tests except the exact,
still-visible upstream Auth0/Authlib JOSE import warning.

```bash
make test
make lint
```

With Docker available, validate real NGINX forwarding, gzip/JWE body preservation,
wire limits and WebSocket upgrades:

```bash
PROXY_TEST_ENABLED=true .venv/bin/python -m pytest -q tests/test_proxy_integration.py
```

The proxy test selects native Linux networking when necessary and removes its
temporary container. [Upload service validation](#measuring-ingestion-latency)
covers real MinIO/emulator/Mongo separately.

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
Run the API, parser and loader with MongoDB, provisioned OpenBao and
MinIO/emulator/bridge first (`make dev` locally), or deploy Helm with provisioned
GCS/Pub/Sub/IAM. It exercises durable uploads and
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
# In another terminal, run make dev (API, both workers and dependencies).
make test-stack
```

`STACK_TEST_BASE_URL` defaults to **`http://localhost:8080`** and both local
credential-profile files use that proxy origin, not the backend's internal port.
Keep the API, parser and loader running throughout the test; `make dev-up` starts
only dependencies and is not sufficient. Override the base URL explicitly only
when intentionally testing another deployment.

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
Windows must be integer days from 1 through 24855 (Mongo's TTL seconds limit).
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
into time-series metadata. See the [document example](../PROJECT-BLUEPRINT.md#stored-document-layout).
HTTP upload events and analysis responses are flat; the storage layout is internal.

Startup creates `ingestion_received_at` on `ingestion.received_at` and
`ingestion_batch_record` on `(ingestion.batch_id, _id)`. Existing
`device_timestamp`, `timestamp_id` and both retention indexes keep their paths.
List projections exclude bodies and ciphertext. Inspect index usage before
changing indexes on the explicitly selected application database.
Deployment/startup never resets the database.

## Operational semantics

Use [API limits/read semantics](API.md) and [pipeline guarantees](INGESTION.md)
when interpreting errors and receipt state. In particular, `stored` can precede
secondary visibility, and analysis `503` can coexist with healthy ingestion.

`/health` is process liveness only. Monitor backlog/DLQs, both workers, raw cleanup,
primary/secondary availability, ingestion/read errors and OpenBao seal/auth/errors.
Fresh startup requires KMS; cached EC keys can remain usable in a running process
after token revocation. Follow [key rotation](ENCRYPTION.md#key-rotation) and
restart all API/parser instances.

## Worker operations

Use the [retry, backpressure and recovery](#retry-backpressure-and-recovery)
procedure for both Pub/Sub stages. Scale parser and loader independently;
multiply the per-loader document rate by replica count when budgeting Mongo load.
After forced stops, delivery deadlines and receipt leases recover work.
Never delete raw merely because silver was published.

## Troubleshooting
- For local MongoDB, inspect `make compose-ps` and
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
