variable "webhook_url" {
  description = "Public HTTPS address of this deployment's POST /webhooks/stripe endpoint."
  type        = string

  validation {
    condition     = startswith(var.webhook_url, "https://") && endswith(var.webhook_url, "/webhooks/stripe")
    error_message = "webhook_url must be an https address ending in /webhooks/stripe."
  }
}

variable "confirm_dedicated_account" {
  description = "Explicit confirmation that this Stripe account belongs to this deployment alone."
  type        = bool
  default     = false
}

variable "environment" {
  description = "Short environment name used in the endpoint description."
  type        = string
  default     = "production"

  validation {
    condition     = length(trimspace(var.environment)) > 0
    error_message = "environment must name the deployment this endpoint serves."
  }
}
