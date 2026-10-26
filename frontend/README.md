<!-- Purpose: Explain management console setup, Auth0 configuration, SSR screens and forms, API boundaries, troubleshooting, and validation. -->

# amazing-http-logger management console

An SSR management dashboard in **plain JavaScript/JSX**, built with Next.js App
Router, React, Tailwind CSS, and the JavaScript shadcn/ui registry components.
The Python backend remains the sole authority for device access, permissions,
persistence, and encryption policy.

For a visual review, see [identity/session](../docs/IMPLEMENTATION_STATUS.md#identity-and-sessions)
and [SSR read/form](../docs/IMPLEMENTATION_STATUS.md#console-reads-and-forms) sequences,
the [operation index](../docs/IMPLEMENTATION_STATUS.md#operation-index), and
[component/deployment diagrams](../docs/IMPLEMENTATION_STATUS.md#architecture-diagrams).

## Run locally

Use Node.js 22.13+ LTS or 24+ LTS, npm, and a running backend
(`make local-native-run` in a separate terminal).

From the repository root:

```bash
make frontend-install
cp frontend/.env.example frontend/.env.local
openssl rand -hex 32
# Set the generated value as AUTH0_SECRET in frontend/.env.local.
# Fill in the other Auth0 settings described below.
make local-frontend-dev
```

`make local-native-run` starts NGINX; open **http://localhost:8080**.
Without complete Auth0 configuration, the welcome
page displays setup instructions instead of pretending to be authenticated.
There is no mock/demo bypass in the application.

The frontend has its own environment file; it does not read the backend's `.env`.
Its listener is **3000** in development and production; Python uses **9090** in both.
NGINX exposes the shared development origin on **8080**. Change the listener with
`make local-frontend-dev FRONTEND_PORT=3010` (or `PORT=3010 npm --prefix frontend run dev`)
and update the proxy's upstream port. Change `APP_BASE_URL` and Auth0 URLs together
only if the browser-facing origin changes. `PORT` belongs in the process
environment, not Next.js's `.env.local`.

`BACKEND_API_URL` is reached by the Next.js **server**, not the browser. When both
processes run on the development host, use `http://127.0.0.1:9090`, even if
the browser reaches the frontend through a public HTTPS reverse proxy. To test
through a public backend tunnel instead, set it to that tunnel's HTTPS origin.
Use the origin only, without `/api` or `/api/v1`: request paths already contain
`/api/v1`. Adding a prefix produces incorrect paths such as `/api/api/v1/devices`.
The public frontend origin belongs in `APP_BASE_URL`; it does not determine the
API address.

### Containerized local mode

After configuring the same Auth0 values, `make local-containers-up` starts the
frontend, collector and both workers in dedicated Compose containers. A host Node
installation is unnecessary for this mode. Runtime variables come from
`frontend/.env` followed by `frontend/.env.local` (or `FRONTEND_ENV_FILE`).
Compose sets `APP_ENV=development` and overrides the server-side API origin to
`http://collector:9090`; only that exact Docker hostname is allowed as an
additional non-loopback HTTP origin in explicit development. HTTPS remains
required for other remote origins and all production configurations.

The public origin stays `http://localhost:8080`, so Auth0 callbacks/grants do not
change between modes. Stop the native frontend/collector before binding their
ports with containers. Use `make local-apps-stop` to return to native mode without
restarting OpenBao. See the [profile guide](../docs/RUNBOOK.md#run-the-live-development-stack).

For proxy routing, trusted HTTPS tunnels and native Linux's
`PROXY_SERVICE=proxy-host`, see [local stack setup](../docs/RUNBOOK.md#run-the-live-development-stack)
and [ports](../docs/RUNBOOK.md#environment-profiles-and-ports).

If the UI reports **HTTP 502** while the local backend works, check
`BACKEND_API_URL` from the frontend host. A proxy can return 502 HTML before the
request ever reaches FastAPI. Check `/health` at that configured address and at
the local API; direct unauthenticated `/api/v1/traffic-logs` requests should return
401, not a gateway error. Check the proxy's target against the actual listener:
HTTP versus HTTPS, host, port and IPv4/IPv6 resolution.
Fix the proxy's upstream or use the reachable internal API address, then restart
the frontend after changing environment settings.

On GCP, Terraform supplies distinct Cloud Run HTTPS origins: `APP_BASE_URL` is
the frontend `public_url`, while `BACKEND_API_URL` is `backend_url`, without an
API path suffix. Register `<public_url>/auth/callback`, logout and web-origin URLs
in Auth0; do not change the existing API audience to either generated URL.
No browser-to-backend CORS or load balancer is needed for server-side API calls.
See the [GCP deployment guide](../infra/gcp/README.md).

## Auth0 configuration

Create a **Regular Web Application** dedicated to this management frontend.
Do not reuse an agent's machine-to-machine client secret.

For the defaults, register:

| Auth0 setting         | Value                                 |
| --------------------- | ------------------------------------- |
| Allowed Callback URLs | `http://localhost:8080/auth/callback` |
| Allowed Logout URLs   | `http://localhost:8080`               |
| Allowed Web Origins   | `http://localhost:8080`               |

Set `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_SECRET`,
`APP_BASE_URL`, and `AUTH0_AUDIENCE` in `frontend/.env.local`. The audience must be
the backend API's identifier, **not** the Auth0 Management API audience.
`AUTH0_DOMAIN` should match the backend's tenant.

### Sign-in troubleshooting

If the callback displays **"An error occurred during the authorization flow."**,
inspect the `error` and `error_description` parameters in the callback URL locally.
Do not share the full URL: it also contains authorization transaction data.

An Auth0 **"Service not found"** error means the requested audience does not
identify an API in the configured tenant. Copy the backend's exact
`AUTH0_AUDIENCE` into the frontend environment file; a sample value, application
URL, or client ID is not a substitute. The frontend and backend must use the same
tenant and API identifier.

If the callback reports **`Client ... is not authorized to access resource server ...`**,
the frontend application lacks user-delegated access to that API. In Auth0
**Applications > APIs**, select the API matching `AUTH0_AUDIENCE`. Under
**Settings > Application Access Policy**, use **Per-app authorization** for
**User-Delegated Access**. Under **Application Access > Edit**, grant the frontend
Regular Web Application **User-Delegated Access**, including the requested
`manage:devices` permission for operators, and save. Do not substitute a
**Client Access** machine-to-machine grant or enable all applications.
See [Auth0 API access policies](https://auth0.com/docs/get-started/apis/api-access-policies-for-applications).

After changing environment settings, restart `make local-frontend-dev` and begin a
fresh sign-in from the configured `APP_BASE_URL` rather than reloading an old
callback URL. That origin's `/auth/callback` must be in the Web application's
Allowed Callback URLs. For direct local development, use `http://localhost:3000`
consistently; for an HTTPS reverse proxy, use its actual public origin.

### Login succeeds but workspace requests return 401

The Web application session and the backend API access token are separate.
Verify that the frontend/backend tenant and `AUTH0_AUDIENCE` match, and that
the **API** issues **RS256** access tokens. The backend's Auth0 SDK accepts
RS256 only. An HS256 token is rejected even with the correct issuer, audience
and expiration; repeatedly signing in cannot repair that mismatch.

`AUTH0_AUDIENCE` is an exact API identifier, not a request base URL. For example,
it may end in `/api` even though `BACKEND_API_URL` must be an origin without
that suffix. Changing SSR requests from loopback to the public HTTPS proxy does
not fix a token with the wrong audience. After correcting the audience, sign out
and sign in again: the existing session still contains the old access token.

Open Auth0 **Applications > APIs**, select the API whose **Identifier** equals
`AUTH0_AUDIENCE`, and check its **JWT Signing Algorithm** under **Token Settings**.
It must be `RS256`. If the Dashboard setting is read-only, a tenant administrator
can update the existing API through the Management API with
`PATCH /api/v2/resource-servers/{API_ID}` and body `{"signing_alg":"RS256"}`.
That requires `update:resource_servers`; ordinary frontend/agent credentials
are not tenant-administration credentials. Keep the API identifier unchanged.
Coordinate the change with any other consumers of that API.

After the provider change, sign out and sign in again to obtain fresh tokens;
already issued HS256 tokens do not change. This setting concerns Auth0 access-token
signatures, not the application's EC/JWE upload encryption. Do not disable
signature validation, accept the token's advertised algorithm automatically, or
put an API signing secret in frontend configuration.

### Sign-out troubleshooting

An Auth0-hosted **"Oops!, something went wrong"** page during sign-out can mean
the return URL is missing from **Allowed Logout URLs**. The technical details
report `invalid_request` and identify `returnTo` or `post_logout_redirect_uri`.

In Auth0 Dashboard, open **Applications > Applications**, select the dedicated
frontend Web application matching `AUTH0_CLIENT_ID`, and open **Settings**.
Add the **exact `APP_BASE_URL` value** from the frontend environment file to
**Allowed Logout URLs**, preserving any other valid entries, and save.
For a public proxy this is the **frontend** HTTPS URL, not the backend URL,
`/auth/callback`, or `/auth/logout`. Callback URLs and logout URLs are separate
allowlists; successful sign-in does not establish that sign-out is configured.

The SDK clears the local session cookie before redirecting to Auth0. A provider
error can still leave its SSO session active. Both OIDC `/oidc/logout` and
`/v2/logout` enforce the return-URL allowlist, so changing the logout strategy
does not repair a missing entry. After saving the provider setting, sign in again
and retry sign-out; no frontend restart is needed unless its environment changed.

### Management permissions

Define the backend API permission `manage:devices` in Auth0 and grant it only to
intended management users (for example with an Auth0 role and API RBAC/permissions
claims). The default `AUTH0_SCOPE` requests `openid profile email manage:devices`.
Tokens without that grant can view only their own JWT-bound device and logs.
The grant also allows fleet-wide reads; it is not tenant-scoped.
The API independently checks the
verified token's `scope` or `permissions` when a device is registered or a policy is saved. The UI does not
manufacture permissions or issue tokens.

Interactive login uses the official `@auth0/nextjs-auth0` SDK. Its encrypted,
HTTP-only session cookie holds the session; API tokens never enter browser local
storage, client component props, or a browser token endpoint. The SDK's
`/auth/access-token` route is disabled. Refresh persistence occurs in Next.js's
`proxy.js` before Server Components read the access token. This implements
server-side access, not a browser SPA making credential-bearing API calls.

## Screens and API boundaries

| Route                    | Purpose                                                                                                          |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `/`                      | Welcome/sign-in or configuration guidance                                                                        |
| `/overview`              | Latest-50-record sample, HTTP response distribution, recent traffic                                              |
| `/logs`                  | Device/UTC time filters, cursor pagination, redacted request metadata                                            |
| `/logs/:id`              | HTTP event, captured endpoints/ports, process, sizes/timing, redacted headers, retained body and digest          |
| `/devices`               | API-backed, paginated stored-device inventory and named registration with initial policy                         |
| `/devices/:id`           | Device identity, policy editing, agent acknowledgement, public encryption suite and traffic link                 |
| `/devices?device_id=...` | Exact policy lookup; redirects to the detail page's explicit configuration view                                  |
| `/settings`              | Application-wide storage and encryption, ordinary/suspicious retention, API-verified identity and access context |
| `/status`                | Eight-stage pipeline diagram; shared local/GCP health snapshot, manual refresh; requires `manage:devices`        |

Pages render their API data on the server with `cache: no-store`. Read views
work without browser JavaScript; client JavaScript enhances navigation and the
save button's pending state. Policy forms use authenticated Server Actions with
Next.js's same-origin protections. Policy edits carry `expected_version`; conflicts
reload the current policy with an explicit error instead of overwriting it.
Timeouts, permission denials, API outages, and empty results are different UI states.

**Devices:** register the ID returned by the agent's `/me`, then use its detail
page for later edits/acknowledgements. Inventory uses **Unnamed device** and
**Agent / policy record** labels without inventing registration data.
Duplicate registration shows **Device already exists**, never an updated policy.
Exact policy lookup does not create an inventory entry. Encoded IDs, including
`@clients` and literal percent characters, preserve identity through navigation.
See [registration and authorization contracts](../docs/API.md#register-a-device-and-configure-its-policy).

**Workspace:** storage choices warn about discard/plaintext; retention warns
about irreversible expiration. Both sections save together with one expected
version. Detail views escape retained bodies; credentials stay redacted.
The adapter supports flat historical and current API records without accessing
Mongo internals. See [workspace API](../docs/API.md#workspace-configuration) and
[retention failure recovery](../docs/RUNBOOK.md#retention-operations).

**Interpretation:** overview counts are samples, not fleet aggregation; a saved
policy is not proof an agent applied it. Status metadata/heartbeats are not
native queue depth or end-to-end guarantees; see [probe semantics](../docs/RUNBOOK.md#pipeline-status).
There is no raw-upload or DLQ management screen. The [pipeline design](../docs/INGESTION.md)
owns raw privacy, asynchronous processing and delivery guarantees.

## Validation

```bash
make frontend-test          # API adapter/input tests and ESLint
make frontend-build        # production SSR compilation
cd frontend && npx playwright install chromium && cd ..
make frontend-test-browser # build + browser suite
```

Browser tests start a production Next.js server on loopback **3301** and a mock
HTTP backend on **38002**, both stopped by Playwright afterward. They use the
Auth0 SDK's public `generateSessionCookie` test helper and fixed **test-only**
credentials. No real Auth0 tenant, production credentials, or database is used.
They cover authenticated SSR with JavaScript disabled, traffic navigation,
stored-device detail navigation, encoded IDs, permission errors, concurrent policy conflicts, mobile overflow, and the disabled
token endpoint. These tests do **not** claim to validate a live Auth0 login.

To exercise the **real frontend → FastAPI → MongoDB** registration/policy workflow,
build the frontend and opt into the isolated API fixture:

```bash
npm --prefix frontend run build
MONGO_TEST_URI='mongodb://127.0.0.1:28017/?replicaSet=rs0' \
DASHBOARD_E2E_REAL_BACKEND=true \
  npm --prefix frontend run test:e2e -- \
    --grep 'workspace encryption settings|registering a device persists|stored agent devices|device detail IDs'
```

This starts the real FastAPI routes on **38002** with a test-only authentication
override, generated test EC keys and an in-memory AES-GCM Transit fixture.
It seeds unnamed agent-created records and
verifies inventory, detail navigation, registration and subsequent policy changes
without browser JavaScript, plus workspace persistence and conflict handling.
It writes to a unique
`dashboard_registration_test_*` database and drops only that database on shutdown.
Playwright stops both temporary servers. Use an approved development MongoDB
replica set, never production. The fixture is not a deployment entry point and
does not validate live Auth0 authentication or require OpenBao.

For production, use `make frontend-build` followed by `make frontend-start`
(listener default **3000**, overridable with `FRONTEND_PORT`),
provide private configuration through the runtime environment, and serve both
the public frontend and its remote API connection over HTTPS. The provided
scripts bind loopback for use behind a trusted reverse proxy. Do not cache
authenticated pages at that proxy. The Helm chart deploys both applications and
can create an optional path-routing Ingress; the external Ingress controller
provides reverse proxy/TLS termination, so no extra NGINX pod is deployed.
Build the standalone frontend image with `make frontend-image`.
No fonts or images are fetched from third-party services at runtime.
