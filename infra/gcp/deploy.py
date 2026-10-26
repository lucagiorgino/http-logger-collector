"""Rerunnable GCP deployment using gcloud, Docker, Terraform and missing-only secret bootstrap."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import dotenv_values
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError
from google.cloud import secretmanager

from infra.gcp import bootstrap_secrets

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra/gcp"
GENERATED = "deployment.auto.tfvars.json"
ROLES = ("backend", "frontend", "spool-parse", "spool-load")


class DeploymentError(RuntimeError):
    pass


def run(
    arguments: list[str],
    label: str,
    *,
    cwd: Path = ROOT,
    stdin: str | None = None,
    show: bool = False,
) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TF_VAR_", "TF_CLI_ARGS")) and key != "TF_WORKSPACE"
    }
    environment.update(TF_IN_AUTOMATION="1", TF_WORKSPACE="default")
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise DeploymentError(
            f"{label} failed (exit {result.returncode}). Check authentication, IAM and tool "
            "configuration; command diagnostics are withheld to avoid exposing credentials."
        )
    if show:
        print(result.stdout, end="")
    return result.stdout.strip()


def private_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def local_environment(arguments: argparse.Namespace) -> dict[str, str]:
    backend = dotenv_values(arguments.env_file)
    frontend = dotenv_values(arguments.frontend_env_file)
    for key in ("AUTH0_DOMAIN", "AUTH0_AUDIENCE"):
        if backend.get(key) and frontend.get(key) and backend[key] != frontend[key]:
            raise DeploymentError(f"{key} differs between backend and frontend env files.")
    allowed = (
        "GCP_PROJECT_ID",
        "GCP_MONGO_URI",
        "AUTH0_DOMAIN",
        "AUTH0_AUDIENCE",
        "AUTH0_CLIENT_ID",
        "AUTH0_CLIENT_SECRET",
        "MONGO_URI",
    )
    return {
        key: value
        for key in allowed
        if (value := os.environ.get(key) or frontend.get(key) or backend.get(key))
    }


def configuration(arguments: argparse.Namespace, values: dict[str, str]) -> dict:
    """Let Terraform evaluate HCL in a providerless directory, without cloud/state access."""
    variables = (INFRA / "variables.tf").read_text()
    names = re.findall(r'variable "([^"]+)"', variables)
    defaults = {
        "project_id": values.get("GCP_PROJECT_ID", ""),
        "auth0_domain": values.get("AUTH0_DOMAIN", ""),
        "auth0_audience": values.get("AUTH0_AUDIENCE", ""),
        "auth0_client_id": values.get("AUTH0_CLIENT_ID", ""),
    }
    overrides = {
        key: value
        for key in ("project_id", "region", "environment")
        if (value := getattr(arguments, key))
    }
    with tempfile.TemporaryDirectory(prefix="logger-config-") as directory:
        temporary = Path(directory)
        (temporary / "variables.tf").write_text(variables)
        private_json(temporary / "defaults.auto.tfvars.json", defaults)
        private_json(temporary / "overrides.json", overrides)
        files = [
            path
            for name in ("terraform.tfvars", "terraform.tfvars.json")
            if (path := INFRA / name).exists()
        ]
        files += sorted(path for path in INFRA.glob("*.auto.tfvars*") if path.name != GENERATED)
        expression = "jsonencode({" + ",".join(f"{key}=var.{key}" for key in names) + "})"
        output = run(
            [
                "terraform",
                "console",
                "-no-color",
                *(f"-var-file={path}" for path in files),
                f"-var-file={temporary / 'overrides.json'}",
            ],
            "Evaluate Terraform configuration",
            cwd=temporary,
            stdin=expression + "\n",
        )
        settings = json.loads(json.loads(output))
    previous = INFRA / GENERATED
    if previous.exists():
        saved = json.loads(previous.read_text())
        for key in ("project_id", "region", "environment"):
            if saved[key] != settings[key]:
                raise DeploymentError(
                    f"{key} differs from the previous deployment. Use a separate checkout/state "
                    "for another environment; do not repoint an existing deployment."
                )
        for key in (
            "deploy_services",
            "backend_image_tag",
            "frontend_image_tag",
            "secret_versions",
        ):
            settings[key] = saved[key]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", settings["project_id"]):
        raise DeploymentError("Set project_id in terraform.tfvars, GCP_PROJECT_ID, or --project.")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", settings["region"]):
        raise DeploymentError(
            "Use a GCP region, not a zone: for example --region=europe-west2, not europe-west2-a."
        )
    if any(
        not re.fullmatch(r"[1-9][0-9]*", value) for value in settings["secret_versions"].values()
    ):
        raise DeploymentError(
            "Secret versions must be explicit positive numeric pins, never latest."
        )
    for variable, key in (
        ("auth0_domain", "AUTH0_DOMAIN"),
        ("auth0_audience", "AUTH0_AUDIENCE"),
        ("auth0_client_id", "AUTH0_CLIENT_ID"),
    ):
        if not settings[variable] or settings[variable].startswith("your-"):
            raise DeploymentError(f"Configure {variable} in tfvars or the frontend env file.")
        if values.get(key) and settings[variable] != values[key]:
            raise DeploymentError(
                f"{variable} differs from local {key}; align the configuration before "
                "reusing frontend credentials."
            )
    return settings


class Deployment:
    def __init__(self, arguments: argparse.Namespace, settings: dict):
        self.arguments = arguments
        self.settings = settings
        self.gcloud = arguments.gcloud
        self.project = settings["project_id"]
        self.region = settings["region"]
        self.environment = settings["environment"]
        self.bucket = arguments.state_bucket or f"{self.project}-logger-tfstate"
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,220}[a-z0-9]", self.bucket):
            raise DeploymentError("Invalid --state-bucket name.")
        self.prefix = f"logger/{self.environment}"

    def cloud(self, *arguments: str) -> list[str]:
        return [self.gcloud, *arguments, f"--project={self.project}", "--quiet"]

    def status(self) -> dict:
        run(self.cloud("auth", "print-access-token"), "Check gcloud authentication")
        billing = json.loads(
            run(
                self.cloud("billing", "projects", "describe", self.project, "--format=json"),
                "Check project billing",
            )
        )
        if not billing.get("billingEnabled"):
            raise DeploymentError("The selected project must have billing enabled.")
        enabled = run(
            self.cloud("services", "list", "--enabled", "--format=value(config.name)"),
            "Inspect enabled APIs",
        ).splitlines()
        services = []
        if "run.googleapis.com" in enabled:
            services = json.loads(
                run(
                    self.cloud(
                        "run", "services", "list", f"--region={self.region}", "--format=json"
                    ),
                    "Inspect Cloud Run services",
                )
            )
        expected = {f"logger-{self.environment}-{role}" for role in ROLES}
        existing = {
            service["metadata"]["name"]: service
            for service in services
            if service["metadata"]["name"] in expected
        }
        print(f"Project: {self.project}; region: {self.region}; environment: {self.environment}")
        for name in sorted(expected):
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in existing.get(name, {}).get("status", {}).get("conditions", [])
            )
            print(
                f"  {name}: {'ready' if ready else 'not ready' if name in existing else 'absent'}"
            )
        try:
            run(self.cloud("auth", "application-default", "print-access-token"), "Check ADC")
        except DeploymentError:
            raise DeploymentError(
                "Cloud status was read using gcloud, but Application Default Credentials "
                "are unavailable. Run gcloud auth application-default login, then "
                f"gcloud auth application-default set-quota-project {self.project}."
            ) from None
        return existing

    def initialize_state(self) -> None:
        backend = INFRA / ".terraform/terraform.tfstate"
        if os.environ.get("TF_DATA_DIR"):
            raise DeploymentError("Unset TF_DATA_DIR; this command uses infra/gcp/.terraform.")
        workspace = INFRA / ".terraform/environment"
        if workspace.exists() and workspace.read_text().strip() != "default":
            raise DeploymentError(
                "Select the default Terraform workspace before using this script."
            )
        if backend.exists():
            current = json.loads(backend.read_text()).get("backend", {})
            config = current.get("config", {})
            if current.get("type") != "gcs" or any(
                config.get(key) != value
                for key, value in (("bucket", self.bucket), ("prefix", self.prefix))
            ):
                raise DeploymentError(
                    "Existing Terraform backend differs. Migrate/review state manually first; "
                    "the script will not move or overwrite it."
                )
        if (INFRA / "terraform.tfstate").exists():
            raise DeploymentError("Local Terraform state exists; migrate it manually first.")
        run(
            self.cloud(
                "services",
                "enable",
                "serviceusage.googleapis.com",
                "cloudresourcemanager.googleapis.com",
                "storage.googleapis.com",
            ),
            "Enable state prerequisites",
        )
        buckets = json.loads(
            run(
                self.cloud(
                    "storage", "buckets", "list", f"--filter=name:{self.bucket}", "--format=json"
                ),
                "Inspect state bucket",
            )
        )
        matches = [
            bucket
            for bucket in buckets
            if bucket.get("name", "").removeprefix("gs://").rstrip("/") == self.bucket
        ]
        if not matches:
            run(
                self.cloud(
                    "storage",
                    "buckets",
                    "create",
                    f"gs://{self.bucket}",
                    f"--location={self.region}",
                    "--uniform-bucket-level-access",
                    "--public-access-prevention",
                ),
                "Create state bucket",
            )
        run(
            self.cloud(
                "storage",
                "buckets",
                "update",
                f"gs://{self.bucket}",
                "--versioning",
                "--uniform-bucket-level-access",
                "--public-access-prevention",
            ),
            "Protect state bucket",
        )
        run(
            [
                "terraform",
                f"-chdir={INFRA}",
                "init",
                "-input=false",
                "-reconfigure",
                f"-backend-config=bucket={self.bucket}",
                f"-backend-config=prefix={self.prefix}",
            ],
            "Initialize remote Terraform state",
            show=True,
        )

    def apply(self, settings: dict, label: str) -> None:
        private_json(INFRA / GENERATED, settings)
        with tempfile.TemporaryDirectory(prefix="logger-plan-") as directory:
            plan = str(Path(directory) / "deployment.tfplan")
            run(
                [
                    "terraform",
                    f"-chdir={INFRA}",
                    "plan",
                    "-input=false",
                    "-no-color",
                    f"-var-file={INFRA / GENERATED}",
                    f"-out={plan}",
                ],
                f"Plan {label}",
                show=True,
            )
            document = json.loads(
                run(
                    ["terraform", f"-chdir={INFRA}", "show", "-json", plan],
                    "Inspect deployment plan",
                )
            )
            if any(
                "delete" in change["change"]["actions"]
                for change in document.get("resource_changes", [])
            ):
                raise DeploymentError(
                    "Plan contains deletion/replacement. Refusing automatic apply; review "
                    "the configuration and use the manual procedure."
                )
            if not self.arguments.yes and input(f"Apply {label}? [y/N] ").strip().lower() != "y":
                raise DeploymentError("Deployment cancelled; no plan was applied.")
            run(
                ["terraform", f"-chdir={INFRA}", "apply", "-input=false", "-no-color", plan],
                f"Apply {label}",
                show=True,
            )

    def outputs(self) -> dict:
        values = json.loads(
            run(
                ["terraform", f"-chdir={INFRA}", "output", "-json"],
                "Read deployment outputs",
            )
        )
        return {key: entry["value"] for key, entry in values.items()}

    def images(self, outputs: dict) -> dict[str, str]:
        release = self.arguments.release
        if not release:
            release = (
                "release-"
                + time.strftime("%Y%m%d%H%M%S", time.gmtime())
                + "-"
                + os.urandom(3).hex()
            )
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", release):
            raise DeploymentError("Invalid --release Docker tag.")
        run(
            [self.gcloud, "auth", "configure-docker", f"{self.region}-docker.pkg.dev", "--quiet"],
            "Configure Artifact Registry authentication",
        )
        for role, image, dockerfile in (
            ("backend", "api", "infra/gcp/runtime/Dockerfile"),
            ("frontend", "dashboard", "docker/frontend.Dockerfile"),
        ):
            repository = outputs["image_repositories"][role]
            existing = json.loads(
                run(
                    self.cloud(
                        "artifacts",
                        "docker",
                        "tags",
                        "list",
                        repository,
                        "--format=json",
                    ),
                    f"Inspect {role} image tags",
                )
            )
            tag = f"{repository}/{image}:{release}"
            if any(
                entry.get("image") == f"{repository}/{image}"
                and entry.get("tag", "").rsplit("/", 1)[-1] == release
                for entry in existing
            ):
                print(f"Preserving published immutable {role} image: {tag}")
                continue
            run(
                ["docker", "build", "--platform=linux/amd64", "-f", dockerfile, "-t", tag, "."],
                f"Build {role} image",
                show=True,
            )
            run(["docker", "push", tag], f"Push {role} image", show=True)
        return {"backend_image_tag": release, "frontend_image_tag": release}

    def deploy(self, values: dict[str, str]) -> None:
        existing = self.status()
        if self.arguments.check:
            print("Read-only check complete; no cloud resources were changed.")
            return
        if (
            not self.arguments.yes
            and input("Create/update billed GCP resources and publish this checkout? [y/N] ")
            .strip()
            .lower()
            != "y"
        ):
            raise DeploymentError("Deployment cancelled.")
        run(["docker", "info", "--format={{.ServerVersion}}"], "Check Docker daemon")
        self.initialize_state()
        addresses = run(
            ["terraform", f"-chdir={INFRA}", "state", "list"],
            "Inspect Terraform ownership",
        ).splitlines()
        managed = [address for address in addresses if address.startswith("module.service[")]
        if existing and not managed:
            raise DeploymentError(
                "Cloud Run services exist outside this Terraform state. Import/recover their "
                "state manually before deploying; the script will not adopt them implicitly."
            )
        foundation = dict(self.settings)
        foundation["deploy_services"] = bool(managed)
        self.apply(foundation, "foundation / existing services")
        outputs = self.outputs()
        print(f"Atlas allowlist: {outputs['egress_ip']}/32")
        print(f"Auth0 callback: {outputs['public_url']}/auth/callback")
        print(f"Auth0 logout URL and web origin: {outputs['public_url']}")
        if not self.arguments.external_ready:
            if self.arguments.yes:
                raise DeploymentError(
                    "Foundation is ready. Configure Atlas/Auth0 using the values above, then "
                    "rerun with --yes --external-ready. No services were disabled."
                )
            if (
                input("Are Atlas and Auth0 configured as shown above? [y/N] ").strip().lower()
                != "y"
            ):
                raise DeploymentError(
                    "Stopped before rollout; rerun after configuring Atlas/Auth0."
                )
        versions = initialize_secrets(self, values)
        images = self.images(outputs)
        rollout = {**self.settings, **images, "secret_versions": versions, "deploy_services": True}
        self.apply(rollout, "application rollout")
        verify_services(self, outputs)
        print(f"Frontend: {outputs['public_url']}\nAPI: {outputs['backend_url']}")
        print("Deployment probes passed. Verify sign-in and an authenticated upload round trip.")


def cloud_mongo_uri(value: str) -> str:
    parsed = urlsplit(value)
    options = {key.lower(): values[-1].lower() for key, values in parse_qs(parsed.query).items()}
    authority = parsed.netloc.rsplit("@", 1)[-1]
    hosts = authority.split(",")
    if (
        parsed.scheme not in {"mongodb", "mongodb+srv"}
        or not hosts
        or any(
            not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", host)
            or "." not in host.split(":")[0]
            or host.startswith("127.")
            or host.split(":")[0] == "0.0.0.0"
            or host.split(":")[0].lower().endswith((".localhost", ".local"))
            for host in hosts
        )
        or options.get("directconnection") == "true"
        or any(
            options.get(key) == "true"
            for key in ("tlsinsecure", "tlsallowinvalidcertificates", "tlsallowinvalidhostnames")
        )
        or any(options.get(key) == "false" for key in ("tls", "ssl"))
        or (
            parsed.scheme == "mongodb"
            and (options.get("tls", options.get("ssl")) != "true" or not options.get("replicaset"))
        )
    ):
        raise DeploymentError(
            "A cloud Mongo replica-set URI with verified TLS is required. Local Mongo/emulator "
            "settings cannot be reused; set GCP_MONGO_URI or enter an Atlas URI at the prompt."
        )
    return value


def initialize_secrets(deployment: Deployment, values: dict[str, str]) -> dict[str, str]:
    with secretmanager.SecretManagerServiceClient() as client:
        name = f"projects/{deployment.project}/secrets/logger-{deployment.environment}-mongo-uri"
        inputs = {}
        if bootstrap_secrets.existing_version(client, name) is None:
            candidate = values.get("GCP_MONGO_URI") or values.get("MONGO_URI", "")
            try:
                inputs["mongo_uri"] = cloud_mongo_uri(candidate).encode()
            except DeploymentError:
                if values.get("GCP_MONGO_URI"):
                    raise
                print("Local Mongo configuration is not cloud-ready; a separate URI is required.")
                candidate = bootstrap_secrets.read_input("Cloud MongoDB URI", None).decode()
                inputs["mongo_uri"] = cloud_mongo_uri(candidate).encode()
        if values.get("AUTH0_CLIENT_SECRET"):
            inputs["auth0_client_secret"] = values["AUTH0_CLIENT_SECRET"].encode()
        bootstrap_secrets.bootstrap(
            client,
            deployment.project,
            deployment.environment,
            input_values=inputs,
        )
        versions = deployment.settings["secret_versions"]
        for key, version in versions.items():
            parent = f"projects/{deployment.project}/secrets/logger-{deployment.environment}-"
            secret = parent + bootstrap_secrets.SECRET_SUFFIXES[key]
            record = client.get_secret_version(
                request={"name": f"{secret}/versions/{version}"},
                retry=None,
                timeout=10,
            )
            if record.state != secretmanager.SecretVersion.State.ENABLED:
                raise DeploymentError(
                    "A pinned secret version is not enabled; resolve it explicitly."
                )
        return versions


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def verify_services(deployment: Deployment, outputs: dict) -> None:
    for role in ROLES:
        document = json.loads(
            run(
                deployment.cloud(
                    "run",
                    "services",
                    "describe",
                    f"logger-{deployment.environment}-{role}",
                    f"--region={deployment.region}",
                    "--format=json",
                ),
                f"Verify {role} readiness",
            )
        )
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in document.get("status", {}).get("conditions", [])
        ):
            raise DeploymentError(f"Cloud Run {role} is not ready.")
    opener = build_opener(NoRedirect())
    for url, expected in (
        (outputs["backend_url"] + "/health", 200),
        (outputs["backend_url"] + "/api/v1/me", 401),
        (outputs["public_url"] + "/", 200),
    ):
        status = None
        for attempt in range(6):
            try:
                with opener.open(Request(url), timeout=15) as response:
                    status = response.status
            except HTTPError as error:
                status = error.code
            except (URLError, TimeoutError):
                status = None
            if status == expected:
                break
            if attempt < 5:
                time.sleep(5)
        else:
            raise DeploymentError(
                f"Deployment probe failed: expected HTTP {expected}, got {status}."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only cloud status / preflight.")
    parser.add_argument("--yes", action="store_true", help="Approve nondestructive GCP changes.")
    parser.add_argument("--external-ready", action="store_true", help="Atlas/Auth0 are configured.")
    parser.add_argument("--project", dest="project_id")
    parser.add_argument("--region")
    parser.add_argument("--environment")
    parser.add_argument("--state-bucket")
    parser.add_argument("--release", help="Unique immutable tag; reuse it to resume a release.")
    parser.add_argument("--gcloud", default="gcloud", help="gcloud executable or absolute path.")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--frontend-env-file",
        type=Path,
        default=(
            ROOT / "frontend/.env.local"
            if (ROOT / "frontend/.env.local").exists()
            else ROOT / "frontend/.env"
        ),
    )
    arguments = parser.parse_args()
    try:
        if os.environ.get("TF_DATA_DIR"):
            raise DeploymentError("Unset TF_DATA_DIR; this command uses infra/gcp/.terraform.")
        missing = [
            tool
            for tool in (arguments.gcloud, "terraform", *(() if arguments.check else ("docker",)))
            if shutil.which(tool) is None
        ]
        if missing:
            raise DeploymentError(
                f"Missing executable(s): {', '.join(missing)}. Install/expose them on PATH "
                "or supply --gcloud=/absolute/path/to/gcloud."
            )
        sdk = Path(shutil.which(arguments.gcloud)).resolve().parent
        os.environ["PATH"] = str(sdk) + os.pathsep + os.environ.get("PATH", "")
        values = local_environment(arguments)
        settings = configuration(arguments, values)
        Deployment(arguments, settings).deploy(values)
    except DeploymentError as error:
        parser.exit(1, f"Deployment stopped: {error}\n")
    except bootstrap_secrets.BootstrapError as error:
        parser.exit(1, f"Deployment stopped: {error}\n")
    except (GoogleAPICallError, GoogleAuthError):
        parser.exit(
            1,
            "Deployment stopped: secret initialization failed; check ADC, Secret Manager "
            "IAM and pinned versions. Existing secret values were not replaced.\n",
        )
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "Deployment stopped: interactive input unavailable or cancelled.\n")
    except (OSError, ValueError):
        parser.exit(1, "Deployment stopped: invalid configuration, file access or tool response.\n")


if __name__ == "__main__":
    main()
