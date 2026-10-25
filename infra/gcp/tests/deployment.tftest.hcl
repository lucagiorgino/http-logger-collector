mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
}
mock_provider "google-beta" {}

variables {
  project_id      = "example-gcp-project"
  auth0_domain    = "tenant.eu.auth0.com"
  auth0_audience  = "https://logs.example.com/api"
  auth0_client_id = "web-client"
}

run "foundation_only" {
  command = plan
  assert {
    condition     = length(module.service) == 0
    error_message = "Bootstrap must not deploy containers before images/secrets exist."
  }
  assert {
    condition     = output.public_url == "https://logger-production-frontend-123456789012.europe-west1.run.app" && output.backend_url == "https://logger-production-backend-123456789012.europe-west1.run.app"
    error_message = "Generated HTTPS URLs must be available before deployment to configure Auth0."
  }
  assert {
    condition     = length(google_service_account.runtime) == 4 && length(google_secret_manager_secret.runtime) == 4
    error_message = "The API, frontend and two workers need four scoped identities and secret containers."
  }
  assert {
    condition     = length(google_pubsub_topic.spool) == 4 && length(google_pubsub_subscription.spool_dlq) == 2
    error_message = "Ingestion transport must exist during foundation provisioning."
  }
  assert {
    condition     = length(google_pubsub_subscription_iam_member.status) == 2 && contains(google_project_iam_custom_role.spool_objects["backend"].permissions, "storage.buckets.get")
    error_message = "Status needs bucket metadata and both subscription metadata views, not subscriber access."
  }
}

run "application" {
  command = plan
  variables {
    deploy_services = true
  }
  assert {
    condition     = length(module.service) == 4 && alltrue([for stage in ["spool-parse", "spool-load"] : module.service[stage].runtime_policy.cpu_idle == false && module.service[stage].runtime_policy.minimum == module.service[stage].runtime_policy.maximum && module.service[stage].runtime_policy.minimum > 0])
    error_message = "Both workers need always-allocated CPU and fixed nonzero replica counts."
  }
  assert {
    condition     = alltrue([for stage in ["spool-parse", "spool-load"] : module.service[stage].runtime_policy.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY" && length(module.service[stage].runtime_policy.public_invokers) == 0])
    error_message = "Consumers must not be publicly invokable."
  }
  assert {
    condition     = alltrue([for stage in ["backend", "frontend"] : module.service[stage].runtime_policy.ingress == "INGRESS_TRAFFIC_ALL" && module.service[stage].runtime_policy.public_invokers == ["allUsers"]])
    error_message = "Browsers and agents must reach the two web services directly using Auth0."
  }
  assert {
    condition     = local.services.frontend.environment.APP_BASE_URL == output.public_url && local.services.frontend.environment.BACKEND_API_URL == output.backend_url && output.public_url != output.backend_url && !contains(keys(local.services.backend.environment), "GCP_EDGE_IP")
    error_message = "The frontend needs its own origin and a distinct API origin without edge headers."
  }
  assert {
    condition     = google_monitoring_uptime_check_config.api[0].monitored_resource[0].labels.host == trimprefix(output.backend_url, "https://")
    error_message = "Liveness monitoring must target the backend Cloud Run hostname."
  }

  assert {
    condition     = google_storage_bucket.spool.soft_delete_policy[0].retention_duration_seconds == 0 && google_storage_bucket.spool.versioning[0].enabled == false
    error_message = "Raw cleanup must not retain soft-deleted or versioned objects."
  }
  assert {
    condition     = local.spool_services["spool-load"].secret_keys == toset(["mongo_uri"]) && !contains(keys(local.spool_services["spool-load"].environment), "GCP_EC_KEYSET")
    error_message = "The loader must receive only Mongo credentials, not upload encryption keys."
  }
  assert {
    condition     = google_pubsub_subscription.spool_silver.message_retention_duration == "1209600s" && google_pubsub_subscription.spool_silver.dead_letter_policy[0].max_delivery_attempts == 5
    error_message = "Silver needs a retained subscription with load-DLQ redrive."
  }
  assert {
    condition     = length(google_pubsub_topic_iam_member.spool_redrive) == 2 && length(google_pubsub_subscription_iam_member.spool) == 2 && length(google_monitoring_alert_policy.spool_lag.conditions) == 2
    error_message = "Both stages need scoped redrive/subscriber IAM and backlog depth/age monitoring."
  }
}

run "custom_location" {
  command = plan
  variables {
    region      = "us-central1"
    environment = "staging"
  }
  assert {
    condition     = output.public_url == "https://logger-staging-frontend-123456789012.us-central1.run.app" && output.backend_url == "https://logger-staging-backend-123456789012.us-central1.run.app"
    error_message = "Direct service URLs must follow the selected region and environment."
  }
}

run "custom_retention" {
  command = plan
  variables {
    spool_message_retention_seconds    = 604800
    spool_oldest_unacked_alert_seconds = 1800
  }
  assert {
    condition     = google_pubsub_subscription.spool_objects.message_retention_duration == "604800s" && google_pubsub_subscription.spool_silver.message_retention_duration == "604800s"
    error_message = "Configured retention must apply to both transport hops."
  }
}
