# amazing-http-logger management console

An SSR management dashboard in **plain JavaScript/JSX**, built with Next.js App
Router, React, Tailwind CSS, and the JavaScript shadcn/ui registry components.
The Python backend remains the sole authority for ownership, permissions,
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

### Management permissions

Define the backend API permission `manage:devices` in Auth0 and grant it only to
intended management users (for example with an Auth0 role and API RBAC/permissions
claims). The default `AUTH0_SCOPE` requests `openid profile email manage:devices`.
For a read-only user, set it to `openid profile email`; viewing logs and device
configuration needs no management permission. The API independently checks the
verified token's `scope` or `permissions` when a policy is saved. The UI does not
manufacture permissions or issue tokens.

Interactive login uses the official `@auth0/nextjs-auth0` SDK. Its encrypted,
HTTP-only session cookie holds the session; API tokens never enter browser local
storage, client component props, or a browser token endpoint. The SDK's
`/auth/access-token` route is disabled. Refresh persistence occurs in Next.js's
`proxy.js` before Server Components read the access token. This implements
server-side access, not a browser SPA making credential-bearing API calls.

## Screens and API boundaries

| Route                    | Purpose                                                                           |
| ------------------------ | --------------------------------------------------------------------------------- |
| `/`                      | Welcome/sign-in or configuration guidance                                         |
| `/overview`              | Latest-50-record sample, HTTP response distribution, recent traffic               |
| `/logs`                  | Device/UTC time filters, cursor pagination, redacted request metadata             |
| `/logs/:id`              | Request, timestamps, headers, body length and SHA-256                             |
| `/devices?device_id=...` | Exact device lookup, encryption policy, acknowledgement and public suite metadata |
| `/settings`              | API-verified identity and access/architecture context                             |

Pages render their API data on the server with `cache: no-store`. Read views
work without browser JavaScript; client JavaScript enhances navigation and the
save button's pending state. Policy forms use authenticated Server Actions with
Next.js's same-origin protections. Writes carry `expected_version`; conflicts
reload the current policy with an explicit error instead of overwriting it.
Timeouts, permission denials, API outages, and empty results are different UI states.

**Scope is deliberately unchanged:** all current API resources are bound to the
access token's `sub`. A human user's subject differs from an agent M2M client's
subject, so this console cannot administer those M2M-owned devices just by
possessing `manage:devices`. Cross-owner administration requires a separately
designed backend delegation/tenant model. Never reuse or impersonate an agent's
credentials in the UI to work around ownership.

There is no device-inventory or aggregation API. Enter an exact known device ID
or follow one from traffic. The API's default TLS-only/version-0 response does not
prove that a device exists or is online. Overview counts are labeled as **sample**
metrics, not total fleet statistics. A saved policy is not proof that an agent has
applied it; the acknowledgement/version is displayed separately.

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
permission errors, concurrent policy conflicts, mobile overflow, and the disabled
token endpoint. These tests do **not** claim to validate a live Auth0 login.

For production, use `make frontend-build` followed by `make frontend-start`
(listener default **3001**, overridable with `FRONTEND_PORT`),
provide private configuration through the runtime environment, and serve both
the public frontend and its remote API connection over HTTPS. The provided
scripts bind loopback for use behind a trusted reverse proxy. Do not cache
authenticated pages at that proxy. The existing Helm chart deploys the Python
stack only; frontend deployment infrastructure is not bundled with this UI.
No fonts or images are fetched from third-party services at runtime.
