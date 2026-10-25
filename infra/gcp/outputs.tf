output "public_url" {
  description = "Shared frontend and API HTTPS origin."
  value       = local.origin
}

output "load_balancer_ip" {
  description = "Create a DNS A record for domain pointing here; remove conflicting AAAA records."
  value       = google_compute_global_address.edge.address
}

output "egress_ip" {
  description = "Allowlist this /32 in Atlas and the external Redis provider."
  value       = module.network.egress_ip
}

output "image_repositories" {
  description = "Push api:<tag> to backend and dashboard:<tag> to frontend."
  value       = { for key, repository in google_artifact_registry_repository.images : key => "${local.registry}/${repository.repository_id}" }
}

output "secret_names" {
  description = "Secret containers only; populate versions outside Terraform."
  value       = { for key, secret in google_secret_manager_secret.runtime : key => secret.secret_id }
}

output "storage_kms_key" {
  description = "Protect all versions: older versions are required to decrypt historical records."
  value       = google_kms_crypto_key.storage.id
}

output "service_accounts" {
  value = { for key, account in google_service_account.runtime : key => account.email }
}

output "cloud_run_services" {
  value = { for key, service in module.service : key => service.name }
}

output "memorystore_cache" {
  description = "Optional cache endpoint, not wired into ingestion. Fetch AUTH/CA outside Terraform."
  value = var.enable_memorystore_cache ? {
    host = google_redis_instance.cache[0].host
    port = google_redis_instance.cache[0].port
  } : null
}
