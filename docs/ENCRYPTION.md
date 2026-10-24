# Log encryption and device configuration

## Transport and storage

Use HTTPS for every non-local client connection, including configuration requests.
The backend rejects HTTP `/api/v1/` requests by default outside development.
`REQUIRE_HTTPS=false` is intended only for isolated local development. Health,
OpenAPI, and documentation paths remain reachable by internal HTTP probes.

Terminate TLS at a separately managed ingress/gateway. Set `FORWARDED_ALLOW_IPS`
to only that proxy's actual IPs or CIDRs so Uvicorn can trust its forwarded scheme.
Never trust arbitrary callers or use `*` on an externally reachable backend.
Restrict direct pod access to the gateway; use TLS on internal hops when required.
This chart does not install a gateway or provision certificates.

Application encryption supplements HTTPS; it does not replace authentication.
The backend decrypts logs for validation, redaction, and analysis. This is not
end-to-end secrecy against the backend, nor MongoDB encryption at rest.
The MVP publisher receives normalized, redacted documents, not encrypted envelopes.
A future broker requiring opaque encrypted payloads needs a different worker boundary.

## Configured cryptographic suite

The suite is selected once in server configuration, not by an upload's untrusted
header. Key generation, startup validation, public-key responses, and decryption
all use these settings:

| Environment variable | Helm value | Default | Supported values |
| --- | --- | --- | --- |
| `ENCRYPTION_KEY_ALGORITHM` | `encryption.keyAlgorithm` | `ECDH-ES+A256KW` | `ECDH-ES+A128KW`, `ECDH-ES+A192KW`, `ECDH-ES+A256KW` |
| `ENCRYPTION_CONTENT_ALGORITHM` | `encryption.contentAlgorithm` | `A256GCM` | `A128GCM`, `A192GCM`, `A256GCM` |
| `ENCRYPTION_CURVE` | `encryption.curve` | `P-384` | `P-256`, `P-384`, `P-521` |

These settings affect log envelopes, not Auth0 token signing or TLS cipher suites.
Set these in `.env` before generating keys and restart the server after changes.
Invalid or unsupported selections fail explicitly at startup and key generation.
Only the configured suite is accepted; upload headers cannot select a weaker suite.
All retained private keys must use the configured curve and, when present, algorithm.
Changing a suite requires coordinated client configuration and matching keys with
a fresh key ID;
previously buffered envelopes using a different suite are not accepted.

The default uses ephemeral-static ECDH on the NIST P-384 curve to derive an AES-256
key-wrapping key; a separate random AES-256-GCM key encrypts the gzip payload.
ECC establishes keys rather than directly encrypting log bytes. P-384 provides
approximately 192-bit classical security, so this is not a claim of 256-bit overall
security simply because the AES key is 256 bits.

