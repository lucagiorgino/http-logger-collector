# GCP production deployment

This is an **independent deployment**, not a change to the local stack. All
production additions are under `infra/gcp/`; existing application sources,
development NGINX configuration, Compose files, images and Helm remain unchanged.
Use a dedicated, billing-enabled GCP project per production environment.

## Architecture and compatibility

```text
Devices / browsers
       |
Global external Application Load Balancer :443
Google-managed certificate / TLS >=1.2 / HTTP-to-HTTPS redirect
       | /api/*, /docs*, /redoc*, /openapi.json, /health
       +--> Cloud Run API :9090 ----> external durable Redis Streams
       |         |                             |
       |         +--> Atlas replica set <---- Cloud Run worker :9090
       |         +--> Secret Manager / Cloud KMS   (private, always allocated CPU)
       |
       +--> Cloud Run Next.js frontend :3000 (all other paths, including /auth/*)
                      |
                      +--> same public HTTPS origin for server-side API calls

All Cloud Run egress -> dedicated VPC subnet -> Cloud NAT -> static IP
Optional Memorystore Standard HA cache -> private services access
```

**Two important differences from a generic Cloud Run example:**

1. The frontend is **Next.js SSR**, not a static SPA. Keep its Auth0 client secret,
   session key and API access tokens server-side. Do not serve it as static files
   or inject secrets with `NEXT_PUBLIC_*`.
2. **Memorystore is not a drop-in ingestion queue.** The current collector requires
   Redis >=7.2, persistent AOF, `maxmemory-policy noeviction`, Lua and
   `WAITAOF 1 0 <timeout>` on the same connection as enqueue. Memorystore's
   snapshots/replication do not provide this AOF contract. The default deployment
   therefore uses a **separately provisioned compatible Redis service** supplied
   through Secret Manager, and refuses incompatible Redis during startup.
   Do not disable the durability check to make a managed service work.

This Terraform creates registry repositories, networking/NAT, secret containers,
key-scoped KMS IAM, three workload identities, Cloud Run services, serverless
NEGs, HTTPS load balancing, a baseline Cloud Armor policy and availability
monitoring. It configures connectivity to **existing Atlas and external Redis**;
it does not purchase/manage those vendors' clusters or credentials.

`enable_memorystore_cache=true` optionally provisions a private TLS/AUTH
`STANDARD_HA` Redis 7.2 **cache**. It is deliberately not wired into `REDIS_URL`.
It has no current application consumer. Leave it off unless you need that cache.
Memorystore AUTH/CA configuration must be used by any future cache client.
The computed AUTH credential also makes Terraform state sensitive when enabled.

### Serverless Redis options

Upstash and other serverless Redis-compatible services are possible future
integrations, **not verified compatible defaults**. Check Streams/consumer groups,
`XAUTOCLAIM`, Lua, batch-size limits, `INFO persistence`, `noeviction`,
`WAITAOF`, durability and failover behavior with the vendor before choosing one.
A replication acknowledgement or periodic snapshot is not a per-write fsync.
If those commands/guarantees are unavailable, a separately designed queue adapter
(for example Pub/Sub with its own replay and acknowledgement model) is required.
That change is not silently substituted here. A compatible external Redis queue
is a **mandatory prerequisite for deploying the existing ingestion behavior**.

### Cloud-only encryption adapter

Secret Manager distributes environment secrets and the EC key-set secret. It
cannot perform OpenBao Transit operations. The **cloud backend Dockerfile only**
overlays `runtime/openbao.py` onto the existing KMS interface inside the image:

- Google workload identity/ADC replaces OpenBao tokens. No service-account keys.
- The EC key set is fetched from a pinned Secret Manager version into RAM; no
  private key files, build arguments or Terraform secret values.
- Each protected log uses a random AES-256-GCM data key and 96-bit nonce. Cloud
  KMS wraps that key with the same authenticated record/device/batch context.
  Native async KMS calls are bounded to 16 concurrent operations per process;
  CPU preparation uses `asyncio.to_thread`. KMS failures fail closed.
- Stored data keys are encrypted; the non-exportable wrapping key stays in KMS.
  CRC32C is verified on Secret Manager and KMS responses.
