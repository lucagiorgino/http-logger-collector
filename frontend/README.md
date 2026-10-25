# amazing-http-logger management console

An SSR management dashboard in **plain JavaScript/JSX**, built with Next.js App
Router, React, Tailwind CSS, and the JavaScript shadcn/ui registry components.
The Python backend remains the sole authority for device access, permissions,
persistence, and encryption policy.

## Run locally

Use Node.js 22.13+ LTS or 24+ LTS, npm, and a running backend (`make dev` in a
separate terminal).

From the repository root:

```bash
make frontend-install
cp frontend/.env.example frontend/.env.local
openssl rand -hex 32
# Set the generated value as AUTH0_SECRET in frontend/.env.local.
# Fill in the other Auth0 settings described below.
make frontend-dev
```

Open **http://localhost:3000**. Without complete Auth0 configuration, the welcome
page displays setup instructions instead of pretending to be authenticated.
There is no mock/demo bypass in the application.

The frontend has its own environment file; it does not read the backend's `.env`.
Its default development port **3000** differs from host Python **8001**, Kubernetes API pods
**8000**, and the API port-forward **18000**. Change the listener with
`make frontend-dev FRONTEND_PORT=3010` (or `PORT=3010 npm --prefix frontend run dev`)
and update `APP_BASE_URL` and the corresponding Auth0 URLs together. `PORT` must
be supplied to the process, not placed in Next.js's `.env.local`.

`BACKEND_API_URL` is reached by the Next.js **server**, not the browser. When both
processes run on the development host, it can use `http://127.0.0.1:8001`, even if
the browser reaches the frontend through a public HTTPS reverse proxy. To test
through a public backend tunnel instead, set it to that tunnel's HTTPS origin.
The public frontend origin belongs in `APP_BASE_URL`; it does not determine the
API address.

For ngrok, configure the existing **backend** endpoint's upstream as
`http://127.0.0.1:8001`, not port 8000. The **frontend** endpoint's upstream is
`http://127.0.0.1:3000`. These loopback addresses assume the ngrok agent runs on
the same host/network as the corresponding process; otherwise use an address
reachable from the agent. Keep the public frontend origin in the identity
provider's callback/logout configuration. Changing a tunnel's upstream does not
require changing its public domain or the frontend's API URL.

If the UI reports **HTTP 502** while the local backend works, check
`BACKEND_API_URL` from the frontend host. A proxy can return 502 HTML before the
request ever reaches FastAPI. Check `/health` at that configured address and at
the local API; direct unauthenticated `/api/v1/traffic-logs` requests should return
401, not a gateway error. Ngrok's `ERR_NGROK_8012` indicates an upstream connection
problem, not an OAuth rejection. Check its reported target against the API's
actual listener, including HTTP versus HTTPS, host, port and IPv4/IPv6 resolution.
Fix the proxy's upstream or use the reachable internal API address, then restart
the frontend after changing environment settings.

## Auth0 configuration

Create a **Regular Web Application** dedicated to this management frontend.
Do not reuse an agent's machine-to-machine client secret.

For the defaults, register:

| Auth0 setting         | Value                                 |
| --------------------- | ------------------------------------- |
| Allowed Callback URLs | `http://localhost:3000/auth/callback` |
| Allowed Logout URLs   | `http://localhost:3000`               |
| Allowed Web Origins   | `http://localhost:3000`               |

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

After changing environment settings, restart `make frontend-dev` and begin a
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
For ngrok this is the public **frontend** HTTPS URL, not the backend URL,
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

Pages render their API data on the server with `cache: no-store`. Read views
work without browser JavaScript; client JavaScript enhances navigation and the
save button's pending state. Policy forms use authenticated Server Actions with
Next.js's same-origin protections. Policy edits carry `expected_version`; conflicts
reload the current policy with an explicit error instead of overwriting it.
Timeouts, permission denials, API outages, and empty results are different UI states.

