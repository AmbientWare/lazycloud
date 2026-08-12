variable "account_id" {
  description = "Cloudflare account that owns the tunnel."
  type        = string

  validation {
    condition     = length(trimspace(var.account_id)) > 0
    error_message = "account_id must identify the Cloudflare account."
  }
}

variable "zone_id" {
  description = "Zone the platform serves from. Must be the zone whose apex is public_hostname."
  type        = string

  validation {
    condition     = length(trimspace(var.zone_id)) > 0
    error_message = "zone_id must identify the platform's zone."
  }
}

variable "public_hostname" {
  description = "Zone apex the platform answers on, such as lazycloud.dev."
  type        = string

  validation {
    condition     = length(trimspace(var.public_hostname)) > 0 && !startswith(var.public_hostname, "*")
    error_message = "public_hostname must be the zone apex, not a wildcard."
  }
}

variable "confirm_zone_records" {
  description = <<-EOT
    Explicit confirmation that this module may write the apex and wildcard records
    in this zone. The zone carries records this module does not own — mail among
    them — and adopting the two it does own is only safe once they have been
    imported and a plan has shown nothing else moves.
  EOT
  type        = bool
  default     = false
}

variable "manage_fallback_origin" {
  description = <<-EOT
    Whether to declare the Cloudflare for SaaS fallback origin. Off by default:
    every active custom hostname resolves through it, so a wrong value takes a
    customer's domain down rather than this platform's.
  EOT
  type        = bool
  default     = false
}

variable "environment" {
  description = "Short environment name used in the tunnel name."
  type        = string
  default     = "production"

  validation {
    condition     = length(trimspace(var.environment)) > 0
    error_message = "environment must name the deployment this tunnel serves."
  }
}