Standards basis:
- [NIST SP 800-186](https://csrc.nist.gov/pubs/sp/800/186/final) specifies recommended
  elliptic-curve domain parameters, including the supported NIST prime-field curves.
- [NIST SP 800-56A Rev. 3](https://csrc.nist.gov/pubs/sp/800/56/a/r3/final) specifies
  discrete-logarithm key-establishment schemes, including ECDH. NIST announced an
  update in January 2026; that announcement is not a replacement final standard.
- [NIST SP 800-38F](https://csrc.nist.gov/pubs/sp/800/38/f/final) specifies AES key
  wrapping; [NIST SP 800-38D](https://csrc.nist.gov/pubs/sp/800/38/d/final) specifies
  AES-GCM for authenticated payload encryption.

Using these components does not make this application a FIPS-validated module.
ECC is **not post-quantum secure**; NIST's post-quantum KEM is
[ML-KEM, FIPS 203](https://csrc.nist.gov/pubs/fips/203/final), which is not implemented.

## OpenBao key management

OpenBao is the KMS for **envelope protection of the server's EC key set**:

1. The administrative CLI generates private EC keys in memory.
2. OpenBao Transit encrypts the key set under a non-exportable `aes256-gcm96`
   key-encryption key (KEK), with plaintext backup disabled.
3. Only the wrapped ciphertext is saved in OpenBao KV v2, using compare-and-swap.
4. At startup the backend authenticates, reads that ciphertext, and asks Transit
   to unwrap it. The private EC keys are validated and cached in backend RAM.
5. Client JWE/ECDH decryption runs locally in the backend.

OpenBao's Transit EC key types provide ECDSA signing, **not ECDH JWE decryption**.
The KEK stays inside OpenBao; the EC private keys do not. This design does not
claim non-exportable EC keys, remote ECDH, or secrecy from a compromised backend.
There are no plaintext EC key files or private EC key Kubernetes Secrets.
The active key ID belongs to the wrapped document, not an environment variable.

Runtime permissions are restricted to the exact KV record's `read` capability
and the exact Transit key's `decrypt` operation. The application receives no
root token, encryption/write capability, rotation privilege, or mount administration.
Fresh startup fails closed if OpenBao, credentials, or the key set are unavailable.
An already running process keeps its cached keys during a KMS outage; revoking
OpenBao access does not erase keys already in memory. Rotation and retirement
require restarting every backend process.

### Local development

```bash
make install
make dev
```

Compose runs MongoDB and OpenBao; Python still runs on the host with live reload.
OpenBao listens on `http://localhost:28200`, separately from Kubernetes port 8200.
`make openbao-up` creates an ignored mode-0600 root credential if absent, starts
OpenBao, provisions Transit/KV and the key set, then writes a separate least-privilege
runtime token. The root credential is used only for development bootstrap.

`.env.example` supplies parameterized addresses, mounts, key names, timeout, and
credential paths. The defaults are `.secrets/openbao-dev-root-token` and
`.secrets/openbao-token`. Do not print, commit, or share either file. The runtime
token expires after 24 hours; run `make openbao-up` again before restarting Python
to issue a fresh token. The root credential file is preserved, not regenerated.

**Development OpenBao is ephemeral**, auto-unsealed, and has no TLS. Restarting
it loses its in-memory KEK and EC key set, even though its root credential file
remains. Run `make openbao-up` and restart Python afterward. Buffered envelopes
encrypted to the lost keys cannot be recovered. MongoDB's persistent volume and
already normalized logs are unaffected. Do not use dev mode for production.

### Production and Kubernetes

Provision a persistent, initialized/unsealed, TLS-enabled OpenBao independently.
Configure its storage backups, unseal/recovery process, access controls, and audit
devices outside this chart. The chart defaults to this external service; it does
not automatically initialize or unseal production OpenBao.

Run initialization from an authorized operator environment, using the same
configured suite and OpenBao mounts/key names as the backend:

```bash
OPENBAO_ADDR=https://bao.example.com \
  .venv/bin/python -m app.kms initialize \
  --admin-token-file /secure/operator-token --key-id ingestion-1 \
  --policy logger-ingestion
```

Use a dedicated provisioning credential with the required administrative
capabilities, not the runtime credential. Initialization preserves existing keys
and refuses incompatible engines or exportable KEKs rather than replacing them.

Configure external OpenBao and the selected suite in Helm:

```yaml
openbao:
  enabled: false
  address: https://bao.example.com
  auth:
    mount: kubernetes
    role: logger-ingestion
    audience: openbao
encryption:
  keyAlgorithm: ECDH-ES+A256KW
  contentAlgorithm: A256GCM
  curve: P-384
app:
  requireHttps: true
  forwardedAllowIps: "<actual gateway IPs or CIDRs>"
```

The API uses a dedicated ServiceAccount and an audience-bound, short-lived projected
JWT, not an automatically mounted general-purpose token. The OpenBao operator must
enable Kubernetes auth and bind the role to the exact API ServiceAccount
(`logger-amazing-http-logger-api` for release `logger`) and namespace, audience
`openbao`, and runtime policy `logger-ingestion`. Do not give the API TokenReview
or administrative privileges.

For an OpenBao server running inside the same cluster, its own ServiceAccount
needs `system:auth-delegator`; OpenBao uses its local reviewer JWT and CA. The
`bootstrap-kubernetes` CLI provisions that local-cluster auth configuration and
role when given `--service-account`, `--namespace`, and optionally `--audience`
and `--kubernetes-host`. An OpenBao outside the cluster needs separately managed,
rotating TokenReview credentials, a reachable Kubernetes API address and its CA;
the operator must configure that auth engine/role rather than assuming local
service-account files exist.

For a private CA, set `openbao.ca.existingSecret` and optionally `openbao.ca.key`
(default `ca.crt`). TLS verification is never disabled. An alternative to Kubernetes
auth is `openbao.auth.existingSecret`, with `tokenKey` defaulting to `token`; only
the least-privilege token is mounted. Provision/rotate it outside Helm and restart
the backend when it changes. Never place credentials in values or `--set`.

For host processes the matching settings are `OPENBAO_ADDR`, `OPENBAO_TOKEN_FILE`,
`OPENBAO_CA_CERT`, `OPENBAO_TRANSIT_MOUNT`, `OPENBAO_TRANSIT_KEY`, `OPENBAO_KV_MOUNT`,
`OPENBAO_KV_PATH`, and `OPENBAO_TIMEOUT_SECONDS`. Kubernetes auth additionally uses
`OPENBAO_AUTH_MOUNT`, `OPENBAO_KUBERNETES_ROLE`, and `OPENBAO_JWT_FILE`.
Mounts/key names must be single safe path components. HTTPS is mandatory outside
development; HTTP redirects and environment proxy settings are not followed.

The Helm development profile instead bundles ephemeral OpenBao, a generated
development root Secret, TokenReview RBAC, and a revision-named bootstrap Job.
Only OpenBao and that Job mount the root Secret; API pods use Kubernetes login.
The Service is ClusterIP and ingress is restricted to API/bootstrap pods when
NetworkPolicy is enforced. Helm preserves the dev root Secret across upgrades.
After an OpenBao pod restart, rerun `make helm-up`, then restart the API Deployment
to load the newly bootstrapped key set. The same buffered-envelope loss warning
as local dev mode applies.

References: [Transit](https://openbao.org/docs/secrets/transit/),
[Transit API](https://openbao.org/docs/api/secret/transit/),
[KV v2 API](https://openbao.org/docs/api/secret/kv/kv-v2/), and
[Kubernetes auth](https://openbao.org/docs/auth/kubernetes/).

## Management protocol

Every device is scoped by the verified Auth0 `sub` and device ID. Management calls
cannot select another owner. Device IDs use the same namespace as submitted logs;
there is no separate device enrollment/credential service. Clients authenticated
as the same owner share that owner's device access.

`GET /api/v1/devices/{id}/configuration` returns:

```json
{
  "device_id": "device-1",
  "version": 1,
  "encryption_mode": "optional",
  "encryption": {
    "algorithm": "ECDH-ES+A256KW",
    "encryption": "A256GCM",
    "curve": "P-384",
    "content_type": "application/jose",
    "public_key": {
      "kty": "EC", "kid": "ingestion-1", "use": "enc",
      "alg": "ECDH-ES+A256KW", "crv": "P-384",
      "x": "<public x coordinate>", "y": "<public y coordinate>"
    }
  },
  "refresh_after_seconds": 300,
  "configuration_status": null
}
```

An unconfigured device has version `0`, mode `tls_only`, and no public key.
These responses use `Cache-Control: no-store`.

| Mode | Accepted uploads |
| --- | --- |
| `tls_only` | gzip JSON only |
| `optional` | gzip JSON or application-encrypted JWE |
| `required` | application-encrypted JWE only |

`PATCH /api/v1/devices/{id}/configuration` requires **`manage:devices`** in the
verified token's `permissions` array or space-separated `scope`. Configure this
permission in Auth0 and grant it only to administrators, not ordinary agent tokens.
The permission is still owner-scoped, not cross-tenant administration.

```json
{"expected_version": 0, "encryption_mode": "optional"}
```

The response contains the new configuration and incremented version. A stale
expected version returns `409`; fetch the latest configuration before retrying.
Updates use atomic compare-and-swap and majority writes on primary.

Clients poll on startup, then at the indicated interval, and refresh after policy
rejection or key changes. Apply the full advertised suite and public key even if
the policy version has not changed; suite selection and key rotation are independent
of per-device policy versions.

`PUT /api/v1/devices/{id}/configuration-status` reports capability and outcome:

```json
{
  "version": 1,
  "status": "applied",
  "supports_encryption": true,
  "key_id": "ingestion-1"
}
```

Valid statuses are `applied`, `unsupported`, and `error`; optional `detail` is at
most 500 characters and must not contain credentials or log content. Applied
encrypted configurations must acknowledge support and the active key ID. Stale
versions return `409`, and false applied capability/key claims return `422`.
Status is self-reported, not proof: ingestion always independently enforces policy.
The latest report and its server timestamp appear in the configuration response.
Compare the report's version/key ID to the current configuration to detect pending
changes or rotation.

Roll out `optional` first, inspect capability/status reports, then set `required`.
Unsupported existing clients cannot acquire encryption through a server setting:
upgrade them first. Clients must not fall back to plaintext when encryption is
required. Buffer or surface delivery errors; do not silently drop logs.

## Encrypted ingestion

1. Serialize `{"logs": [...]}` containing 1-500 logs.
2. Gzip once.
3. Encrypt the gzip bytes as compact JWE using the advertised suite and public EC key.
4. POST the JWE string to `/api/v1/traffic-log-batches` with an Auth0 bearer token
   and `Content-Type: application/jose`. Do **not** send `Content-Encoding: gzip`.

The protected JWE header must contain the advertised `alg` and `enc`, `kid`,
`cty=application/gzip`, and the sender's ephemeral **public** EC key `epk`.
The JOSE library generates a fresh ephemeral key for each upload; do not reuse or
implement ephemeral key agreement yourself. Its `epk` contains only `kty`, `crv`,
`x`, and `y`, with the configured curve, never a private scalar `d`.
Optional `typ` must be `JWE`. Other headers, including JWE `zip`, remote key URLs,
and non-configured algorithms, are rejected. No client-shared secret is needed.

Python agent example using `joserfc` (the configuration and token are obtained
over authenticated HTTPS):

```python
import gzip
import json
import httpx
from joserfc import jwe
from joserfc.jwk import ECKey

public = configuration["encryption"]["public_key"]
suite = configuration["encryption"]
registry = jwe.JWERegistry(algorithms=[suite["algorithm"], suite["encryption"]])
registry.max_ciphertext_length = 1_500_000
envelope = jwe.encrypt_compact(
    {
        "alg": suite["algorithm"], "enc": suite["encryption"],
        "kid": public["kid"], "cty": "application/gzip",
    },
    gzip.compress(json.dumps({"logs": logs}).encode()),
    ECKey.import_key(public),
    registry=registry,
)
response = httpx.post(
    f"{base_url}/api/v1/traffic-log-batches",
    content=envelope,
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/jose",
    },
    timeout=30,
)
response.raise_for_status()
```

Limits: 1,500,000 bytes encrypted wire size, 1 MiB decrypted gzip, 8 MiB expanded.
Authentication, envelope integrity, decompression bounds, ownership, and every
device's effective encryption policy are checked before publishing any log.
Malformed/tampered/unknown-key envelopes return generic `400`; wrong media encoding
returns `415`; size overflow returns `413`; policy violations return `403`; storage
or policy/key-management unavailability returns `503`. Successful receipts remain `202` with
`status=stored` for MongoDB or `queued` for a replacement publisher.

Control-plane policy reads use primary with majority read concern, never a stale
analysis replica. Analysis retains its existing secondary-only deployment routing.
Policy is checked during each request; changes do not retroactively cancel a
request already being accepted. Envelopes do not provide replay deduplication:
the MVP's existing retry/partial-write duplicate limitations still apply.

## Key rotation

All commands use the configured OpenBao address, mounts and suite. Specify an
operator credential file with `--admin-token-file`; development defaults to the
ignored development root credential. No command outputs EC private keys.

For a single stopped backend, generate and activate a new key before restarting:

```bash
.venv/bin/python -m app.kms rotate --key-id ingestion-2 \
  --admin-token-file /secure/operator-token
```

For multiple replicas, use two phases so no instance advertises a key that another
instance has not loaded:

```bash
.venv/bin/python -m app.kms stage --key-id ingestion-2 \
  --admin-token-file /secure/operator-token
# Restart/roll out every backend instance; wait until all have both keys.
.venv/bin/python -m app.kms activate --key-id ingestion-2 \
  --admin-token-file /secure/operator-token
# Restart/roll out every backend instance again to advertise the new active key.
```

Each update wraps the complete key set and uses KV compare-and-swap. A conflict
fails explicitly; inspect current state before retrying. Old private keys remain
available for buffered envelopes. Keep them for at least the maximum supported
offline/retry period, then retire one:

```bash
.venv/bin/python -m app.kms retire --key-id ingestion-1 \
  --admin-token-file /secure/operator-token
```

The active key cannot be retired. Restart **all** backend processes after retirement;
otherwise old cached keys remain usable. Retirement removes the key from the current
key set, not historical KV versions, backups, or existing process memory. Removed or
unknown IDs are rejected; clients must surface the error, not downgrade encryption.

Rotate the Transit wrapping KEK separately:

```bash
.venv/bin/python -m app.kms rotate-kek --admin-token-file /secure/operator-token
```

This rotates the non-exportable KEK and rewraps the current KV ciphertext without
changing EC keys or the public client protocol; no backend restart is needed for
KEK-only rewrapping. Keep old KEK versions for retained KV history/backups.
Back up OpenBao storage and recovery material together through your operational
process; wrapped KV data alone cannot recover a lost Transit KEK.
