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

variable "argocd_repo_ssh_key_path" {
  description = "Path to SSH private key file for ArgoCD to access the Git repo"
  type        = string
  default     = "~/.ssh/id_rsa"
}
