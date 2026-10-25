"""Administrative one-time provisioning; never writes private EC keys to disk."""

import argparse
import json
from uuid import uuid4

from google.cloud import secretmanager
from joserfc.jwk import ECKey


def generate_keyset() -> bytes:
    key = ECKey.generate_key("P-384", parameters={"kid": uuid4().hex, "use": "enc"})
    document = {"active_key_id": key.kid, "keys": [key.as_dict(private=True)]}
    return json.dumps(document).encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret", required=True, help="projects/PROJECT/secrets/SECRET")
    arguments = parser.parse_args()
    with secretmanager.SecretManagerServiceClient() as client:
        version = client.add_secret_version(
            request={
                "parent": arguments.secret,
                "payload": {"data": generate_keyset()},
            }
        )
    print(f"Created EC key-set version: {version.name}")


if __name__ == "__main__":
    main()
