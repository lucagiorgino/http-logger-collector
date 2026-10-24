# Runbook

## Prerequisites
- Python 3.11+
- Docker and Docker Compose for development MongoDB and OpenBao
- For Kubernetes deployment only: Kubernetes 1.27+, kubectl, Helm 3 or 4, and a
  StorageClass when using bundled MongoDB

For HTTPS proxy configuration, device encryption policies, OpenBao provisioning, and
rotation, see [Log encryption and device configuration](ENCRYPTION.md).

## Local setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run the live development backend

```bash
make dev
```

Compose starts one MongoDB container as a one-member `rs0` replica set. Its
healthcheck initializes it and waits for primary readiness. The backend runs in
the host virtual environment through `python -m app` with Uvicorn reload.
Python code changes do not require a Docker build, Helm, or Kubernetes.

Compose also starts OpenBao and bootstraps its Transit KEK, wrapped EC key set,
and a least-privilege runtime credential. `make dev` waits for both dependencies,
then starts Python. All components read
the same `.env` file, preserving Auth0 settings. The default API address is
`127.0.0.1:8001`; use `APP_PORT` in `.env` or `make dev APP_PORT=8101` to change it.

To run the components independently:

```bash
make dev-up
make run
```

`make run` uses the MongoDB URI in `.env`, which interpolates `MONGO_HOST`,
`MONGO_PORT`, and `MONGO_REPLICA_SET`, normally resolving to
`mongodb://localhost:28017/?replicaSet=rs0`. MongoDB publishes only port 28017 on
loopback and stores data in one named volume. No hosts-file edits, shared network
namespaces, or extra database services are needed. OpenBao uses loopback port
28200; Python loads `OPENBAO_ADDR` and `OPENBAO_TOKEN_FILE` from the same `.env`.
The generated `.secrets/` credentials are private and ignored by Git and Docker builds.
The application never receives the development root credential.

The local profile sets `MONGO_ALLOW_PRIMARY_READS=true`, explicitly reading analysis
from its only member. This setting is allowed only with `APP_ENV=development`;
enabling it in another environment fails startup. Deployments continue to read
strictly from secondaries, without primary fallback.

Stop Python with Ctrl+C. `make dev-down` stops both services and retains MongoDB
volumes. Individual targets are `mongo-up/down` and `openbao-up/down`. Use the same
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
| Host Python | 8001 | `.env` `APP_HOST` / `APP_PORT` |
| Kubernetes API pod | 8000 | Helm `app.port` |
| Kubernetes MongoDB pods | 27017 | Helm `mongodb.port` |
| Kubernetes OpenBao pod | 8200 | Helm `openbao.port` |
| Kubernetes API port-forward | 18000 | Make `KUBE_LOCAL_PORT` |

Kubernetes uses ClusterIP Services, not host ports. Its port-forward defaults to
a separate local port. These defaults avoid overlap with one another, but cannot
reserve ports against unrelated software; change occupied ports explicitly.
Keep the same MongoDB port and replica-set name when reusing its development volume.
Changing `.env` requires a Python restart.

## Optional local Kubernetes deployment
```bash
minikube start
make image
docker pull mongo:7
docker pull ghcr.io/openbao/openbao:2.7.1
minikube image load amazing-http-logger:dev mongo:7 ghcr.io/openbao/openbao:2.7.1
make helm-lint
make helm-up KUBE_CONTEXT=minikube
make helm-test KUBE_CONTEXT=minikube
kubectl --context=minikube -n logger get pods,jobs,pvc
make helm-port-forward KUBE_CONTEXT=minikube
```

Check `kubectl config current-context` before deployment. Make targets accept
`KUBE_CONTEXT`, `KUBE_NAMESPACE`, `HELM_RELEASE`, `HELM_VALUES`, `HELM_ARGS`,
`IMAGE_REPOSITORY`, `IMAGE_TAG`, `KUBE_SERVICE`, and `KUBE_LOCAL_PORT`. Their defaults select the `logger`
release/namespace and the **development** Helm profile; the chart's own default
values select **external** MongoDB and HTTPS OpenBao.