- The existing document schema and `vault:v1:` parser prefix are preserved, but
  the payload is explicitly `gcp-kms-envelope-v1`, **not an OpenBao ciphertext**.
  Local and cloud ciphertext are **not interchangeable**.

Use a **fresh cloud database**. Migrating existing local/OpenBao-encrypted data,
or migrating back, requires an explicit decrypt/re-encrypt procedure with both
key authorities available; it is not performed here. Never delete old KMS key
versions or EC decryption keys while data/devices still depend on them.

The managed load balancer replaces production NGINX; an additional NGINX
container would duplicate routing. Local NGINX remains untouched. Paths are
preserved, never rewritten by stripping `/api`.

## 1. Tools, authentication and state

Install Terraform >=1.9, Google Cloud CLI, Docker with Linux amd64 build support,
Python >=3.11 and OpenSSL. Use a controlled provisioning identity with permissions
to enable services and administer the resources in this directory, including
project custom roles/IAM, workload service accounts, Secret Manager, KMS,
registry, Compute networking/load balancing, logging and monitoring.
Bootstrap additionally needs permission to create a private Storage bucket.
Do **not** grant those administrative roles to runtime identities.

For interactive deployment:

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

For CI, use Workload Identity Federation and a narrowly scoped deployer identity;
do not download long-lived JSON keys. `gcloud` credentials and Terraform ADC
are distinct; authenticate both for interactive use.

From the **repository root**, create the versioned private state bucket:

```bash
terraform -chdir=infra/gcp/bootstrap init
terraform -chdir=infra/gcp/bootstrap plan \
  -var="project_id=$PROJECT_ID" -var="region=$REGION" \
  -var="state_bucket_name=$STATE_BUCKET" -out=bootstrap.tfplan
terraform -chdir=infra/gcp/bootstrap apply bootstrap.tfplan
```

The bootstrap root initially uses local state (git-ignored). Move its state into
the bucket after creation, and keep a protected backup until migration succeeds:

```bash
cp infra/gcp/bootstrap/backend.tf.example infra/gcp/bootstrap/backend.tf
terraform -chdir=infra/gcp/bootstrap init -migrate-state \
  -backend-config="bucket=$STATE_BUCKET" \
  -backend-config="prefix=bootstrap"
```

The backend declaration is intentionally activated only after first creation.
Do not run both local and remote bootstrap states against the same bucket.
The application root already declares its GCS backend:

```bash
cp infra/gcp/terraform.tfvars.example infra/gcp/terraform.tfvars
# Edit the new terraform.tfvars: project, region, environment, hostname and Auth0.
# Leave deploy_services=false for the first apply. Never put secret VALUES here.

terraform -chdir=infra/gcp init \
  -backend-config="bucket=$STATE_BUCKET" \
  -backend-config="prefix=logger/$ENVIRONMENT"
terraform -chdir=infra/gcp fmt -check -recursive
terraform -chdir=infra/gcp validate
terraform -chdir=infra/gcp plan -out=foundation.tfplan
terraform -chdir=infra/gcp apply foundation.tfplan
```

Commit the provider lock files. State has locking and object version history;
limit bucket access to deployment operators/CI, never runtime service accounts.
Treat plans/state as sensitive even though user-supplied secret values are
provisioned out of band. Do not commit `.terraform/`, state, plans or tfvars.
Use separate backend prefixes, projects and secret sets for other environments.

## 2. Atlas, Redis, DNS and Auth0 prerequisites

Get the reserved public and egress IPs:

```bash
terraform -chdir=infra/gcp output load_balancer_ip
terraform -chdir=infra/gcp output egress_ip
terraform -chdir=infra/gcp output -json secret_names
```

**MongoDB Atlas:** provision a production dedicated replica set on GCP near the
chosen region, running MongoDB >=7.0 with healthy secondaries, backups and PITR.
Allowlist only the NAT egress IP `/32`, not `0.0.0.0/0`. Create a database user
scoped to `readWrite` on the selected application database (including the
collection/index creation needed at startup). Analysis requires secondaries;
primary-read fallback remains disabled in production.

Use a TLS-verified URI with the **actual replica-set name**:

