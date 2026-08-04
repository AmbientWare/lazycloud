variable "tailnet_id" {
  description = "ID of the dedicated hosted tailnet. Do not use a personal or shared tailnet."
  type        = string

  validation {
    condition     = length(trimspace(var.tailnet_id)) > 0
    error_message = "tailnet_id must identify the dedicated hosted tailnet."
  }
}

variable "confirm_dedicated_tailnet" {
  description = "Explicit confirmation that tailnet_id is an isolated tailnet dedicated to this deployment."
  type        = bool
  default     = false
}

variable "overwrite_existing_policy" {
  description = "Replace the tailnet's complete existing policy. Set only after reviewing the plan for a dedicated tailnet."
  type        = bool
  default     = false
}

variable "environment" {
  description = "Short environment name used in credential descriptions."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,30}$", var.environment))
    error_message = "environment must be 2-31 lowercase alphanumeric characters starting with a letter."
  }
}

variable "agent_tag" {
  description = "Tag assigned only to durable customer-compute machine identities."
  type        = string
  default     = "tag:lazycloud-agent"

  validation {
    condition     = can(regex("^tag:[a-z][a-z0-9-]{1,30}$", var.agent_tag))
    error_message = "agent_tag must use the tag:<lowercase-name> form."
  }
}

variable "control_plane_tag" {
  description = "Tag assigned only to ephemeral control-plane gateway identities."
  type        = string
  default     = "tag:lazycloud-control-plane"

  validation {
    condition     = can(regex("^tag:[a-z][a-z0-9-]{1,30}$", var.control_plane_tag))
    error_message = "control_plane_tag must use the tag:<lowercase-name> form."
  }
}

variable "agent_proxy_port" {
  description = "TCP port exposed by each customer-compute machine route proxy."
  type        = number
  default     = 29443

  validation {
    condition     = var.agent_proxy_port >= 1024 && var.agent_proxy_port <= 65535
    error_message = "agent_proxy_port must be an unprivileged TCP port."
  }
}

variable "control_plane_port" {
  description = "TCP port the control plane serves to agents and workers over the tailnet."
  type        = number
  default     = 9000

  validation {
    condition     = var.control_plane_port >= 1024 && var.control_plane_port <= 65535
    error_message = "control_plane_port must be an unprivileged TCP port."
  }
}

variable "gateway_auth_key_expiry_seconds" {
  description = "Lifetime of the reusable gateway enrollment key. Rotate the deployed secret before this expires."
  type        = number
  default     = 7776000

  validation {
    condition = (
      var.gateway_auth_key_expiry_seconds >= 86400 &&
      var.gateway_auth_key_expiry_seconds <= 7776000
    )
    error_message = "gateway_auth_key_expiry_seconds must be between one and 90 days."
  }
}
