output "public_url" {
  description = "Frontend Cloud Run HTTPS origin; available before services are deployed."
  value       = local.frontend_url
}

output "backend_url" {
  description = "API Cloud Run HTTPS origin for agents, API docs and frontend server-side requests."
  value       = local.backend_url
}

output "egress_ip" {
  description = "Allowlist this /32 in Atlas."
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

output "spool_environment" {
  description = "GCS/PubSub ingestion connection settings; no credentials."
  value       = local.spool_environment
}