```text
mongodb+srv://USER:URL_ENCODED_PASSWORD@CLUSTER.mongodb.net/?replicaSet=ACTUAL_SET_NAME&tls=true&retryWrites=true&w=majority&maxPoolSize=10&waitQueueTimeoutMS=5000
```

Encode username/password components correctly. Do not enable `directConnection`,
`tlsInsecure` or invalid-certificate/hostname overrides. Atlas SRV/TXT discovery
must work from Cloud Run. Firestore/Cloud SQL are not MongoDB-compatible
replacements for this schema, time-series TTL indexes and driver.
Private Atlas connectivity can replace public allowlisting, but requires
vendor/project-specific peering or Private Service Connect not created here.

**External Redis:** provision an authenticated, TLS-verified Redis >=7.2 endpoint
with durable AOF storage, `noeviction`, bounded memory, backups, an operational
recovery/failover plan and support for every command above. Allowlist the same
NAT IP `/32` or arrange private routing. Use a dedicated instance/ACL identity;
grant only the application stream prefix plus the required queue operations:
`INFO`, `EVAL`, `XLEN`, `XADD`, `WAITAOF`, `XGROUP`, `XREADGROUP`, `XAUTOCLAIM`,
`XACK`, `XDEL`, `MULTI`, `EXEC` and connection setup commands.

```text
rediss://QUEUE_USER:URL_ENCODED_PASSWORD@REDIS_HOST:TLS_PORT/0?ssl_check_hostname=true
```

Hostname verification is explicitly required by the cloud runtime. Use a
publicly trusted certificate chain. Vendor name alone is not evidence of
compatibility: startup performs the real AOF/command checks. `WAITAOF 1 0`
confirms local fsync, not replica fsync or zero-RPO automatic failover; verify
your chosen service's failure guarantees before promising them to clients.

**DNS:** create an A record for `domain` pointing at `load_balancer_ip`.
Remove conflicting AAAA records; this configuration reserves IPv4 only.
Do not put an unconfigured third-party proxy in front during certificate
validation. Certificates are created with the service deployment in step 5.

**Auth0:** use an existing RS256 API with the exact `auth0_audience`; grant the
frontend Regular Web Application user-delegated API access and the appropriate
operator permissions. Set these exact Web application URLs:

| Setting | Value |
| --- | --- |
| Callback | `https://logs.example.com/auth/callback` |
| Logout | `https://logs.example.com` |
| Web origin | `https://logs.example.com` |

Replace the example host with `domain`. Agents use separate M2M apps/grants;
do not reuse the frontend secret. The audience is an API identifier and may end
in `/api`; `BACKEND_API_URL` is the origin **without** `/api`. Both frontend URLs
are set to the public HTTPS origin so SSR requests also traverse the ALB.
Terraform does not alter the Auth0 tenant.

## 3. Populate Secret Manager without Terraform secret state

The first apply creates **empty secret containers**, not secret versions.
Populate these before creating Cloud Run. Run in Bash without shell tracing:

```bash
# Type each value at the hidden prompt; it does not become a shell history entry.
for NAME in mongo-uri redis-url auth0-client-secret; do
  read -r -s -p "Value for $NAME: " SECRET_VALUE
  printf '\n'
  printf '%s' "$SECRET_VALUE" | gcloud secrets versions add \
    "logger-${ENVIRONMENT}-${NAME}" --data-file=- --project="$PROJECT_ID"
  unset SECRET_VALUE
done

openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add \
  "logger-${ENVIRONMENT}-auth0-session" --data-file=- --project="$PROJECT_ID"

# Isolated admin tooling, not a change to the local application's virtualenv.
python3 -m venv infra/gcp/.venv
infra/gcp/.venv/bin/pip install -r requirements.txt -r infra/gcp/runtime/requirements.txt
infra/gcp/.venv/bin/python infra/gcp/runtime/provision_keyset.py \
  --secret="projects/$PROJECT_ID/secrets/logger-${ENVIRONMENT}-ec-keyset"
```

