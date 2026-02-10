# -----------------------------------------------------------------------------
# ash-1 Cluster (US East - Ashburn)
# -----------------------------------------------------------------------------
# Primary production cluster for LazyCloud
#
# Usage:
#   terraform init -backend-config=backend.hcl
#   terraform plan
#   terraform apply

terraform {
  required_version = ">= 1.8"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.59"
    }
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.10"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.1"
    }
  }

  # Partial backend configuration - complete with backend.hcl
  backend "s3" {}
}

# -----------------------------------------------------------------------------
# Providers
# -----------------------------------------------------------------------------

provider "hcloud" {
  token = var.hcloud_token
}

provider "talos" {}

provider "aws" {
  region = var.aws_region
}

# Helm and Kubernetes providers are configured using module outputs
# Terraform handles the dependency ordering automatically
provider "helm" {
  kubernetes = {
    host                   = module.cluster.kubernetes_host
    client_certificate     = base64decode(module.cluster.kubernetes_client_certificate)
    client_key             = base64decode(module.cluster.kubernetes_client_key)
    cluster_ca_certificate = base64decode(module.cluster.kubernetes_ca_certificate)
  }
}

provider "kubernetes" {
  host                   = module.cluster.kubernetes_host
  client_certificate     = base64decode(module.cluster.kubernetes_client_certificate)
  client_key             = base64decode(module.cluster.kubernetes_client_key)
  cluster_ca_certificate = base64decode(module.cluster.kubernetes_ca_certificate)
}

# -----------------------------------------------------------------------------
# Cluster Module
# -----------------------------------------------------------------------------

module "cluster" {
  source = "../../modules/hetzner-cluster"

  # Cluster identity
  cluster_id   = "ash-1"
  cluster_name = var.cluster_name

  # Hetzner configuration
  hcloud_token        = var.hcloud_token
  location            = var.location
  control_plane_count = var.control_plane_count
  control_plane_type  = var.control_plane_type
  platform_count      = var.platform_count
  platform_type       = var.platform_type
  sandbox_type        = var.sandbox_type

  # Kubernetes/Talos versions
  kubernetes_version = var.kubernetes_version
  talos_version      = var.talos_version

  # Networking
  network_ipv4_cidr = var.network_ipv4_cidr
  node_ipv4_cidr    = var.node_ipv4_cidr
  pod_ipv4_cidr     = var.pod_ipv4_cidr
  service_ipv4_cidr = var.service_ipv4_cidr

  # AWS credentials for JuiceFS + External Secrets
  aws_access_key_id     = var.aws_access_key_id
  aws_secret_access_key = var.aws_secret_access_key

  # JuiceFS configuration
  juicefs_name  = var.juicefs_name
  juicefs_token = var.juicefs_token

  # ArgoCD configuration
  argocd_domain            = var.argocd_domain
  git_repo_url             = var.git_repo_url
  argocd_repo_ssh_key_path = var.argocd_repo_ssh_key_path

  # Application namespaces
  app_namespaces = var.app_namespaces

  # AWS Secrets Manager
  secrets_prefix = var.secrets_prefix

  # Firewall
  management_cidrs      = var.management_cidrs
  enable_public_ingress = var.enable_public_ingress

  # Destroy helpers
  skip_bootstrap = var.skip_bootstrap

  # Cloudflare Tunnel (auto-creates tunnel, DNS, and stores token in Secrets Manager)
  cloudflare_api_token  = var.cloudflare_api_token
  cloudflare_account_id = var.cloudflare_account_id
  cloudflare_zone_id    = var.cloudflare_zone_id
  cloudflare_zone       = var.cloudflare_zone
  route_root_domain     = var.route_root_domain
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "cluster_id" {
  description = "Cluster identifier"
  value       = module.cluster.cluster_id
}

output "kubeconfig" {
  description = "Kubeconfig for the cluster"
  value       = module.cluster.kubeconfig
  sensitive   = true
}

output "talosconfig" {
  description = "Talos client configuration"
  value       = module.cluster.talosconfig
  sensitive   = true
}

output "control_plane_vip" {
  description = "Floating IP (VIP) for the kube API"
  value       = module.cluster.control_plane_vip
}

output "control_plane_ips" {
  description = "Public IPv4 addresses of control plane nodes"
  value       = module.cluster.control_plane_ips
}

output "argocd_admin_password" {
  description = "ArgoCD initial admin password (retrieve after apply)"
  value       = module.cluster.argocd_admin_password
}

output "cloudflare_tunnel_id" {
  description = "Cloudflare Tunnel ID for this cluster"
  value       = module.cluster.cloudflare_tunnel_id
}