The development profile has placeholder Auth0 settings. Set a real tenant and
audience with `HELM_ARGS='--set auth0.domain=your-tenant.auth0.com --set auth0.audience=https://your-api'`.
Do not put access tokens, database passwords, or client secrets in Helm values.

MongoDB starts as a three-member StatefulSet with one PVC per member, a headless
Service, and stable in-cluster DNS names. An idempotent Job initializes `rs0` or
validates its existing membership, then waits for a primary and two secondaries.
The API init containers wait for primary connectivity and OpenBao key provisioning
before application startup.
The revision-named Job is an ordinary resource, not a post-install hook, so
`helm --wait --wait-for-jobs` cannot deadlock waiting for that hook to run.

MongoDB is not externally exposed. Its ingress NetworkPolicy permits only the
release's API, MongoDB, and initializer pods; enforcement requires a CNI that
supports NetworkPolicy. The bundled database has no authentication or TLS and is
for development only. Do not use it for production data.

OpenBao runs as a non-root development Deployment with a ClusterIP Service and an
ingress NetworkPolicy permitting only API/bootstrap pods. A generated development
root Secret is shared only with that server and a revision-named bootstrap Job.
The Job provisions Transit, KV v2, keys, policy, and Kubernetes authentication.
OpenBao's ServiceAccount gets `system:auth-delegator` for TokenReview. API pods use
their own dedicated ServiceAccount and an audience-bound JWT, never the root token.
Installing the bundled profile therefore requires ClusterRoleBinding privileges.

The bundled KMS is ephemeral. After an OpenBao pod restart, run `make helm-up`
to bootstrap again, then restart the API Deployment to discard its old cached
keys. This cannot recover previously buffered encrypted envelopes. The chart
does not implement persistent production OpenBao or automatic unsealing.

## External MongoDB and OpenBao / production

Provision an authenticated, TLS-enabled MongoDB replica set independently. Configure
the real URI in the git-ignored `.env`, or use your cluster's secret-management
operator to provision the Secret. Never pass the URI in `--set` or a values file:
Helm persists release values in the cluster.

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
docker build -f docker/Dockerfile -t registry.example.com/team/amazing-http-logger:0.1.0 .
docker push registry.example.com/team/amazing-http-logger:0.1.0
helm upgrade --install logger helm/amazing-http-logger \
  --kube-context YOUR_CONTEXT --namespace logger \
  --set externalMongo.existingSecret=logger-mongodb \
  --set openbao.address=https://bao.example.com \
  --set auth0.domain=your-tenant.auth0.com \
  --set auth0.audience=https://your-api \
  --set image.repository=registry.example.com/team/amazing-http-logger \
  --set-string image.tag=0.1.0 \
  --wait --wait-for-jobs --timeout 10m
