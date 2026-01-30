# Required providers for the cluster module
# Backend configuration is handled by the calling environment, not the module

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
}