**Device-only scope:** resources use globally unique device IDs, not user/device
pairs. Operators with `manage:devices` can manage and read fleet resources,
including independent M2M devices. Tokens without the grant are restricted to
their issuer-bound device. Authentication is still mandatory; broad management
access must be granted only to trusted operators. Status reports always require
the agent's own JWT-bound ID.

Register a device using its exact issuer-bound ID, a name and its initial policy.
The form calls `POST /api/v1/devices` with the server-side access token; the ID must
match the agent's `/api/v1/me`. No Auth0 credentials or JWT
claims are created by this form. Inventory comes from `GET /api/v1/devices` with
keyset pagination. It includes named registrations and records created by agent
acknowledgements or policy changes, including version-zero records without names
or registration timestamps. These appear as **Unnamed device** and
**Agent / policy record**; the UI does not fabricate registration metadata.
Both IDs and names must be globally unique. Registration sends no version field;
an existing ID (including a policy-only record) or name returns HTTP 400 and shows
**Device already exists**, not a success message. Duplicate requests never change
the existing policy. Use the policy editor, not registration, for existing devices.
Exact names are case-sensitive; the form trims surrounding whitespace.

Inventory and traffic links open dedicated device details using
`GET /api/v1/devices/{id}`. Missing records and API failures are explicit error
states. Exact policy lookup still works for unknown IDs through `/configuration`;
it does not create a record until a policy is saved or the agent reports status.
The API's default TLS-only/version-0 response does not prove that a device exists
or is online. Encoded IDs, including `@clients` and literal percent characters,
retain their identity through navigation and API calls. There is no aggregation API. Overview counts are labeled as **sample**
metrics, not total fleet statistics. A saved policy is not proof that an agent has
applied it; the acknowledgement/version is displayed separately.

**Workspace configuration:** `/settings` reads and updates
`/api/v1/workspace/configuration` with the server-side token. `manage:devices`
is required for both operations. **Storage and encryption** provides a choice
for each of request bodies, authorization headers and cookies:
**Do not retain**, **Retain encrypted** (default), or **Retain plaintext**.
Retained encrypted fields use a dedicated OpenBao AES-256-GCM key, separate
from agent upload JWE. Discarded fields never reach storage or Transit; discarded
bodies keep their byte length/SHA-256 digest, and discarded header entries are removed.
Plaintext retention is an explicit encryption opt-out. The form warns about
discarded data and plaintext storage, and uses
`expected_version` to reject stale edits with HTTP 409. It works without JavaScript.
Existing records are not rewritten; old discarded bodies cannot be recovered.
Authorized request details display retained bodies as escaped text. Collection
reads omit bodies; authorization/cookie values stay redacted even in details.

**Retention policies:** a separate Workspace section controls ordinary logs
(enabled, 7 days by default) and suspicious logs (enabled, 30 days by default).
Each has an independent enable switch and integer day window (1-24855).
Disabling a category retains it indefinitely. The form saves both sections with
the same expected version and works without JavaScript.
Retention applies to existing records based on event timestamps, unlike field
storage choices, which affect future uploads. The UI warns that expired data is permanently
deleted and MongoDB background bucket expiration is not an exact deadline.
Suspicious classification is server-owned and reserved for a future analysis job;
agents cannot exempt uploads from ordinary retention. A failed save can leave
desired settings stored without all index changes applied; reload and retry after
backend recovery. See the [backend retention contract](../README.md#retention-policies).

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
(listener default **3001**, overridable with `FRONTEND_PORT`),
provide private configuration through the runtime environment, and serve both
the public frontend and its remote API connection over HTTPS. The provided
scripts bind loopback for use behind a trusted reverse proxy. Do not cache
authenticated pages at that proxy. The existing Helm chart deploys the Python
stack only; frontend deployment infrastructure is not bundled with this UI.
No fonts or images are fetched from third-party services at runtime.
