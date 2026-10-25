variable "project_id" {
  description = "Existing billing-enabled GCP project."
  type        = string
}

variable "region" {
  description = "Cloud Run, registry, KMS and network region."
  type        = string
  default     = "europe-west1"
}

variable "environment" {
  description = "Short environment identifier used in resource names."
  type        = string
  default     = "production"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,11}$", var.environment))
    error_message = "Use 1-12 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "domain" {
  description = "Public hostname without scheme/path; DNS is managed separately."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]*\\.[a-z]{2,}$", var.domain))
    error_message = "Supply a DNS hostname, for example logs.example.com."
  }
}

variable "auth0_domain" {
  description = "Auth0 tenant/custom domain, without https:// or trailing slash."
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$", var.auth0_domain))
    error_message = "Supply an Auth0 hostname without a URL scheme."
  }
}

variable "auth0_audience" {
  description = "Exact existing Auth0 API identifier, identical for frontend and backend."
  type        = string
  validation {
    condition     = length(trimspace(var.auth0_audience)) > 0
    error_message = "The Auth0 API audience must not be empty."
  }
}

variable "auth0_client_id" {
  description = "Frontend Regular Web Application client ID (not a secret)."
  type        = string
}

variable "deploy_services" {
  description = "First apply false; publish images and secret versions, then apply true."
  type        = bool
  default     = false
}

variable "backend_image_tag" {
  description = "Unique immutable backend image tag. Also used by the worker."
  type        = string
  default     = "release-1"
}

variable "frontend_image_tag" {
  description = "Unique immutable frontend image tag."
  type        = string
  default     = "release-1"
}

variable "secret_versions" {
  description = "Pinned numeric Secret Manager versions; values never enter Terraform."
  type = object({
    mongo_uri           = string
    redis_url           = string
    ec_keyset           = string
    auth0_client_secret = string
    auth0_session       = string
  })
  default = {
    mongo_uri           = "1"
    redis_url           = "1"
    ec_keyset           = "1"
    auth0_client_secret = "1"
    auth0_session       = "1"
  }
  validation {
    condition     = alltrue([for version in values(var.secret_versions) : can(regex("^[1-9][0-9]*$", version))])
    error_message = "Pin every secret to a numeric version; latest is deliberately disallowed."
  }
}

variable "mongo_db_name" {
  description = "Fresh environment-specific MongoDB database; do not reuse OpenBao-encrypted data."
  type        = string
  default     = "http_logger_production"
}

variable "subnet_cidr" {
  description = "Dedicated Direct VPC egress subnet, /26 or larger."
  type        = string
  default     = "10.42.0.0/24"
  validation {
    condition     = can(cidrnetmask(var.subnet_cidr)) && try(tonumber(split("/", var.subnet_cidr)[1]) <= 26, false)
    error_message = "Supply an IPv4 subnet with prefix length at most 26."
  }
}

variable "max_instances" {
  description = "Maximum instances for each web service; size MongoDB connection limits accordingly."
  type        = number
  default     = 10
  validation {
    condition     = var.max_instances >= 2 && var.max_instances <= 100 && floor(var.max_instances) == var.max_instances
    error_message = "Use an integer between 2 and 100."
  }
}

variable "worker_instances" {
  description = "Always-on, CPU-allocated worker replicas; not request-driven autoscaling."
  type        = number
  default     = 2
  validation {
    condition     = var.worker_instances >= 1 && var.worker_instances <= 20 && floor(var.worker_instances) == var.worker_instances
    error_message = "Use an integer between 1 and 20."
  }
}

variable "deletion_protection" {
  description = "Protect Cloud Run services from accidental deletion."
  type        = bool
  default     = true
}

variable "enable_memorystore_cache" {
  description = "Optional HA cache ONLY. Memorystore is NOT compatible with this application's durable ingestion queue."
  type        = bool
  default     = false
}

variable "memorystore_size_gb" {
  description = "Memory per node of the optional Standard HA cache."
  type        = number
  default     = 5
  validation {
    condition     = var.memorystore_size_gb >= 5 && floor(var.memorystore_size_gb) == var.memorystore_size_gb
    error_message = "Use a whole number of GiB, at least 5."
  }

}

variable "notification_channels" {
  description = "Existing Cloud Monitoring notification channel resource names for availability alerts."
  type        = list(string)
  default     = []
}
