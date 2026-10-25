variable "name" { type = string }
variable "region" { type = string }
variable "domain" { type = string }
variable "address" { type = string }
variable "services" { type = map(string) }

resource "google_compute_region_network_endpoint_group" "run" {
  for_each              = var.services
  name                  = "${var.name}-${each.key}"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = each.value
  }
}

resource "google_compute_security_policy" "edge" {
  name = var.name
  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "Baseline policy; add workload-specific rate limits after measuring device bursts."
  }
}

resource "google_compute_backend_service" "run" {
  for_each                = var.services
  name                    = "${var.name}-${each.key}"
  protocol                = "HTTP"
  load_balancing_scheme   = "EXTERNAL_MANAGED"
  security_policy         = google_compute_security_policy.edge.id
  enable_cdn              = false
  custom_response_headers = ["Strict-Transport-Security:max-age=31536000"]
  custom_request_headers = each.key == "backend" ? [
    "X-Ermes-Client-IP:{client_ip_address}",
    "X-Ermes-Edge-IP:${var.address}",
    "X-Forwarded-Proto:https",
  ] : []
  backend {
    group = google_compute_region_network_endpoint_group.run[each.key].id
  }
  log_config {
    enable      = true
    sample_rate = 0.1
  }
}

resource "google_compute_url_map" "https" {
  name            = var.name
  default_service = google_compute_backend_service.run["frontend"].id
  host_rule {
    hosts        = [var.domain]
    path_matcher = "application"
  }
  path_matcher {
    name            = "application"
    default_service = google_compute_backend_service.run["frontend"].id
    path_rule {
      paths = [
        "/api", "/api/*", "/docs", "/docs/*", "/redoc", "/redoc/*",
        "/openapi.json", "/health",
      ]
      service = google_compute_backend_service.run["backend"].id
    }
  }
}

resource "google_compute_managed_ssl_certificate" "main" {
  name = var.name
  managed {
    domains = [var.domain]
  }
}

resource "google_compute_ssl_policy" "main" {
  name            = var.name
  min_tls_version = "TLS_1_2"
  profile         = "MODERN"
}

resource "google_compute_target_https_proxy" "main" {
  name             = var.name
  url_map          = google_compute_url_map.https.id
  ssl_certificates = [google_compute_managed_ssl_certificate.main.id]
  ssl_policy       = google_compute_ssl_policy.main.id
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.name}-https"
  target                = google_compute_target_https_proxy.main.id
  ip_address            = var.address
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

resource "google_compute_url_map" "redirect" {
  name = "${var.name}-redirect"
  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "PERMANENT_REDIRECT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  name    = "${var.name}-redirect"
  url_map = google_compute_url_map.redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  name                  = "${var.name}-http"
  target                = google_compute_target_http_proxy.redirect.id
  ip_address            = var.address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}
