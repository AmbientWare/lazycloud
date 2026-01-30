# -----------------------------------------------------------------------------
# Cluster Identity
# -----------------------------------------------------------------------------

variable "cluster_id" {
  description = "Unique cluster identifier (e.g., ash-1, ash2, fsn1)"
  type        = string
}

variable "cluster_name" {
  description = "Name prefix for all resources"
  type        = string
}

# -----------------------------------------------------------------------------
# Hetzner Cloud
# -----------------------------------------------------------------------------

variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  type        = string
  sensitive   = true
}

variable "location" {
  description = "Hetzner Cloud location (ash = Ashburn US, fsn1 = Falkenstein DE)"
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
  default     = "cpx31" # 4 vCPU, 8 GB RAM
}

variable "worker_count" {
  description = "Initial number of worker nodes (Cluster Autoscaler manages scaling)"
  type        = number
  default     = 4
}

variable "worker_type" {
  description = "Server type for worker nodes"
  type        = string
  default     = "cpx41" # 8 vCPU, 16 GB RAM
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

variable "talos_schematic_id" {
  description = "Talos factory schematic ID (includes extensions like gvisor)"
  type        = string
  default     = "d9ff89777e246792e7642abd3220a616afb4e49822382e4213a2e528ab826fe5"
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
# AWS (for JuiceFS S3 backend + External Secrets)
# -----------------------------------------------------------------------------

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
# AWS Secrets Manager
# -----------------------------------------------------------------------------

variable "secrets_prefix" {
  description = "Prefix for AWS Secrets Manager secret names"
  type        = string
  default     = "lazycloud"
}

# -----------------------------------------------------------------------------
# Firewall
# -----------------------------------------------------------------------------

variable "management_cidrs" {
  description = "CIDRs allowed to access Kube API (6443) and Talos API (50000). Empty list uses auto-detected current IP."
  type        = list(string)
  default     = []
}

variable "enable_public_ingress" {
  description = "Open ports 80/443 on worker nodes for ingress traffic. Not needed if using Cloudflare Tunnel."
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# Destroy Helpers
# -----------------------------------------------------------------------------

variable "skip_bootstrap" {
  description = "Skip all Kubernetes/Helm bootstrap resources. Set to true before destroy to avoid errors."
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
  description = "Also route root domain (lazycloud.dev and *.lazycloud.dev) through this cluster's tunnel. Only enable on one cluster."
  type        = bool
  default     = false
}