The provisioning tool prints only the new version resource name. Update
`secret_versions` to the resulting numeric versions (normally `1` on a fresh
environment). Do not use `latest`; rotation must produce a deliberate Cloud Run
revision. The admin tool generates a **new** key set on every invocation:
do not rerun it as a routine deploy step or discard historical decryption keys.
EC rotation must retain older keys in the key-set JSON until devices migrate.

Frontend reads only its two secret values. Worker reads only MongoDB/Redis.
Backend also reads the EC secret and has a custom three-permission role bound
to the **single storage KMS key**, not to the project. No workload can create
secrets, change IAM, rotate keys or access the other workloads' credentials.
Cloud Run's platform service agent pulls same-project Artifact Registry images;
workload identities need no registry administrator/reader role.

## 4. Build and push images

Use unique immutable tags for every release; both registry repositories reject
tag reuse. From the repository root:

```bash
export RELEASE="release-1"
export BACKEND_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/logger-${ENVIRONMENT}-backend"
export FRONTEND_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/logger-${ENVIRONMENT}-frontend"

gcloud auth configure-docker "${REGION}-docker.pkg.dev"
docker build --platform linux/amd64 -f infra/gcp/runtime/Dockerfile \
  -t "${BACKEND_REPO}/api:${RELEASE}" .
docker push "${BACKEND_REPO}/api:${RELEASE}"

# The existing frontend Dockerfile is used unchanged.
docker build --platform linux/amd64 -f docker/frontend.Dockerfile \
  -t "${FRONTEND_REPO}/dashboard:${RELEASE}" .
docker push "${FRONTEND_REPO}/dashboard:${RELEASE}"
```

The API and worker share the cloud backend image, with different startup
commands. Do not deploy the ordinary local backend image: it still expects
OpenBao and does not contain the cloud entrypoints.
No OpenBao server, development credentials or service-account JSON keys are
packaged into the cloud image.

## 5. Plan and apply the services

In `infra/gcp/terraform.tfvars`, set `deploy_services=true`, set both image tags
to the published release, and confirm the pinned secret versions:

```bash
terraform -chdir=infra/gcp plan -out=production.tfplan
# Review ingress restrictions, IAM, image tags and always-on worker resources.
terraform -chdir=infra/gcp apply production.tfplan
```

Worker replicas have instance-based CPU allocation (`cpu_idle=false`) and equal
nonzero minimum/maximum counts. They expose only private liveness HTTP; the
worker is **not** an on-demand Cloud Run Job, does not need user requests to
consume and does not scale to zero. A supervisor makes failed consumer tasks
fail liveness. SIGTERM/cancellation can interrupt writes; entries remain pending
for reclaim after 60 seconds. Delivery remains at-least-once with the existing
best-effort replay deduplication, not exactly-once.

Frontend/API permit `allUsers` invocation **only behind internal/load-balancer
ingress** because the ALB has no Google end-user identity. Sensitive API routes
still require Auth0 tokens/scopes. Worker has no public invoker. Organization
policies forbidding `allUsers` require a separately approved invocation policy;
do not work around them by enabling unrestricted ingress.

The ALB overwrites trusted client-IP/edge-IP headers; the cloud API entrypoint
ignores caller-supplied `X-Forwarded-For` and does not enable Uvicorn wildcard
proxy trust. HTTPS API requests missing the trusted edge headers fail closed.
Internal project/network principals are part of the trusted infrastructure
boundary; do not grant untrusted tenants access to that VPC/project.

The Google-managed certificate may take tens of minutes after DNS propagates:

```bash
gcloud compute ssl-certificates describe "logger-${ENVIRONMENT}" \
  --global --project="$PROJECT_ID" --format='value(managed.status)'
# Wait for ACTIVE, then:
curl --fail --show-error "https://logs.example.com/health"
curl --head "http://logs.example.com/"     # 308 to HTTPS
curl --head "https://logs.example.com/"    # frontend
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  "https://logs.example.com/api/v1/me"      # 401, not 400/404/502
```

Replace hostnames in smoke commands. Verify `/docs`, actual sign-in/callback,
workspace reads and a two-device plaintext/JWE upload through the shared domain.
Confirm persisted logs, encrypted body detail reads, cross-device denial and
queue drain. For the existing HTTP-only integration test, create a **separate
ignored test profile** with the cloud HTTPS origin, correct audience and two
dedicated M2M credentials/policies; do not rewrite the local profile.

