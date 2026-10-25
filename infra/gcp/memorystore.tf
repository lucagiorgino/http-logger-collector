# Cache only: snapshots/HA do not satisfy ingestion's per-write WAITAOF contract.
resource "google_compute_global_address" "private_services" {
  count         = var.enable_memorystore_cache ? 1 : 0
  name          = "${local.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = module.network.network_id
}

resource "google_service_networking_connection" "private_services" {
  count                   = var.enable_memorystore_cache ? 1 : 0
  network                 = module.network.network_id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services[0].name]
}

resource "google_redis_instance" "cache" {
  count                   = var.enable_memorystore_cache ? 1 : 0
  name                    = "${local.name}-cache"
  region                  = var.region
  tier                    = "STANDARD_HA"
  memory_size_gb          = var.memorystore_size_gb
  redis_version           = "REDIS_7_2"
  authorized_network      = module.network.network_id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  auth_enabled            = true
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  redis_configs = {
    maxmemory-policy = "noeviction"
  }
  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 3
        minutes = 0
        seconds = 0
        nanos   = 0
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_service_networking_connection.private_services]
}
