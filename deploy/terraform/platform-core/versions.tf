terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  # Coordinates come from the operator's backend file (deploy/terraform/README.md).
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.67.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "= 3.3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "= 3.3.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "lazycloud:platform"   = var.name
      "lazycloud:managed-by" = "terraform"
    }
  }
}

# Both reach the cluster this root creates with a token from the operator's
# AWS identity, never a local kubeconfig.
locals {
  cluster_auth = {
    host                   = aws_eks_cluster.control_plane.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.control_plane.certificate_authority[0].data)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", var.name, "--region", var.region]
    }
  }
}

provider "kubernetes" {
  host                   = local.cluster_auth.host
  cluster_ca_certificate = local.cluster_auth.cluster_ca_certificate
  exec {
    api_version = local.cluster_auth.exec.api_version
    command     = local.cluster_auth.exec.command
    args        = local.cluster_auth.exec.args
  }
}

provider "helm" {
  # Its own repository list and cache, so an operator's unrelated Helm
  # repositories cannot fail this apply.
  repository_config_path = "${path.module}/.helm/repositories.yaml"
  repository_cache       = "${path.module}/.helm/cache"
  kubernetes             = local.cluster_auth
}