Cloud Run startup itself validates MongoDB indexes, Redis durability and KMS/EC
configuration. A rollout that never becomes ready is a deployment failure,
not a reason to bypass those checks. Direct public `run.app` requests must be
rejected by ingress. Keep collector access through the ALB only.

## Operations, costs and limits

- This is a **single-region** application behind a global edge, not a multi-region
  database/queue failover solution. Minimum instances reduce cold starts but are
  not an uptime guarantee. Test regional outages and restore procedures.
- Two warm instances of each web service plus two continuously billed workers,
  ALB/Cloud Armor, NAT/static IPs, KMS calls, Atlas and external Redis cost money
  even at low traffic. Set billing budgets/alerts before deployment. Optional HA
  Memorystore adds cost even though the application does not use it.
- Configure `notification_channels` with existing Monitoring channel names.
  An HTTPS availability check/alert is included; without channels incidents have
  no outbound notification. `/health` is liveness, not end-to-end persistence.
  Also monitor Redis stream length/pending age/evictions/AOF errors, worker
  failures, Atlas replication/connection headroom, Cloud Run latency/5xx and
  KMS quota/latency using provider metrics and operational dashboards.
- Cloud Armor starts with an allow-only baseline. Add measured rate limits/WAF
  rules appropriate to device bursts; do not pretend that the baseline enforces
  attack-specific rules. CDN is disabled for Auth0/API responses.
- OAuth transaction URL logs are excluded from the default project log sink.
  Apply the same exclusion/redaction to any organization/folder/custom sinks;
  do not enable raw token/cookie/body logging. Restrict logs access and retention.
- Bound Cloud Run instance counts and MongoDB pool sizes together. The example
  URI bounds each pool to ten; account for each replica-set member and workload.
  Protected logs require one KMS key-wrap per event; benchmark quotas and costs.
- Google-managed KMS rotation retains old versions. Secret rotation requires
  adding a version, updating `secret_versions`, plan/apply and verifying every
  instance. Do not revoke the old EC set prematurely. Preserve KMS keys alongside
  MongoDB backups; a database backup without its keys is not a usable restore.
- For rollback, reference the previous immutable image tags and still-valid
  pinned secrets, review the plan, then apply. Do not destroy databases/keys.
  Removing deployed services requires explicitly disabling Cloud Run deletion
  protection first. Secret/registry/KMS/cache/state resources additionally use
  Terraform `prevent_destroy`; decommission only through a reviewed procedure.

## Local validation (no GCP credentials or resource creation)

```bash
terraform -chdir=infra/gcp init -backend=false
terraform -chdir=infra/gcp fmt -check -recursive
terraform -chdir=infra/gcp validate
terraform -chdir=infra/gcp test

infra/gcp/.venv/bin/python -m pytest -q infra/gcp/tests/runtime_checks.py
infra/gcp/.venv/bin/ruff check infra/gcp/runtime infra/gcp/tests
infra/gcp/.venv/bin/ruff format --check infra/gcp/runtime infra/gcp/tests
```

Terraform tests mock GCP and cover foundation-only, deployed workload policies
and optional HA cache. Python tests exercise real local AES-GCM with mocked
Google RPCs and the existing storage logic; they do not prove live GCP IAM,
Atlas/Redis vendor compatibility, DNS/certificates or Auth0 tenant grants.
Complete the smoke/integration checks above in a staging project before launch.

The explicit `runtime_checks.py` filename keeps cloud SDK tests out of the
existing local `make test` discovery; installing cloud SDKs is not required
for the unchanged local workflow.

References: [Cloud Run container contract](https://cloud.google.com/run/docs/container-contract),
[global load-balancer headers](https://cloud.google.com/load-balancing/docs/https/custom-headers-global),
[Memorystore configuration](https://cloud.google.com/memorystore/docs/redis/supported-redis-configurations),
[Redis WAITAOF](https://redis.io/docs/latest/commands/waitaof/),
[Secret Manager Cloud Run integration](https://cloud.google.com/run/docs/configuring/services/secrets).
