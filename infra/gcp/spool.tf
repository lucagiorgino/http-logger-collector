locals {
  spool_stages = toset(["parse", "load"])
  spool_environment = {
    SPOOL_STORAGE_BACKEND            = "gcs"
    SPOOL_BUCKET                     = google_storage_bucket.spool.name
    SPOOL_EVENT_TOPIC                = google_pubsub_topic.spool["objects"].id
    SPOOL_SUBSCRIPTION               = google_pubsub_subscription.spool_objects.id
    SPOOL_SILVER_TOPIC               = google_pubsub_topic.spool["silver"].id
    SPOOL_SILVER_SUBSCRIPTION        = google_pubsub_subscription.spool_silver.id
    SPOOL_PARSE_DLQ_TOPIC            = google_pubsub_topic.spool["parse-dlq"].id
    SPOOL_LOAD_DLQ_TOPIC             = google_pubsub_topic.spool["load-dlq"].id
    SPOOL_TENANT                     = local.name
    SPOOL_MONGO_DOCUMENTS_PER_SECOND = tostring(var.spool_mongo_documents_per_second)
  }
  spool_services = {
    for stage in local.spool_stages : "spool-${stage}" => {
      image = "${local.registry}/${google_artifact_registry_repository.images["backend"].repository_id}/api:${var.backend_image_tag}"
      port  = 9090
      args  = ["cloud_runtime.spool:app", "--host", "0.0.0.0", "--port", "9090", "--no-proxy-headers"]
      environment = merge(local.common_environment, local.spool_environment, {
        SPOOL_WORKER_STAGE = stage
        }, stage == "parse" ? {
        GCP_STORAGE_KEY = google_kms_crypto_key.storage.id
        GCP_EC_KEYSET   = "${google_secret_manager_secret.runtime["ec_keyset"].id}/versions/${var.secret_versions.ec_keyset}"
      } : {})
      secret_keys = toset(["mongo_uri"])
      minimum     = var.worker_instances
      maximum     = var.worker_instances
      worker      = true
    }
  }
}

resource "google_storage_bucket" "spool" {
  name                        = "${var.project_id}-${local.name}-uploads"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = false
  }
  # Consumption really deletes raw sensitive fields, rather than hiding a retained version.
  soft_delete_policy {
    retention_duration_seconds = 0
  }
  # No age TTL: pending/failed objects remain replayable until explicitly resolved.
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "spool" {
  for_each   = toset(["objects", "silver", "parse-dlq", "load-dlq"])
  name       = "${local.name}-uploads-${each.key}"
  depends_on = [google_project_service.apis]
}

