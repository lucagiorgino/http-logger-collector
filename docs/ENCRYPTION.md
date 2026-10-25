# Log encryption and device configuration

## Transport and storage

Use HTTPS for every non-local client connection, including configuration requests.
The backend rejects HTTP `/api/v1/` requests by default outside development.
`REQUIRE_HTTPS=false` is intended only for isolated local development. Health,
OpenAPI, and documentation paths remain reachable by internal HTTP probes.

Terminate TLS at a separately managed ingress/gateway. Set `FORWARDED_ALLOW_IPS`
to only that proxy's actual IPs or CIDRs so Uvicorn can trust its forwarded scheme
and resolve the upload's source IP. The application uses the resulting ASGI peer,
never raw forwarding headers. Gateways must sanitize forwarded addresses.
Never trust arbitrary callers or use `*` on an externally reachable backend.
Restrict direct pod access to the gateway; use TLS on internal hops when required.
This chart does not install a gateway or provision certificates.

Application encryption supplements HTTPS; it does not replace authentication.
The backend decrypts logs for validation, redaction, and analysis. This is not
end-to-end secrecy against the backend. Upload encryption and the separate
workspace-controlled field encryption at rest have independent keys and policies.
The MVP publisher receives normalized documents with selected sensitive fields
encrypted for storage, not the original JWE envelope. A future broker requiring
opaque whole-batch encryption needs a different worker boundary.

