resource "google_service_account" "runtime" {
  for_each     = toset(["backend", "frontend", "spool-parse", "spool-load"])
  account_id   = "${startswith(each.key, "spool-") ? substr(local.name, 0, 17) : local.name}-${each.key}"
  display_name = "${each.key} runtime, ${var.environment}"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret" "runtime" {
  for_each  = toset(["mongo_uri", "ec_keyset", "auth0_client_secret", "auth0_session"])
  secret_id = "${local.name}-${replace(each.key, "_", "-")}"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.apis]
}

locals {
  secret_access = {
    backend_mongo_uri            = { account = "backend", secret = "mongo_uri" }
    backend_ec_keyset            = { account = "backend", secret = "ec_keyset" }
    frontend_auth0_client_secret = { account = "frontend", secret = "auth0_client_secret" }
    frontend_auth0_session       = { account = "frontend", secret = "auth0_session" }
  }
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_access
  secret_id = google_secret_manager_secret.runtime[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.value.account].email}"
}

resource "google_kms_key_ring" "storage" {
  name       = "${local.name}-storage"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "storage" {
  name            = "sensitive-fields"
  key_ring        = google_kms_key_ring.storage.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"
  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "storage" {
  role_id = "logger_${replace(var.environment, "-", "_")}_storage"
  title   = "Logger storage encryption ${var.environment}"
  permissions = [
    "cloudkms.cryptoKeys.get",
    "cloudkms.cryptoKeyVersions.useToEncrypt",
    "cloudkms.cryptoKeyVersions.useToDecrypt",
  ]
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key_iam_member" "backend" {
  crypto_key_id = google_kms_crypto_key.storage.id
  role          = google_project_iam_custom_role.storage.name
  member        = "serviceAccount:${google_service_account.runtime["backend"].email}"
}