data "google_storage_project_service_account" "spool" {
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic_iam_member" "gcs_events" {
  topic  = google_pubsub_topic.spool["objects"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${data.google_storage_project_service_account.spool.email_address}"
}

resource "google_storage_notification" "spool" {
  bucket             = google_storage_bucket.spool.name
  topic              = google_pubsub_topic.spool["objects"].id
  payload_format     = "JSON_API_V1"
  event_types        = ["OBJECT_FINALIZE"]
  object_name_prefix = "raw/"
  depends_on         = [google_pubsub_topic_iam_member.gcs_events]
}

resource "google_project_service_identity" "spool_pubsub" {
  provider   = google-beta
  project    = var.project_id
  service    = "pubsub.googleapis.com"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "spool_objects" {
  name                       = "${local.name}-uploads"
  topic                      = google_pubsub_topic.spool["objects"].id
  ack_deadline_seconds       = 210
  message_retention_duration = "${var.spool_message_retention_seconds}s"
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  expiration_policy {
    ttl = ""
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.spool["parse-dlq"].id
    max_delivery_attempts = 5 # Pub/Sub minimum; application-level failure retries default to 3.
  }
}

resource "google_pubsub_subscription" "spool_silver" {
  name                       = "${local.name}-uploads-silver"
  topic                      = google_pubsub_topic.spool["silver"].id
  ack_deadline_seconds       = 210
  message_retention_duration = "${var.spool_message_retention_seconds}s"
  expiration_policy {
    ttl = ""
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.spool["load-dlq"].id
    max_delivery_attempts = 5
  }
}

resource "google_pubsub_subscription" "spool_dlq" {
  for_each                   = local.spool_stages
  name                       = "${local.name}-uploads-${each.key}-inspection"
  topic                      = google_pubsub_topic.spool["${each.key}-dlq"].id
  message_retention_duration = "${var.spool_message_retention_seconds}s"
  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_topic_iam_member" "spool_redrive" {
  for_each = local.spool_stages
  topic    = google_pubsub_topic.spool["${each.key}-dlq"].name
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:${google_project_service_identity.spool_pubsub.email}"
}

resource "google_pubsub_subscription_iam_member" "spool_redrive" {
  for_each     = local.spool_stages
  subscription = each.key == "parse" ? google_pubsub_subscription.spool_objects.name : google_pubsub_subscription.spool_silver.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_project_service_identity.spool_pubsub.email}"
}

resource "google_project_iam_custom_role" "spool_objects" {
  for_each = {
    backend     = ["storage.objects.create", "storage.objects.get", "storage.buckets.get"]
    spool-parse = ["storage.objects.get"]
    spool-load  = ["storage.objects.delete"]
  }
  role_id     = "logger_${replace(var.environment, "-", "_")}_${replace(each.key, "-", "_")}_raw"
  title       = "Temporary upload access ${each.key}"
  permissions = each.value
}

resource "google_storage_bucket_iam_member" "spool" {
  for_each = google_project_iam_custom_role.spool_objects
  bucket   = google_storage_bucket.spool.name
  role     = each.value.name
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_pubsub_subscription_iam_member" "spool" {
  for_each     = local.spool_stages
  subscription = each.key == "parse" ? google_pubsub_subscription.spool_objects.name : google_pubsub_subscription.spool_silver.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.runtime["spool-${each.key}"].email}"
}

resource "google_pubsub_subscription_iam_member" "status" {
  for_each = {
    objects = google_pubsub_subscription.spool_objects.name
    silver  = google_pubsub_subscription.spool_silver.name
  }
  subscription = each.value
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${google_service_account.runtime["backend"].email}"
}

resource "google_pubsub_topic_iam_member" "spool" {
  for_each = {
    parse  = { account = "spool-parse", topic = "parse-dlq" }
    load   = { account = "spool-load", topic = "load-dlq" }
    silver = { account = "spool-parse", topic = "silver" }
  }
  topic  = google_pubsub_topic.spool[each.value.topic].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.runtime[each.value.account].email}"
}

resource "google_secret_manager_secret_iam_member" "spool" {
  for_each = {
    parse_mongo = { account = "spool-parse", secret = "mongo_uri" }
    parse_keys  = { account = "spool-parse", secret = "ec_keyset" }
    load_mongo  = { account = "spool-load", secret = "mongo_uri" }
  }
  secret_id = google_secret_manager_secret.runtime[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.value.account].email}"
}

resource "google_kms_crypto_key_iam_member" "spool" {
  crypto_key_id = google_kms_crypto_key.storage.id
  role          = google_project_iam_custom_role.storage.name
  member        = "serviceAccount:${google_service_account.runtime["spool-parse"].email}"
}

resource "google_monitoring_alert_policy" "spool_dlq" {
  for_each              = local.spool_stages
  display_name          = "${local.name} upload ${each.key} DLQ nonempty"
  combiner              = "OR"
  notification_channels = var.notification_channels
  conditions {
    display_name = "Unconsumed dead letters"
    condition_threshold {
      filter          = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND resource.label.subscription_id=\"${google_pubsub_subscription.spool_dlq[each.key].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }

    }
  }
}

resource "google_monitoring_alert_policy" "spool_lag" {
  display_name          = "${local.name} silver transport lag"
  combiner              = "OR"
  notification_channels = var.notification_channels
  conditions {
    display_name = "Silver subscription backlog"
    condition_threshold {
      filter          = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND resource.label.subscription_id=\"${google_pubsub_subscription.spool_silver.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.spool_backlog_alert_batches
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  conditions {
    display_name = "Silver subscription oldest unacknowledged message"
    condition_threshold {
      filter          = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" AND resource.label.subscription_id=\"${google_pubsub_subscription.spool_silver.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.spool_oldest_unacked_alert_seconds
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
}
