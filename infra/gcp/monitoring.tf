resource "google_logging_project_exclusion" "auth_transactions" {
  name        = "${local.name}-auth-transactions"
  description = "Do not persist OAuth authorization codes/state in platform request URLs."
  filter      = "httpRequest.requestUrl =~ \"/auth/(callback|login|logout)([/?]|$)\""
  depends_on  = [google_project_service.apis]
}

resource "google_monitoring_uptime_check_config" "api" {
  count        = var.deploy_services ? 1 : 0
  display_name = "${local.name} HTTPS API"
  timeout      = "10s"
  period       = "60s"
  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }
  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.domain
    }
  }
  depends_on = [module.edge]
}

resource "google_monitoring_alert_policy" "availability" {
  count                 = var.deploy_services ? 1 : 0
  display_name          = "${local.name} HTTPS availability"
  combiner              = "OR"
  notification_channels = var.notification_channels
  conditions {
    display_name = "HTTPS health check failing for five minutes"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.api[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
      trigger {
        count = 1
      }
    }
  }
  documentation {
    content   = "Check certificate/DNS, Cloud Run startup/liveness, Atlas, external Redis and KMS. /health is process liveness, not a queue-drain or dependency check."
    mime_type = "text/markdown"
  }
}
