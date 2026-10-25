import argparse
import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

from joserfc.jwk import ECKey

from app.core.config import settings
from app.core.encryption import KEY_ID, parse_keyset
from app.core.openbao import KMSUnavailable, OpenBaoClient, read_credential
from app.core.storage_encryption import validate_storage_key


def write_credential(path: str, value: str, *, create_only: bool = False) -> None:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if create_only:
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            read_credential(path)
            return
        with os.fdopen(descriptor, "w") as output:
            output.write(value)
        return
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", dir=destination.parent, prefix=".openbao-token-", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(value)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_development() -> None:
    if settings.app_env != "development":
        raise ValueError("OpenBao development setup is restricted to development")
    write_credential(
        settings.openbao_dev_root_token_file, secrets.token_urlsafe(32), create_only=True
    )


def ensure_engines(client: OpenBaoClient) -> None:
    response = client.request("GET", "sys/mounts") or {}
    mounts = response.get("data", {})
    for mount, kind, options in (
        (settings.openbao_transit_mount, "transit", {}),
        (settings.openbao_kv_mount, "kv", {"version": "2"}),
    ):
        existing = mounts.get(mount + "/")
        if existing is None:
            client.request("POST", f"sys/mounts/{mount}", {"type": kind, "options": options})
        elif existing.get("type") != kind or (
            kind == "kv" and existing.get("options", {}).get("version") != "2"
        ):
            raise KMSUnavailable("OpenBao mount does not match the required engine")
    path = f"{settings.openbao_transit_mount}/keys/{settings.openbao_transit_key}"
    response = client.request("GET", path, missing_ok=True)
    if response is None:
        client.request(
            "POST",
            path,
            {"type": "aes256-gcm96", "exportable": False, "allow_plaintext_backup": False},
        )
        response = client.request("GET", path)
    key = (response or {}).get("data", {})
    if (
        key.get("type") != "aes256-gcm96"
        or key.get("exportable") is not False
        or key.get("allow_plaintext_backup") is not False
    ):
        raise KMSUnavailable("OpenBao wrapping key must be non-exportable AES-256-GCM")
    storage_path = f"{settings.openbao_transit_mount}/keys/{settings.openbao_storage_key}"
    if client.request("GET", storage_path, missing_ok=True) is None:
        client.request(
            "POST",
            storage_path,
            {
                "type": "aes256-gcm96",
                "exportable": False,
                "allow_plaintext_backup": False,
                "derived": False,
                "convergent_encryption": False,
                "auto_rotate_period": "720h",
            },
        )
    validate_storage_key(client)


def generate_key(key_id: str) -> dict:
    if not KEY_ID.fullmatch(key_id):
        raise ValueError("Key ID must contain 1-128 letters, digits, dots, underscores or hyphens")
    settings.validate_encryption_suite()
    return ECKey.generate_key(
        settings.encryption_curve,
        parameters={"kid": key_id, "alg": settings.encryption_key_algorithm, "use": "enc"},
    ).as_dict(private=True)


def initialize(client: OpenBaoClient, key_id: str) -> None:
    ensure_engines(client)
    record = client.read_wrapped(missing_ok=True)
    if record is not None:
        parse_keyset(client.unwrap(record))
        return
    client.store_keyset({"active_key_id": key_id, "keys": [generate_key(key_id)]}, 0)


def configure_runtime_policy(client: OpenBaoClient, policy_name: str) -> None:
    if not KEY_ID.fullmatch(policy_name) or policy_name in {".", ".."}:
        raise ValueError("Invalid OpenBao runtime policy name")
    policy = (
        f'path "{client.kv_path}" {{ capabilities = ["read"] }}\n'
        f'path "{settings.openbao_transit_mount}/decrypt/{settings.openbao_transit_key}" '
        '{ capabilities = ["update"] }\n'
        f'path "{settings.openbao_transit_mount}/keys/{settings.openbao_storage_key}" '
        '{ capabilities = ["read"] }\n'
        f'path "{settings.openbao_transit_mount}/encrypt/{settings.openbao_storage_key}" '
        '{ capabilities = ["update"] }\n'
        f'path "{settings.openbao_transit_mount}/decrypt/{settings.openbao_storage_key}" '
        '{ capabilities = ["update"] }\n'
    )
    client.request("PUT", f"sys/policies/acl/{policy_name}", {"policy": policy})


