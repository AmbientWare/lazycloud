variable "deployment" {
  description = "Name distinguishing this installation. Prefixes every globally-named resource."
  type        = string
  default     = "lazycloud-prod"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.deployment))
    error_message = "deployment must be 3-31 lowercase alphanumeric characters or dashes, starting with a letter."
  }
}

variable "region" {
  description = "Region holding the control plane and the shared fleet."
  type        = string
  default     = "us-east-1"
}

variable "control_plane_cidr" {
  description = "CIDR of the VPC holding the control plane."
  type        = string
  default     = "10.80.0.0/16"
}

variable "control_plane_instance_type" {
  description = "Instance type running the Compose stack. Graviton; the images are multi-arch."
  type        = string
  default     = "t4g.medium"
}

variable "control_plane_root_volume_gib" {
  description = "Root volume size. Holds the image cache and the cache-server content store."
  type        = number
  default     = 100

  validation {
    condition     = var.control_plane_root_volume_gib >= 50
    error_message = "control_plane_root_volume_gib must be at least 50; the cache server alone is sized for 20 GiB of content."
  }
}

variable "operator_ingress_cidrs" {
  description = <<-EOT
    CIDRs permitted to reach the control plane instance directly.

    Empty is the intended value. Public traffic arrives through the Cloudflare
    tunnel, workers arrive over the tailnet, and operators use SSM Session
    Manager, so nothing needs an inbound rule. Anything listed here is a hole
    somebody opened on purpose.
  EOT
  type        = list(string)
  default     = []
}
