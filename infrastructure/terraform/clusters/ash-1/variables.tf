# -----------------------------------------------------------------------------
# Variables for ash-1 cluster
# -----------------------------------------------------------------------------
# These are passed through to the hetzner-cluster module.
# Sensitive values should be provided via environment variables:
#   TF_VAR_hcloud_token
#   TF_VAR_aws_access_key_id
#   TF_VAR_aws_secret_access_key
#   TF_VAR_juicefs_token

# -----------------------------------------------------------------------------
# Hetzner Cloud
# -----------------------------------------------------------------------------

variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  type        = string
  sensitive   = true
}

variable "cluster_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "lazycloud-prod"
}

variable "location" {
  description = "Hetzner Cloud location (ash = Ashburn US)"
  type        = string
  default     = "ash"
}

variable "control_plane_count" {
  description = "Number of control plane nodes (should be odd: 1, 3, 5)"
  type        = number
  default     = 1
}

variable "control_plane_type" {
  description = "Server type for control plane nodes"
  type        = string
  default     = "cpx31"
}

variable "platform_count" {
  description = "Number of platform nodes (fixed, not autoscaled)"
  type        = number
  default     = 2
}

variable "platform_type" {
  description = "Server type for platform nodes"
  type        = string
  default     = "cpx31"
}

variable "sandbox_type" {
  description = "Server type for sandbox nodes (Cluster Autoscaler manages scaling)"
  type        = string
  default     = "ccx33"
}

# -----------------------------------------------------------------------------
# Kubernetes / Talos
# -----------------------------------------------------------------------------

variable "kubernetes_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.32.0"
}

variable "talos_version" {
  description = "Talos Linux version"
  type        = string
  default     = "v1.12.2"
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

variable "network_ipv4_cidr" {
  description = "CIDR for the Hetzner private network"
  type        = string
  default     = "10.0.0.0/16"
}

variable "node_ipv4_cidr" {
  description = "CIDR for the node subnet"
  type        = string
  default     = "10.0.1.0/24"
}

variable "pod_ipv4_cidr" {
  description = "CIDR for pod IPs (Cilium)"
  type        = string
  default     = "10.244.0.0/16"
}

variable "service_ipv4_cidr" {
  description = "CIDR for service IPs"
  type        = string
  default     = "10.96.0.0/12"
}

# -----------------------------------------------------------------------------
# AWS
# -----------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for Secrets Manager"
  type        = string
  default     = "us-east-1"
}

variable "aws_access_key_id" {
  description = "AWS access key (used for JuiceFS S3 + External Secrets Operator)"
  type        = string
  sensitive   = true
}

variable "aws_secret_access_key" {
  description = "AWS secret key (used for JuiceFS S3 + External Secrets Operator)"
  type        = string
  sensitive   = true
}

# -----------------------------------------------------------------------------
# JuiceFS
# -----------------------------------------------------------------------------

variable "juicefs_name" {
  description = "JuiceFS Cloud filesystem name"
  type        = string
  default     = "lazycloud-prod"
}

variable "juicefs_token" {
  description = "JuiceFS Cloud volume token"
  type        = string
  sensitive   = true
}

# -----------------------------------------------------------------------------
# ArgoCD
# -----------------------------------------------------------------------------

variable "argocd_domain" {
  description = "Domain for ArgoCD ingress"
  type        = string
  default     = "argocd.lazycloud.dev"
}

variable "git_repo_url" {
  description = "Git repository URL for ArgoCD"
  type        = string
  default     = "git@github.com:AmbientWare/lazycloud.git"
}

variable "argocd_repo_ssh_key_path" {
  description = "Path to SSH private key file for ArgoCD to access the Git repo"
  type        = string
  default     = "~/.ssh/id_rsa"
}

# -----------------------------------------------------------------------------
# AWS Secrets Manager
# -----------------------------------------------------------------------------

variable "secrets_prefix" {
  description = "Prefix for AWS Secrets Manager secret names"
  type        = string
  default     = "lazycloud"
}

# -----------------------------------------------------------------------------
# Application Configuration
# -----------------------------------------------------------------------------

variable "app_namespaces" {
  description = "Application namespaces to create"
  type = list(object({
    name   = string
    labels = optional(map(string), {})
  }))
  default = [
    {
      name   = "lazycloud-prod"
      labels = { "lazycloud.dev/depot-registry" = "true" }
    },
    {
      name   = "lazycloud-staging"
      labels = { "lazycloud.dev/depot-registry" = "true" }
    }
  ]
}

# -----------------------------------------------------------------------------
# Firewall
# -----------------------------------------------------------------------------

variable "management_cidrs" {
  description = "CIDRs allowed to access Kube API (6443) and Talos API (50000). Empty uses auto-detected current IP."
  type        = list(string)
  default     = []
}

variable "enable_public_ingress" {
  description = "Open ports 80/443 on nodes for ingress traffic. Not needed with Cloudflare Tunnel."
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# Destroy Helpers
# -----------------------------------------------------------------------------

variable "skip_bootstrap" {
  description = "Skip all Kubernetes/Helm bootstrap resources. Set to true before destroy."
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# Cloudflare
# -----------------------------------------------------------------------------

variable "cloudflare_api_token" {
  description = "Cloudflare API token with Cloudflare Tunnel:Edit and DNS:Edit permissions"
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID"
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for the domain"
  type        = string
}

variable "cloudflare_zone" {
  description = "Cloudflare zone (domain name, e.g., lazycloud.dev)"
  type        = string
  default     = "lazycloud.dev"
}

variable "route_root_domain" {
  description = "Also route root domain (lazycloud.dev and *.lazycloud.dev) through this cluster's tunnel"
  type        = bool
  default     = false
}