```

The Secret must be in the release namespace and contain `MONGO_URI` (or the key
selected by `externalMongo.uriKey`). Kubernetes environment variables do not reload
when a Secret changes; restart the Deployment after rotation. The URI must specify
`replicaSet=<name>`, plus authentication/TLS options, and every advertised member
hostname must be resolvable and reachable from the pods. `directConnection=true`
is rejected.

For private image registries, configure `imagePullSecrets`. Production exposure
requires a separately managed TLS ingress/gateway to the ClusterIP Service;
configure it to accept the documented compressed batch size without stripping
`Content-Encoding`. Configure database backups, access controls, and monitoring
outside this chart.

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
**retains MongoDB PVCs**. Bundled OpenBao state is lost.
Deleting the namespace or manually deleting those PVCs can destroy data. Use a stable
release name and namespace for the development database.

For externally managed dependencies, set their reachable addresses and runtime
credential path in `.env` and use `make run` instead of `make dev`. Kubernetes MongoDB DNS names remain
cluster-internal; forwarding a single member port does not preserve replica discovery.

## Run tests
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
creates a unique `traffic_test_*` database and drops it afterward. Do not point tests
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
Run the backend with MongoDB and a provisioned OpenBao first (`make dev` locally,
or deploy Helm). It exercises the currently configured ingestion publisher and
reads accepted records through analysis, waiting for replication/processing.
Deployment analysis must have a readable secondary; the local single-node profile
uses its existing explicit development-only primary-read setting.

Create **two different Auth0 Machine-to-Machine applications**, authorize both for
the backend API audience, and enable the `client_credentials` grant. Neither
application needs `manage:devices`: the workers act only as agents and never
PATCH a device policy. These test-only credentials should not be deployed to
ordinary agents.

Each client authenticates as its own owner (typically `<client-id>@clients`), not
as an interactive human user. The test obtains the verified subject from `/api/v1/me`
instead of decoding an unverified token or assuming the subject format.

Configure device policies before the test. The stable IDs default to
`stack-test-plaintext` and `stack-test-encrypted`, or can be set with
`STACK_TEST_PLAINTEXT_DEVICE_ID` and `STACK_TEST_ENCRYPTED_DEVICE_ID`. The plaintext
device must use `tls_only`; the encrypted device must
allow JWE (`optional` or `required`). An incompatible policy fails with a setup
message instead of being changed automatically.

Policy management remains a frontend/operator responsibility, subject to the
existing backend ownership rules. **The current frontend cannot manage an M2M
owner's devices from a different human subject.** That requires an explicit
delegation model; the test does not bypass this restriction or provision it.

```bash
cp .env.stack-test.example .env.stack-test
chmod 600 .env.stack-test
# Edit .env.stack-test locally: set the backend URL, Auth0 token URL/audience,
# and the separate plaintext/encrypted client IDs, secrets and stable device IDs.
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
Environment variables override profile values. Both the two client IDs and the
two device IDs must differ.

Two worker threads independently obtain fresh bearer tokens, discover their
identities, and read their existing device configurations without changing them.
Both acknowledge the advertised policy version; the encrypted device reports the
active key. It sends JSON -> gzip -> compact JWE using only the advertised suite
and public key. If its existing policy is `required`, the test also verifies
that a gzip-only upload is rejected. The plaintext device sends gzip JSON. Both
submit three synthetic logs with unique per-run URLs.

The test waits for records from each acknowledged batch, following analysis
pagination as necessary. Older batches on the same stable device do not affect
the counts. It verifies receipt IDs, ownership, redaction and body minimization,
then checks cross-owner list/item isolation. Policies are never changed or reset.

`STACK_TEST_HTTP_TIMEOUT` bounds each request (default 10 seconds);
`STACK_TEST_VISIBILITY_TIMEOUT` bounds the read-back wait (default 30 seconds).
Increase the latter for slower replicas or a queued publisher. A `503` or any other
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
updates status for the configured stable devices**; the API
does not expose deletion. It does not modify existing device policies; do not
target production. It needs no MongoDB/OpenBao administrator credentials. Unit tests for this
client harness use an explicit mock transport; they do not claim a live Auth0 run.

## Storage initialization

Startup creates `traffic_logs` with `timeField=timestamp`, `metaField=metadata`, and
`granularity=seconds`, and checks that an existing collection has compatible options.
It fails rather than using a regular collection with the same name. Index creation
and collection administration happen against primary; only analysis reads use replicas.

## Operational semantics

- Batch uploads require one gzip member and `application/json`. Limits are 500 records,
  1 MiB compressed, and 8 MiB decompressed; size violations return `413`.
- `202` acknowledges durable handoff. In the MVP the receipt says `stored`; with a
  future broker adapter it can say `queued`, which does not mean processed.
- Ingestion failures return `503`. Bulk writes may have partially succeeded; client
  retries are not deduplicated. Use bounded retry policies appropriate to that limitation.
- Replica reads are eventually consistent. New records may briefly be missing; `404`
  does not prove that a just-accepted record was never written.
- No secondary means analysis returns `503`, even while ingestion remains healthy.
  The explicit local-development primary-read profile does not require a secondary.
- `/health` reports process liveness only; monitor primary/secondary availability
  and ingestion/read error rates separately, along with OpenBao seal/auth/error metrics.
- Fresh startup requires OpenBao. EC keys stay cached in RAM for the process lifetime;
  a running backend may continue decrypting during a KMS outage. Revoking a KMS token
  does not remove cached keys. Restart all processes after EC rotation/retirement.

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
