"""Initialize only empty Terraform-managed secret containers, without rotating existing values."""

import argparse
import getpass
import os
import re
import secrets
import stat
import sys
import warnings
from pathlib import Path

import google_crc32c
from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.auth.exceptions import GoogleAuthError
from google.cloud import secretmanager

from .runtime.provision_keyset import generate_keyset

SECRET_SUFFIXES = {
    "mongo_uri": "mongo-uri",
    "ec_keyset": "ec-keyset",
    "auth0_client_secret": "auth0-client-secret",
    "auth0_session": "auth0-session",
}
MAX_SECRET_BYTES = 65_536


class BootstrapError(RuntimeError):
    pass


def existing_version(client: secretmanager.SecretManagerServiceClient, name: str) -> str | None:
    try:
        client.get_secret(request={"name": name}, retry=None, timeout=10)
        versions = list(
            client.list_secret_versions(request={"parent": name}, retry=None, timeout=10)
        )
    except NotFound:
        raise BootstrapError(f"{name} is missing; apply the Terraform foundation first.") from None
    if not versions:
        return None
    latest = max(versions, key=lambda version: int(version.name.rsplit("/", 1)[-1]))
    if latest.state != secretmanager.SecretVersion.State.ENABLED:
        raise BootstrapError(
            f"{name} has a disabled or destroyed newest version; resolve it explicitly. "
            "Bootstrap will not regenerate or restore existing secrets."
        )
    return latest.name.rsplit("/", 1)[-1]


def read_input(label: str, path: Path | None) -> bytes:
    try:
        if path is not None:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise BootstrapError(
                        f"{label} input must be a private regular file (chmod 600)."
                    )
                value = source.read(MAX_SECRET_BYTES + 1)
                if len(value) > MAX_SECRET_BYTES:
                    raise BootstrapError(f"{label} input exceeds {MAX_SECRET_BYTES} bytes.")
                value = value.rstrip(b"\r\n")
                value.decode("utf-8")
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(f"{label}: ").encode()
    except (OSError, UnicodeError, EOFError, getpass.GetPassWarning):
        raise BootstrapError(
            f"Cannot securely read {label}; use a hidden terminal prompt or a private input file."
        ) from None
    if not value.strip() or len(value) > MAX_SECRET_BYTES:
        raise BootstrapError(f"{label} must contain 1-{MAX_SECRET_BYTES} UTF-8 bytes.")
    return value


def bootstrap(
    client: secretmanager.SecretManagerServiceClient,
    project: str,
    environment: str,
    *,
    mongo_uri_file: Path | None = None,
    auth0_client_secret_file: Path | None = None,
) -> dict[str, str]:
    names = {
        key: f"projects/{project}/secrets/logger-{environment}-{suffix}"
        for key, suffix in SECRET_SUFFIXES.items()
    }
    versions = {key: existing_version(client, name) for key, name in names.items()}
    inputs = {}
    for key, label, path in (
        ("mongo_uri", "Cloud MongoDB URI", mongo_uri_file),
        ("auth0_client_secret", "Auth0 frontend client secret", auth0_client_secret_file),
    ):
        if versions[key] is None:
            inputs[key] = read_input(label, path)

    result = {}
    for key, name in names.items():
        version_number = versions[key]
        if version_number is None:
            # Recheck after prompting so a completed bootstrap is not inadvertently rotated.
            version_number = existing_version(client, name)
        if version_number is None:
            if key == "ec_keyset":
                value = generate_keyset()
            elif key == "auth0_session":
                value = secrets.token_hex(32).encode()
            else:
                value = inputs[key]
            version = client.add_secret_version(
                request={
                    "parent": name,
                    "payload": {"data": value, "data_crc32c": google_crc32c.value(value)},
                },
                retry=None,
                timeout=10,
            )
            if not version.client_specified_payload_checksum:
                raise BootstrapError(
                    f"{name} creation did not confirm payload integrity; inspect before retrying."
                )
            version_number = version.name.rsplit("/", 1)[-1]
            print(f"Created {name}/versions/{version_number}", file=sys.stderr)
        else:
            print(f"Preserved {name}/versions/{version_number}", file=sys.stderr)
        result[key] = version_number
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--environment", default="production")
    parser.add_argument("--mongo-uri-file", type=Path)
    parser.add_argument("--auth0-client-secret-file", type=Path)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9-]+", arguments.project):
        parser.error("--project must be a GCP project ID or number")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,11}", arguments.environment):
        parser.error("--environment must match Terraform's 1-12 character environment identifier")
    try:
        with secretmanager.SecretManagerServiceClient() as client:
            versions = bootstrap(
                client,
                arguments.project,
                arguments.environment,
                mongo_uri_file=arguments.mongo_uri_file,
                auth0_client_secret_file=arguments.auth0_client_secret_file,
            )
    except BootstrapError as error:
        parser.exit(1, f"Secret bootstrap failed: {error}\n")
    except (GoogleAPICallError, GoogleAuthError):
        parser.exit(
            1,
            "Secret bootstrap failed: check ADC, Secret Manager IAM and Terraform foundation "
            "resources. Rerun to resume missing values; no existing secrets are rotated.\n",
        )
    print("secret_versions = {")
    for key, version in versions.items():
        print(f'  {key} = "{version}"')
    print("}")


if __name__ == "__main__":
    main()