Both encrypted and plaintext batches carry the same
[typed HTTP event schema](../README.md#strict-event-validation). Captured endpoints,
sizes/timing and process context survive ingestion. Request bodies and captured
authorization/cookie values are retained under the default-on storage policy.
Credential header values remain redacted in API responses; only authorized
single-record reads decrypt retained bodies. Collection reads omit bodies.
Unknown/duplicate fields, unsafe header keys, invalid types and per-field or
batch size violations reject the whole batch before publishing. Captured source IP
is an agent observation (`captured_source_ip`), never the trusted upload `source_ip`.

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

OpenBao manages separate keys for **EC key-set envelope protection** and
**sensitive log field encryption at rest**. The EC key-set path is:

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

Runtime permissions are restricted to the exact KV record's `read` capability,
the wrapping key's `decrypt` operation, and the separate storage key's metadata
`read` plus `encrypt`/`decrypt` operations. Storage encryption grants `update`,
not `create`, so runtime cannot create or replace keys. The application receives no
root token, key export/write capability, rotation privilege, or mount administration.
Fresh startup fails closed if OpenBao, credentials, or the key set are unavailable.
An already running process keeps its cached keys during a KMS outage; revoking
OpenBao access does not erase keys already in memory. Rotation and retirement
require restarting every backend process.

### Workspace storage encryption

The **Workspace** page (`/settings`) controls application-wide storage, independently
of per-device upload policies. Its **Storage and encryption** section offers
**Do not retain**, **Retain encrypted** (default), and **Retain plaintext** per category.
Both reads and changes require `manage:devices`.
The primary/majority-backed singleton in `workspace_configurations` defaults to:

```json
{
  "version": 0,
  "updated_at": null,
  "retain_request_body": true,
  "retain_authorization_header": true,
  "retain_cookies": true,
  "encrypt_request_body": true,
  "encrypt_authorization_header": true,
  "encrypt_cookies": true
}
```

The same workspace response includes independent ordinary/suspicious log
retention settings (enabled by default, 7/30 days). These are not encryption
settings: they apply to existing data and determine permanent deletion using
MongoDB TTL indexes. See [retention policies](../README.md#retention-policies).

`PATCH /api/v1/workspace/configuration` requires `expected_version` and all three
strict Boolean switches; unexpected fields are rejected. Atomic version matching
returns `409` for stale/concurrent edits. Optional strict Boolean `retain_*` fields
default to true and preserve their saved values when omitted. `retain_*=false`
takes precedence over the corresponding encryption switch.
Enabling encryption for any retained category validates KMS
availability before saving. Ingestion reads a primary-backed policy snapshot,
validates the whole batch, then encrypts selected sensitive values **before**
handing documents to the replaceable publisher. A failed settings read or KMS
operation prevents publishing and returns sanitized `503`; there is no plaintext
fallback, local key file, or success-shaped receipt.

- **Body:** the original validated UTF-8 body is stored only if retention is enabled.
  Byte length and SHA-256 digest are kept even when the body is discarded
  (`0`/`null` when no body was supplied).
- **Authorization:** case-insensitive `Authorization` and `Proxy-Authorization`
  values in captured request/response headers.
- **Cookies:** case-insensitive `Cookie` and `Set-Cookie` values in both maps.

Discarding authorization/cookies removes matching entries from both header maps,
not just their values. Discarded fields are never included in Transit ciphertext
or passed to the publisher. Selecting **Retain plaintext** deliberately disables
encryption while keeping retention enabled.
It does not disable TLS or upload JWE policy. Changes do not rewrite existing
logs, run migrations, or recover previously discarded data. `X-Api-Key` remains
redacted/discarded, independently of these switches.

#### Storage algorithm and key lifecycle

`OPENBAO_STORAGE_KEY` defaults to `log-storage` (Helm `openbao.storageKey`).
It must differ from the EC wrapping `OPENBAO_TRANSIT_KEY`. Idempotent bootstrap
creates a non-exportable, non-derived Transit `aes256-gcm96` key with plaintext
backup and convergent encryption disabled. The AES key never leaves OpenBao;
the backend uses Transit batch encryption rather than implementing cryptography.
OpenBao supplies random 96-bit nonces and full-length authentication tags.
An authenticated context binds each encrypted sensitive-field bundle to its
record ID, device ID, batch ID, field categories and format purpose/version.
Ciphertext swapping or context changes fail authentication.

MongoDB stores `storage_encryption` with format version, field categories and a
versioned `vault:vN:...` ciphertext; selected header values are `[ENCRYPTED]`.
Analysis collection reads stay on replicas, exclude bodies and ciphertext at the
database query layer, and redact credentials without KMS access. Large retained
body payloads therefore do not inflate list queries. After device authorization,
a single-record read fetches from
the replica and decrypts through Transit; the response exposes the retained body,
not ciphertext/key material, and still redacts authorization/cookie values.
Detailed responses use `Cache-Control: no-store`; the frontend escapes body text.
Disabling a setting does not prevent decryption of older encrypted records.

Standards basis: [FIPS 197](https://csrc.nist.gov/pubs/fips/197/final) specifies AES;
[NIST SP 800-38D](https://csrc.nist.gov/pubs/sp/800/38/d/final) specifies GCM,
including nonce uniqueness and invocation constraints; [SP 800-57 Part 1 Rev. 5](https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final)
provides key lifecycle guidance. This is authenticated **field/object encryption**,
not unauthenticated disk-sector encryption or a claim of a FIPS-validated deployment.
Use a validated cryptographic module/deployment when certification is required.

New storage keys auto-rotate every `720h` (30 days). Operators must monitor usage
and rotate **before 2^32 encryption invocations per key version**, or an earlier
organizational cryptoperiod, rather than treating the timer as a usage limiter.
The application does not implement a cross-client invocation counter. A high-volume
deployment must enforce a suitable shorter cryptoperiod/operational limit.

```bash
# Existing installations: provision the separate key and update the runtime ACL.
python -m app.kms initialize
# Rotate storage only; preserve old versions for retained logs.
python -m app.kms rotate-storage
```

Use `--admin-token-file` outside development. Runtime tokens cannot rotate,
export, delete or configure the storage key. Do not rename/delete the key or
raise `min_decryption_version` past any retained record; preserve key versions
and OpenBao storage/backups for the entire log retention period.
**Development OpenBao is ephemeral: restarting it destroys the storage key and
makes previously encrypted log fields permanently unreadable.** Re-bootstrap
creates a new key, not a recovery of the old one. Persistent production OpenBao,
TLS, access control, recovery procedures and appropriately protected backups are
required for durable encrypted logs.

### Local development

```bash
make install
make dev
```

Compose runs MongoDB and OpenBao; Python still runs on the host with live reload.
OpenBao listens on `http://localhost:28200`, separately from Kubernetes port 8200.
`make openbao-up` creates an ignored mode-0600 root credential if absent, starts
OpenBao, provisions Transit/KV, the wrapped EC key set and the dedicated storage
key, then writes a separate least-privilege runtime token. The root credential
is used only for development bootstrap.

For an already running, initialized/unsealed OpenBao, the existing initialization
script can be run independently, without restarting the server:

```bash
.venv/bin/python -m app.kms initialize
```

It creates missing application keys and updates the runtime ACL, preserving
existing key material and versions. It does not rotate keys, change MongoDB data,
or issue a runtime token. To issue a fresh development runtime token without a
container restart, use `.venv/bin/python -m app.kms bootstrap-dev`.

`.env.example` supplies parameterized addresses, mounts, key names, timeout, and
credential paths. The defaults are `.secrets/openbao-dev-root-token` and
`.secrets/openbao-token`. Do not print, commit, or share either file. The runtime
token expires after 24 hours; run `bootstrap-dev` again before restarting Python
to issue a fresh token. The root credential file is preserved, not regenerated.

**Development OpenBao is ephemeral**, auto-unsealed, and has no TLS. Restarting
it loses its in-memory KEK, EC key set and storage key, even though its root
credential file remains. Run `make openbao-up` and restart Python afterward.
Buffered envelopes and retained log fields encrypted to the lost keys cannot be
recovered. MongoDB's volume remains intact, but initialization cannot restore
those encrypted fields. Do not use dev mode for production.

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
`OPENBAO_CA_CERT`, `OPENBAO_TRANSIT_MOUNT`, `OPENBAO_TRANSIT_KEY`, `OPENBAO_STORAGE_KEY`, `OPENBAO_KV_MOUNT`,
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

Every resource is keyed only by globally unique device ID. Agents discover their JWT-bound `device_id` through
`GET /api/v1/me` before polling configuration: it is the signed
`https://amazing-http-logger/device_id` claim, or `sub` if absent. This ID, not a
batch field, selects the ingestion policy. Issuer-managed device claims must not
come from editable user metadata or token-request parameters. There is no separate
device enrollment/credential service. Tokens bound to the same device share its
access, regardless of their subject; different devices cannot access each other's
data without the global management grant.

Operators can register a named device and initial encryption policy through
`POST /api/v1/devices` or the frontend's **Device policies** form. This uses the
JWT-bound device ID and `manage:devices` authorization as policy management;
it does not issue credentials. Inventory uses primary-backed collection/item
reads, independently of traffic-analysis replica routing. All persisted records,
including unnamed/version-zero agent and policy records, appear in inventory;
`name` and `registered_at` may be null. The dedicated device detail page combines
identity, policy editing, acknowledgement, public suite metadata and a traffic link.
Item reads return `404` only when the record is absent; effective default
configuration lookups alone do not persist a device. See
[device registration](../README.md#register-a-device-and-configure-its-policy).
IDs and names are globally unique; duplicate registration returns `400`, including
attempts to register an existing policy-only ID. Initial registration is an insert,
not a policy update.

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
The permission authorizes fleet-wide policy management and reads. It is not
tenant-scoped; ordinary agents must not receive it.

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
the path ID must equal the reporting token's device ID (`403` otherwise), even if
the token has management permission.

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

1. Serialize `{"logs": [...]}` containing 1-500 traffic observations without
   identity, source IP or other attribution fields. Attribution comes from the
   verified JWT and trusted connection for both gzip and JWE uploads.
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
import httpx2 as httpx
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
Authentication, envelope integrity, decompression bounds, trusted attribution, and
the JWT-bound device's effective encryption policy are checked before publishing any log.
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
