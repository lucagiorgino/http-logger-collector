variable "name" { type = string }
variable "region" { type = string }
variable "image" { type = string }
variable "port" { type = number }
variable "args" { type = list(string) }
variable "environment" { type = map(string) }
variable "service_account" { type = string }
variable "network" { type = string }
variable "subnet" { type = string }
variable "min_instances" { type = number }
variable "max_instances" { type = number }
variable "worker" { type = bool }
variable "deletion_protection" { type = bool }
variable "secrets" {
  type = map(object({
    secret  = string
    version = string
  }))
}

resource "google_cloud_run_v2_service" "main" {
  name                = var.name
  location            = var.region
  deletion_protection = var.deletion_protection
  ingress             = var.worker ? "INGRESS_TRAFFIC_INTERNAL_ONLY" : "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = var.service_account
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "60s"
    max_instance_request_concurrency = var.worker ? 1 : 40
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = var.network
        subnetwork = var.subnet
      }
    }
    containers {
      image   = var.image
      command = length(var.args) > 0 ? ["uvicorn"] : null
      args    = length(var.args) > 0 ? var.args : null
      ports {
        container_port = var.port
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = !var.worker
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = var.environment
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 40
        tcp_socket {
          port = var.port
        }
      }
      liveness_probe {
        period_seconds    = 30
        timeout_seconds   = 5
        failure_threshold = 3
        http_get {
          path = var.port == 3000 ? "/" : "/health"
          port = var.port
        }
      }
    }
  }
}

# Browsers and agents use Auth0, not Google IAM tokens.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.worker ? 0 : 1
  location = var.region
  name     = google_cloud_run_v2_service.main.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "name" { value = google_cloud_run_v2_service.main.name }

output "runtime_policy" {
  description = "Effective ingress, scaling and invoker settings."
  value = {
    ingress         = google_cloud_run_v2_service.main.ingress
    cpu_idle        = google_cloud_run_v2_service.main.template[0].containers[0].resources[0].cpu_idle
    minimum         = google_cloud_run_v2_service.main.template[0].scaling[0].min_instance_count
    maximum         = google_cloud_run_v2_service.main.template[0].scaling[0].max_instance_count
    public_invokers = google_cloud_run_v2_service_iam_member.public[*].member
  }
}
