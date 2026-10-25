mock_provider "google" {}

variables {
  project_id      = "example-gcp-project"
  domain          = "logs.example.com"
  auth0_domain    = "tenant.eu.auth0.com"
  auth0_audience  = "https://logs.example.com/api"
  auth0_client_id = "web-client"
}

run "foundation_only" {
  command = plan
  assert {
    condition     = length(module.service) == 0 && length(module.edge) == 0
    error_message = "Bootstrap must not deploy containers before images/secrets exist."
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.access) == 7
    error_message = "Secret IAM must be limited to the seven explicit workload/secret pairs."
  }
  assert {
    condition     = length(google_redis_instance.cache) == 0
    error_message = "An incompatible Memorystore queue must not be provisioned by default."
  }
}

run "application" {
  command = plan
  variables {
    deploy_services = true
  }
  assert {
    condition     = module.service["worker"].runtime_policy.cpu_idle == false && module.service["worker"].runtime_policy.minimum == module.service["worker"].runtime_policy.maximum
    error_message = "Workers need always-allocated CPU and fixed nonzero replica counts."
  }
  assert {
    condition     = module.service["worker"].runtime_policy.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY" && length(module.service["worker"].runtime_policy.public_invokers) == 0
    error_message = "The consumer must not be publicly invokable."
  }
  assert {
    condition     = module.service["backend"].runtime_policy.ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "The API must not be directly exposed via run.app."
  }
  assert {
    condition     = local.services.frontend.environment.BACKEND_API_URL == "https://logs.example.com"
    error_message = "SSR calls must use the shared origin, without duplicating /api."
  }
}

run "optional_cache" {
  command = plan
  variables {
    enable_memorystore_cache = true
  }
  assert {
    condition     = google_redis_instance.cache[0].tier == "STANDARD_HA" && google_redis_instance.cache[0].transit_encryption_mode == "SERVER_AUTHENTICATION"
    error_message = "The optional cache must use HA and TLS."
  }
}
