variable "name" { type = string }
variable "region" { type = string }
variable "subnet_cidr" { type = string }

resource "google_compute_network" "main" {
  name                    = var.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "run" {
  name                     = "${var.name}-run"
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_address" "egress" {
  name   = "${var.name}-egress"
  region = var.region
}

resource "google_compute_router" "main" {
  name    = var.name
  region  = var.region
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "main" {
  name                               = var.name
  router                             = google_compute_router.main.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.egress.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  enable_dynamic_port_allocation     = true
  min_ports_per_vm                   = 64
  max_ports_per_vm                   = 4096
  subnetwork {
    name                    = google_compute_subnetwork.run.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

output "network_id" { value = google_compute_network.main.id }
output "subnet_id" {
  value      = google_compute_subnetwork.run.id
  depends_on = [google_compute_router_nat.main]
}
output "egress_ip" { value = google_compute_address.egress.address }
