# GCP production deployment

Use a dedicated billing-enabled GCP project per environment. This is an independent
production deployment; local development still uses Docker dependencies and host
Python. Cloud-only KMS adapters remain under `infra/gcp/`.

## Deployment summary

Frontend and API use separate generated Cloud Run HTTPS URLs; workers stay
internal. No load balancer, purchased domain or custom certificate is required.
Keep the existing Auth0 tenant and API audience.

1. Prepare a billing-enabled GCP project, Terraform, Google Cloud CLI, Docker,
   Python and authenticated deployment credentials.
2. Bootstrap Terraform state, configure `terraform.tfvars`, then apply foundation
   resources with `deploy_services = false` (section 1).
3. Provision MongoDB Atlas with healthy secondaries and allowlist Terraform's
   `egress_ip` (section 2).
4. Run `make gcp-secrets-bootstrap` with the cloud venv, project and environment
   (section 3). It fills missing values and reports numeric secret versions to pin.
5. Read `terraform -chdir=infra/gcp output -raw public_url`. Register
   `<public_url>/auth/callback`, `<public_url>` as logout URL and web origin in
   Auth0. Keep the existing API audience and any required localhost entries.
6. Build and push both images (section 4), configure immutable image tags and
   secret versions, then set `deploy_services = true` and review/apply the plan
   (section 5).
7. Open `public_url`, configure agents with `backend_url`, and verify sign-in,
   the Status page and an ingestion round trip through Mongo storage/raw cleanup.

The sections below contain the commands and permission requirements. Atlas, NAT,
storage, messaging, KMS and running services still incur costs.

## Architecture

```text
Browsers -> Cloud Run Next.js frontend HTTPS URL
              -> server-side requests to Cloud Run API HTTPS URL
Devices  -> Cloud Run API HTTPS URL
Cloud Run provides TLS and HTTP-to-HTTPS redirect for both run.app origins.

POST /api/v1/traffic-log-uploads
  -> GCS immutable raw -> object-event Pub/Sub -> Cloud Run spool-parse
  -> silver Pub/Sub -> Cloud Run spool-load -> majority Atlas writes
  -> majority stored receipt -> delete raw -> ACK
Parser and loader each have a DLQ.

All Cloud Run egress -> dedicated VPC subnet -> Cloud NAT -> static IP
Frontend SSR -> backend HTTPS origin for API calls (no browser API tokens/CORS)
```

Terraform always provisions the private regional raw bucket, GCS notifications,
object/silver topics and pull subscriptions, both DLQs with inspection subscriptions,
scoped IAM, backlog/DLQ alerts and **four workload identities**:
backend, frontend, spool-parse and spool-load. `deploy_services` gates all four
Cloud Run services, not raw storage/Pub/Sub provisioning.
Atlas clusters and credentials are provisioned independently.