def configure_kubernetes(
    client: OpenBaoClient,
    policy: str,
    service_account: str,
    namespace: str,
    audience: str,
    kubernetes_host: str,
) -> None:
    response = client.request("GET", "sys/auth") or {}
    mount = settings.openbao_auth_mount
    existing = response.get("data", {}).get(mount + "/")
    if existing is None:
        client.request("POST", f"sys/auth/{mount}", {"type": "kubernetes"})
    elif existing.get("type") != "kubernetes":
        raise KMSUnavailable("OpenBao auth mount is not Kubernetes authentication")
    client.request(
        "POST",
        f"auth/{mount}/config",
        {"kubernetes_host": kubernetes_host, "disable_iss_validation": True},
    )
    if not KEY_ID.fullmatch(
        settings.openbao_kubernetes_role
    ) or settings.openbao_kubernetes_role in {".", ".."}:
        raise ValueError("OPENBAO_KUBERNETES_ROLE is required for Kubernetes bootstrap")
    client.request(
        "POST",
        f"auth/{mount}/role/{settings.openbao_kubernetes_role}",
        {
            "bound_service_account_names": [service_account],
            "bound_service_account_namespaces": [namespace],
            "audience": audience,
            "token_policies": [policy],
            "token_no_default_policy": True,
            "token_ttl": "1h",
            "token_max_ttl": "1h",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage OpenBao ingestion and storage encryption keys"
    )
    parser.add_argument(
        "command",
        choices=[
            "prepare-dev",
            "bootstrap-dev",
            "bootstrap-kubernetes",
            "initialize",
            "rotate",
            "stage",
            "activate",
            "retire",
            "rotate-kek",
            "rotate-storage",
        ],
    )
    parser.add_argument("--admin-token-file")
    parser.add_argument("--key-id")
    parser.add_argument("--policy", default="logger-ingestion")
    parser.add_argument("--service-account")
    parser.add_argument("--namespace")
    parser.add_argument("--audience", default="openbao")
    parser.add_argument("--kubernetes-host", default="https://kubernetes.default.svc:443")
    args = parser.parse_args()
    settings.validate_encryption_suite()
    if args.command == "prepare-dev":
        prepare_development()
        print("Development OpenBao root credential file is ready")
        return
    if args.command == "bootstrap-dev" and settings.app_env != "development":
        parser.error("bootstrap-dev is restricted to development")
    if not args.admin_token_file and settings.app_env != "development":
        parser.error("--admin-token-file is required outside development")
    if args.command == "bootstrap-kubernetes" and not (args.service_account and args.namespace):
        parser.error("--service-account and --namespace are required")
    token_file = args.admin_token_file or settings.openbao_dev_root_token_file
    key_id = args.key_id or "ingestion-" + secrets.token_hex(8)
    with OpenBaoClient(read_credential(token_file)) as client:
        if args.command in {"initialize", "bootstrap-dev", "bootstrap-kubernetes"}:
            initialize(client, key_id)
            configure_runtime_policy(client, args.policy)
        if args.command == "bootstrap-dev":
            response = client.request(
                "POST",
                "auth/token/create",
                {
                    "policies": [args.policy],
                    "no_default_policy": True,
                    "ttl": "24h",
                    "renewable": False,
                    "display_name": "logger-development",
                },
            )
            auth = (response or {}).get("auth")
            token = auth.get("client_token") if isinstance(auth, dict) else None
            if not isinstance(token, str) or not token:
                raise KMSUnavailable("OpenBao did not issue a runtime token")
            write_credential(settings.openbao_token_file, token)
        elif args.command == "bootstrap-kubernetes":
            configure_kubernetes(
                client,
                args.policy,
                args.service_account,
                args.namespace,
                args.audience,
                args.kubernetes_host,
            )
        elif args.command in {"rotate", "stage", "activate", "retire"}:
            record = client.read_wrapped()
            if record is None:
                raise KMSUnavailable("OpenBao key set is missing")
            document = client.unwrap(record)
            keys = parse_keyset(document)
            if args.command in {"rotate", "stage"}:
                if key_id in keys.keys:
                    raise ValueError("Key ID already exists")
                document["keys"].append(generate_key(key_id))
                if args.command == "rotate":
                    document["active_key_id"] = key_id
            else:
                if not args.key_id or args.key_id not in keys.keys:
                    raise ValueError("An existing --key-id is required")
                if args.command == "activate":
                    document["active_key_id"] = args.key_id
                else:
                    if args.key_id == keys.active_key_id:
                        raise ValueError("The active key cannot be retired")
                    document["keys"] = [k for k in document["keys"] if k["kid"] != args.key_id]
            client.store_keyset(document, record["metadata"]["version"])
        elif args.command == "rotate-storage":
            validate_storage_key(client)
            client.request(
                "POST",
                f"{settings.openbao_transit_mount}/keys/{settings.openbao_storage_key}/rotate",
                {},
            )
        elif args.command == "rotate-kek":
            client.request(
                "POST",
                f"{settings.openbao_transit_mount}/keys/{settings.openbao_transit_key}/rotate",
                {},
            )
            record = client.read_wrapped()
            if record is None:
                raise KMSUnavailable("OpenBao key set is missing")
            response = client.request(
                "POST",
                f"{settings.openbao_transit_mount}/rewrap/{settings.openbao_transit_key}",
                {"ciphertext": record["data"]["ciphertext"]},
            )
            ciphertext = (response or {}).get("data", {}).get("ciphertext")
            if not isinstance(ciphertext, str) or not ciphertext.startswith("vault:v"):
                raise KMSUnavailable("Invalid OpenBao rewrap response")
            client.request(
                "POST",
                client.kv_path,
                {
                    "options": {"cas": record["metadata"]["version"]},
                    "data": {"ciphertext": ciphertext},
                },
            )
    print(f"OpenBao {args.command} completed; no private key material was written to disk")


if __name__ == "__main__":
    main()
