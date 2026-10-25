locals {
  name = "logger-${var.environment}"
  apis = toset([
    "artifactregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])
  registry = "${var.region}-docker.pkg.dev/${var.project_id}"
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  for_each      = toset(["backend", "frontend"])
  location      = var.region
  repository_id = "${local.name}-${each.value}"
  format        = "DOCKER"
  description   = "Immutable ${each.value} releases"
  docker_config {
    immutable_tags = true
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.apis]
}

module "network" {
  source      = "./modules/network"
  name        = local.name
  region      = var.region
  subnet_cidr = var.subnet_cidr
  depends_on  = [google_project_service.apis]
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  # Cloud Run's documented deterministic URLs avoid a service/self-URL dependency cycle.
  frontend_url = "https://${local.name}-frontend-${data.google_project.current.number}.${var.region}.run.app"
  backend_url  = "https://${local.name}-backend-${data.google_project.current.number}.${var.region}.run.app"
  common_environment = {
    APP_ENV                   = "production"
    APP_RELOAD                = "false"
    REQUIRE_HTTPS             = "true"
    MONGO_DB_NAME             = var.mongo_db_name
    MONGO_ALLOW_PRIMARY_READS = "false"
  }
  base_services = {
    backend = {
      image = "${local.registry}/${google_artifact_registry_repository.images["backend"].repository_id}/api:${var.backend_image_tag}"
      port  = 9090
      args  = ["cloud_runtime.api:app", "--host", "0.0.0.0", "--port", "9090", "--no-proxy-headers"]
      environment = merge(local.common_environment, local.spool_environment, {
        AUTH0_DOMAIN    = var.auth0_domain
        AUTH0_AUDIENCE  = var.auth0_audience
        GCP_STORAGE_KEY = google_kms_crypto_key.storage.id
        GCP_EC_KEYSET   = "${google_secret_manager_secret.runtime["ec_keyset"].id}/versions/${var.secret_versions.ec_keyset}"
      })
      secret_keys = toset(["mongo_uri"])
      minimum     = 2
      maximum     = var.max_instances
      worker      = false
    }
    frontend = {
      image = "${local.registry}/${google_artifact_registry_repository.images["frontend"].repository_id}/dashboard:${var.frontend_image_tag}"
      port  = 3000
      args  = []
      environment = {
        APP_BASE_URL    = local.frontend_url
        BACKEND_API_URL = local.backend_url
        AUTH0_DOMAIN    = var.auth0_domain
        AUTH0_AUDIENCE  = var.auth0_audience
        AUTH0_CLIENT_ID = var.auth0_client_id
        AUTH0_SCOPE     = "openid profile email manage:devices"
      }
      secret_keys = toset(["auth0_client_secret", "auth0_session"])
      minimum     = 2
      maximum     = var.max_instances
      worker      = false
    }
  }
  services = merge(local.base_services, local.spool_services)
  secret_environment_names = {
    mongo_uri           = "MONGO_URI"
    auth0_client_secret = "AUTH0_CLIENT_SECRET"
    auth0_session       = "AUTH0_SECRET"
  }
}

module "service" {
  source              = "./modules/cloud_run"
  for_each            = var.deploy_services ? local.services : {}
  name                = "${local.name}-${each.key}"
  region              = var.region
  image               = each.value.image
  port                = each.value.port
  args                = each.value.args
  environment         = each.value.environment
  service_account     = google_service_account.runtime[each.key].email
  network             = module.network.network_id
  subnet              = module.network.subnet_id
  min_instances       = each.value.minimum
  max_instances       = each.value.maximum
  worker              = each.value.worker
  deletion_protection = var.deletion_protection
  secrets = {
    for key in each.value.secret_keys : local.secret_environment_names[key] => {
      secret  = google_secret_manager_secret.runtime[key].secret_id
      version = var.secret_versions[key]
    }
  }
  depends_on = [
    google_secret_manager_secret_iam_member.access,
    google_kms_crypto_key_iam_member.backend,
    google_storage_bucket_iam_member.spool,
    google_pubsub_subscription_iam_member.spool,
    google_pubsub_topic_iam_member.spool,
    google_secret_manager_secret_iam_member.spool,
    google_kms_crypto_key_iam_member.spool,
    google_storage_notification.spool,
    google_pubsub_topic_iam_member.spool_redrive,
    google_pubsub_subscription_iam_member.spool_redrive,
  ]
}