The management sidebar's **Status** page uses the shared `/api/v1/status` API.
Terraform grants the backend read-only raw-bucket and two-subscription metadata
access, not message consumption. Workers report per-instance health through
MongoDB, so this requires no public worker endpoints or Cloud Run control-plane
access. Deploy updated workers and apply IAM changes together. Probe failures
and missing reports are visible as unavailable/unknown; this snapshot does not
replace Pub/Sub backlog/DLQ alerts. See the
[status runbook](../../docs/RUNBOOK.md#pipeline-status).

### Ingestion contract and recovery

- POST returns `202` after durable raw receipt, before content validation.
  Optional device-scoped `X-Batch-Id` returns `200` for identical-byte/format
  retries and `409` for conflicting content.
- `GET /api/v1/traffic-log-uploads/{upload_id}` reports asynchronous parsing,
  Mongo storage and raw cleanup. Malformed JWE/gzip/JSON/events fail asynchronously.
- `app.spool_worker parse` decrypts, validates, normalizes and applies the
  acceptance-time workspace discard/encryption snapshot. Silver is gzip BSON,
  bounded to **9,500,000 wire bytes / 32 MiB expanded**.
- `app.spool_worker load` throttles Mongo writes per replica, uses atomic receipt
  leases and deletes raw only after majority Mongo persistence plus a majority
  `stored` receipt. Cleanup retries do not reload stored records.
- The parser has raw-read, object-subscription consume, silver/parse-DLQ publish,
  Mongo and Cloud KMS/Secret Manager access. The loader has raw-delete,
  silver-subscription consume, load-DLQ publish and Mongo, without EC/KMS access.
  Replay operators separately need raw-read, receipt-update and event-publish access.
- Both workers default to three application attempts; native Pub/Sub redrive
  defaults to five delivery attempts, the platform minimum. Failed DLQ publish
  leaves work unacknowledged. Application DLQs contain sanitized pointers;
  native load redrive can contain protected normalized documents, including fields
  intentionally plaintext by policy. Restrict access and never dump payloads.
- `spool_message_retention_seconds` defaults to 14 days. Notification expiry does
  not delete raw; replay retained raw after fixing capacity/dependencies.
  Pending/failed raw has **no age TTL, versioning or soft deletion**.
  This is a temporary replay source, not a historical data lake.
- Delivery is at least once, not exactly once: deterministic IDs/leases cannot
  eliminate every ambiguous Mongo time-series insert duplicate.
- Use `python -m app.spool_worker replay <upload_id>` after repairs, or
  `cleanup <upload_id>` for stored uploads with pending raw deletion. Inspect the
  receipt first; never replay active leases or drain DLQs before recovery.

`terraform output spool_environment` exposes nonsecret runtime settings.
Runtime uses ADC/workload identity, never downloaded service-account keys.
Production environment settings select GCS through Terraform/Helm; local
`.env.example` uses MinIO/emulator defaults. Do not carry local endpoint/emulator
overrides into production.
`spool_mongo_documents_per_second` is per loader instance; total capacity multiplies
by `worker_instances`. Worker supervision lives in `runtime/spool.py`.
Configure `notification_channels`; backlog defaults warn above 500 messages or
3600 seconds, below message retention. Monitor both DLQs and raw cleanup.
See [root README](../../README.md#core-ingestion-flow) and
[runbook](../../docs/RUNBOOK.md#upload-ingestion).

### Cloud-only encryption adapter

The cloud backend Dockerfile overlays `runtime/openbao.py` onto the KMS interface:

- Workload identity/ADC replaces OpenBao tokens.
- A pinned Secret Manager EC key-set version is loaded into RAM; no private key
  files, build arguments or Terraform secret values.
- Each protected record uses a random AES-256-GCM data key and 96-bit nonce;
  Cloud KMS wraps the key with authenticated record/device/batch context.
  Native async KMS is bounded to 16 concurrent operations per process;
  CPU preparation uses `asyncio.to_thread`. KMS failures fail closed.
- Stored data keys are encrypted; wrapping keys remain non-exportable in KMS.
  CRC32C is verified on Secret Manager/KMS responses.
- The document schema and `vault:v1:` prefix are retained, but the payload is
  `gcp-kms-envelope-v1`, **not OpenBao ciphertext**. Local/cloud ciphertext are
  not interchangeable. Use a fresh cloud database; cross-key-authority conversion
  is not performed. Retain keys needed by Mongo backups and all raw backlog.

The frontend is **Next.js SSR**, not a static SPA. Keep Auth0 client/session
secrets and API tokens server-side, never `NEXT_PUBLIC_*`.
The two services use distinct generated `run.app` origins, with no load balancer,
custom DNS, managed-certificate resources or Cloud Armor policy. API paths are
preserved, not stripped. `/api/v1/*`, `/docs`, `/redoc`, `/openapi.json` and
`/health` are on the **backend** URL, not the frontend URL.

## 1. Tools, authentication and state

Install Terraform >=1.9, Google Cloud CLI, Docker with Linux amd64 build support,
Python >=3.11. Provisioning identities need service enablement,
IAM/custom roles, service accounts, Secret Manager/KMS, registry, networking,
logging/monitoring and private state-bucket administration.
Do not grant administrative roles to runtime identities.

```bash
export PROJECT_ID="your-gcp-project"
export REGION="europe-west1"
export ENVIRONMENT="production"
export STATE_BUCKET="${PROJECT_ID}-logger-tfstate"
gcloud auth login
gcloud config set project "$PROJECT_ID"
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT_ID"
gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com \
  storage.googleapis.com --project="$PROJECT_ID"
```

CI uses Workload Identity Federation and a narrow deployer identity; do not
download JSON keys. `gcloud` credentials and Terraform ADC are distinct.
From the repository root:

```bash
terraform -chdir=infra/gcp/bootstrap init
terraform -chdir=infra/gcp/bootstrap plan \
  -var="project_id=$PROJECT_ID" -var="region=$REGION" \
  -var="state_bucket_name=$STATE_BUCKET" -out=bootstrap.tfplan
terraform -chdir=infra/gcp/bootstrap apply bootstrap.tfplan
cp infra/gcp/bootstrap/backend.tf.example infra/gcp/bootstrap/backend.tf
terraform -chdir=infra/gcp/bootstrap init -migrate-state \
  -backend-config="bucket=$STATE_BUCKET" -backend-config="prefix=bootstrap"

cp infra/gcp/terraform.tfvars.example infra/gcp/terraform.tfvars
# Set project, region, environment and Auth0. Keep deploy_services=false.
terraform -chdir=infra/gcp init \
  -backend-config="bucket=$STATE_BUCKET" -backend-config="prefix=logger/$ENVIRONMENT"
terraform -chdir=infra/gcp fmt -check -recursive
terraform -chdir=infra/gcp validate
terraform -chdir=infra/gcp plan -out=foundation.tfplan
terraform -chdir=infra/gcp apply foundation.tfplan
```

Keep a protected bootstrap-state backup until migration succeeds. Never operate
both local and remote states on the same bucket. Limit state access to deployment
operators/CI; state locking/version history does not make it public-safe.
Commit provider lock files, not `.terraform/`, state, plans, tfvars or secret values.
Use separate projects/backend prefixes/secret sets per environment.

## 2. Atlas, generated URLs and Auth0

### Choosing tfvars Auth0 settings

These settings are independent:

| Variable | What to supply |
| --- | --- |
| `auth0_domain` | Your existing Auth0 tenant or configured Auth0 custom hostname, without scheme/path; not the application's hostname. |
| `auth0_audience` | The **exact API Identifier** from Auth0 Dashboard > APIs, shared by frontend, backend and agents. It is not a routing URL and does not need to resolve. |
| `auth0_client_id` | The existing frontend Regular Web Application client ID, not an agent M2M client ID. |

Moving to GCP does not require changing the audience. Even an existing identifier
that looks like an old ngrok URL can stay unchanged without running ngrok.
Do not replace it with a generated Cloud Run URL or the new public hostname:
tokens must still match the API registered in Auth0.

There is **no application `domain` variable**. Terraform derives Cloud Run's
documented deterministic URLs from the service name, GCP **project number**
(not project ID) and region:
`https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app`.
Service names here stay within the platform's 63-character DNS-segment limit.
The URLs are available after the foundation apply, before containers exist,
so Auth0 callbacks can be configured without a deployment/self-URL cycle.

Keep credentials out of tfvars. The existing frontend client secret must be
uploaded to Secret Manager as described below. Register the new public callback,
logout and web-origin URLs on that same Auth0 application; retain localhost URLs
if the application is also used for local development.

```bash
terraform -chdir=infra/gcp output public_url
terraform -chdir=infra/gcp output backend_url
terraform -chdir=infra/gcp output egress_ip
terraform -chdir=infra/gcp output -json secret_names
```

Provision a MongoDB Atlas >=7.0 dedicated replica set near the selected GCP region,
with healthy secondaries, backups/PITR and a database-scoped `readWrite` user
including startup collection/index privileges. Allowlist only NAT egress `/32`,
not `0.0.0.0/0`. Private connectivity requires independently configured
peering/Private Service Connect.

Use a TLS-verified URI with the actual `replicaSet`, `retryWrites=true`,
`w=majority`, bounded `maxPoolSize` and `waitQueueTimeoutMS`. Encode credentials
correctly and verify SRV/TXT resolution from Cloud Run. Do not enable direct
connections or insecure certificate/hostname overrides. Production analysis
requires secondaries; primary fallback stays disabled. Firestore/Cloud SQL
cannot replace this Mongo schema/TTL/driver contract.

No DNS records, purchased domain or certificate provisioning are required.
`public_url` is the browser/frontend origin; `backend_url` is the API/agent origin.
Changing the region/environment/project changes these URLs and requires updating
Auth0 and agent configuration. Revisions keep the same service URLs.

When migrating an already-applied older configuration, remove `domain` from
your ignored tfvars and review the plan: it should remove the former edge
module/global ingress IP, not databases, raw objects, KMS keys or secrets.
Keep the separate **NAT egress IP**; Atlas still uses it. Switching origins also
means signing in again because browser sessions are hostname-scoped.

Use an existing Auth0 RS256 API with exact `auth0_audience`. Grant the frontend
Regular Web Application delegated API access and intended operator permissions.
For example:

| Setting | Value |
| --- | --- |
| Callback | `<public_url>/auth/callback` |
| Logout | `<public_url>` |
| Web origin | `<public_url>` |

Substitute the exact HTTPS `public_url` output, without angle brackets.
Keep the localhost entries too if reusing the same application for development.
Agents use separate M2M applications/grants,
never the frontend secret. Audience is an identifier and may end in `/api`;
`BACKEND_API_URL` is the backend HTTPS origin without `/api`. Terraform wires
frontend `APP_BASE_URL` to `public_url` and `BACKEND_API_URL` to `backend_url`.
SSR makes authenticated server-side API calls, so browser CORS access to the
backend is not needed. Terraform does not modify the Auth0 tenant.

## 3. Populate Secret Manager

Foundation creates empty secret containers, not versions. The administrative
bootstrap fills **only empty containers**, like local OpenBao initialization.
Run from the repository root, using ADC and the cloud dependencies:

```bash
python3 -m venv infra/gcp/.venv
infra/gcp/.venv/bin/pip install -r requirements.txt -r infra/gcp/runtime/requirements.txt
make gcp-secrets-bootstrap VENV=infra/gcp/.venv \
  PROJECT_ID="$PROJECT_ID" ENVIRONMENT="$ENVIRONMENT"
```

The command securely prompts for the **cloud Mongo URI** and existing frontend
Auth0 client secret only when missing. It generates a P-384 EC keyset and a
32-byte random session secret in memory only when those values are missing.
It never reads local `.env`/OpenBao credentials, exports existing secret values,
or modifies tfvars. Existing enabled versions are preserved, so rerunning after
partial failure initializes only the remaining values. A missing Terraform
container or disabled/destroyed newest version fails explicitly, not by replacing
keys or silently selecting an older version.

Progress includes resource/version names only. Standard output is a
`secret_versions = { ... }` block: review and copy its numeric versions into
`infra/gcp/terraform.tfvars`, never use `latest`. It reports the newest existing
enabled versions; do not change deliberately pinned older versions without
reviewing the intended deployment. Secret rotation remains an explicit operation
and needs a new Cloud Run revision. Retain historical EC keys while devices/raw
uploads depend on them.

For noninteractive use, supply private UTF-8 files instead of hidden prompts:

```bash
infra/gcp/.venv/bin/python -m infra.gcp.bootstrap_secrets \
  --project="$PROJECT_ID" --environment="$ENVIRONMENT" \
  --mongo-uri-file=/secure/path/mongo-uri \
  --auth0-client-secret-file=/secure/path/auth0-client-secret
```

Input files must have no group/other permissions (`chmod 600`); provision them
outside the repository. Do not put secret values in command arguments, shell
history, Terraform, build arguments or logs. Prompts refuse stdin that cannot
hide input. The bootstrap creates no private-key files.

The operator needs `secretmanager.secrets.get`, `secretmanager.versions.list`
and `secretmanager.versions.add` on the four Terraform-created secrets, plus
authenticated ADC. No secret payload read permission is needed. Do not grant
these administrative permissions to runtime identities. Run one bootstrap at a
time: Secret Manager version creation has no atomic create-if-empty operation,
so parallel initializations are not supported.

`runtime/provision_keyset.py` remains an explicit new-key provisioning tool; it
generates a fresh key on every invocation and is **not** the rerunnable bootstrap.
Terraform provisions the Cloud KMS wrapping key independently.

Frontend reads only its two secret values; loader reads Mongo only. Backend/parser
also receive EC-secret and key-scoped KMS permissions. Runtime identities cannot
change IAM, create secrets or rotate keys. The Cloud Run platform service agent
pulls same-project registry images; workloads need no registry administrator role.

## 4. Build and push images

Registry tags are immutable; choose unique release tags:

```bash
export RELEASE="release-1"
export BACKEND_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/logger-${ENVIRONMENT}-backend"
export FRONTEND_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/logger-${ENVIRONMENT}-frontend"
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
docker build --platform linux/amd64 -f infra/gcp/runtime/Dockerfile \
  -t "${BACKEND_REPO}/api:${RELEASE}" .
docker push "${BACKEND_REPO}/api:${RELEASE}"
docker build --platform linux/amd64 -f docker/frontend.Dockerfile \
  -t "${FRONTEND_REPO}/dashboard:${RELEASE}" .
docker push "${FRONTEND_REPO}/dashboard:${RELEASE}"
```

API/parser/loader share the cloud image with role-specific commands. The ordinary
local image expects OpenBao and has no cloud entrypoints. No development
credentials, OpenBao server or service-account keys belong in either image.

## 5. Deploy services

Set `deploy_services=true`, both immutable image tags and pinned secret versions:

```bash
terraform -chdir=infra/gcp plan -out=production.tfplan
terraform -chdir=infra/gcp apply production.tfplan
```

Review IAM, ingress and always-on worker resources. Workers use instance-based
CPU (`cpu_idle=false`) and equal nonzero min/max counts. They have private
liveness HTTP, no public invoker, and are not request-driven Jobs or scale-to-zero
consumers. Supervision fails liveness when consumer tasks fail.

API/frontend have `INGRESS_TRAFFIC_ALL` and `allUsers` invoker access so browsers
and agents can use their generated URLs without Google IAM tokens.
**This does not bypass Auth0**: protected API routes still validate access tokens
and permissions. Workers remain internal with no public invoker. If organization
policy forbids public Cloud Run invocation, this topology needs an approved
alternative; do not disable Auth0 checks.

Cloud Run terminates TLS and supplies forwarding headers. The cloud-only API
entrypoint requires HTTPS metadata and takes the final platform-appended
`X-Forwarded-For` address, never the caller-supplied prefix, `Forwarded` or old
custom edge headers. Malformed/missing API transport metadata fails closed;
platform liveness probes do not require it. Uvicorn forwarding-header handling
remains disabled. Do not run this cloud entrypoint behind arbitrary additional
proxies or outside Cloud Run without revisiting this trust boundary.
The API wrapper and frontend supply HSTS directly now that no edge adds it.

```bash
export FRONTEND_URL="$(terraform -chdir=infra/gcp output -raw public_url)"
export BACKEND_URL="$(terraform -chdir=infra/gcp output -raw backend_url)"
curl --fail --show-error "$BACKEND_URL/health"
curl --head "$FRONTEND_URL/"              # frontend
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  "$BACKEND_URL/api/v1/me"                 # 401 without Auth0 credentials
```

Verify docs, sign-in/callback, workspace reads, two-device gzip/JWE uploads,
asynchronous failures, `stored`/`raw_deleted`, protected detail reads and
cross-device denial. Use a separate ignored cloud stack-test profile; do not
rewrite local credentials. Set that profile's `STACK_TEST_BASE_URL` to
`backend_url` (not `public_url`) and use its separate M2M grants. Startup failures
in Mongo indexes or role-specific KMS/EC configuration are deployment failures,
not checks to bypass.

## Operations, costs and validation

- This is a single-region deployment, not regional failover. Test restore.
- Warm web instances, continuously billed workers, NAT/static IPs,
  GCS/Pub/Sub, KMS and Atlas cost money even at low traffic; configure budgets.
- Set Monitoring notification channels. Health checks are liveness, not end-to-end
  persistence. Monitor both subscription backlog depths/ages and DLQs, raw
  cleanup, worker errors, Mongo replication/pools, API 5xx and KMS quotas/latency.
- No Cloud Armor/WAF or edge rate limiting is provisioned. Keep bounded upload
  sizes, worker rates and instance limits; assess abuse protection before
  exposing production workloads. Frontend and sensitive API responses stay uncached.
- OAuth transaction URL logs are excluded from the default sink; apply matching
  exclusions to organization/custom sinks. Never log raw tokens/cookies/bodies.
- Bound worker rates, instance counts and Mongo pools together. Each protected
  record requires KMS wrapping; measure cost/quotas rather than assume speedup.
- KMS rotation retains old versions. Preserve all required EC/storage keys and
  Mongo backups; database backups without keys are unusable.
- Roll back using previous immutable images and still-valid pinned secrets.
  Do not destroy databases/keys. Decommissioning requires reviewed deletion-
  protection/`prevent_destroy` changes.

No-credentials validation:

```bash
terraform -chdir=infra/gcp init -backend=false
terraform -chdir=infra/gcp fmt -check -recursive
terraform -chdir=infra/gcp validate
terraform -chdir=infra/gcp test
infra/gcp/.venv/bin/python -m pytest -q infra/gcp/tests/runtime_checks.py infra/gcp/tests/bootstrap_checks.py
infra/gcp/.venv/bin/ruff check infra/gcp/bootstrap_secrets.py infra/gcp/runtime infra/gcp/tests
infra/gcp/.venv/bin/ruff format --check infra/gcp/bootstrap_secrets.py infra/gcp/runtime infra/gcp/tests
```

Mock Terraform/Google RPC tests do not prove live IAM, GCS event latency,
Atlas connectivity, platform forwarding headers or Auth0 grants. Complete staging smoke tests.
The cloud `*_checks.py` suites stay outside local test discovery; storage/Pub/Sub SDKs are
root dependencies, while KMS/Secret Manager SDKs stay in cloud requirements.

References: [Cloud Run contract](https://cloud.google.com/run/docs/container-contract),
[Cloud Run HTTPS URLs](https://cloud.google.com/run/docs/triggering/https-request#service_url),
[Secret Manager integration](https://cloud.google.com/run/docs/configuring/services/secrets).
