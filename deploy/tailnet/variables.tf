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

variable "control_plane_service" {
  description = "Tailscale Service the control plane advertises, and the address agents dial."
  type        = string
  default     = "svc:lazycloud-control-plane"

  validation {
    condition     = startswith(var.control_plane_service, "svc:") && length(var.control_plane_service) > 4
    error_message = "control_plane_service must use the svc:<name> form."
  }
}

# Whether the control plane serves pod ingress, which decides whether the
# service may require that port.
#
# A Tailscale service distributes its address only to hosts serving every port
# it declares. Declaring one the deployment does not serve leaves the service
# advertised, granted, resolvable, and reachable by nobody: the console reports
# "advertising the service, but some required ports are missing", and no peer
# ever receives the VIP. Nothing at either end reports an error, because at
# either end nothing is wrong.
#
# This has to agree with LAZYCLOUD_TCP_INGRESS_ENABLED. They are two statements
# of one fact and the failure when they disagree is silent.
variable "tcp_ingress_enabled" {
  description = "Whether the control plane serves the SNI-routed TCP ingress port. Must match LAZYCLOUD_TCP_INGRESS_ENABLED."
  type        = bool
  default     = false
}

variable "tcp_ingress_port" {
  description = "TCP port the control plane serves pod ingress on, routed by SNI."
  type        = number
  default     = 1995

  validation {
    condition     = var.tcp_ingress_port >= 1024 && var.tcp_ingress_port <= 65535
    error_message = "tcp_ingress_port must be an unprivileged TCP port."
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

variable "magic_dns" {
  description = "Whether MagicDNS resolves tailnet names. The control-plane service address depends on it."
  type        = bool
  default     = true
}

variable "dns_search_paths" {
  description = "Search domains for split DNS. Empty when no restricted nameservers are configured."
  type        = list(string)
  default     = []
}

variable "devices_approval_on" {
  description = "Whether a new device waits for an admin before joining."
  type        = bool
  default     = false
}

variable "devices_auto_updates_on" {
  description = "Whether devices update their Tailscale client automatically."
  type        = bool
  default     = true
}

variable "devices_key_duration_days" {
  description = "Lifetime of a device key before it must be renewed."
  type        = number
  default     = 180

  validation {
    condition     = var.devices_key_duration_days >= 1 && var.devices_key_duration_days <= 180
    error_message = "devices_key_duration_days must be between one and 180 days."
  }
}

variable "users_approval_on" {
  description = "Whether a new user waits for an admin before joining."
  type        = bool
  default     = true
}
