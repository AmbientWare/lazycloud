terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    planetscale = {
      source  = "planetscale/planetscale"
      version = "~> 1.8"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "lazycloud:deployment" = var.deployment
      "lazycloud:managed-by" = "terraform"
    }
  }
}

# Both authenticate from the cluster this module creates, so nothing reads a
# kubeconfig from the machine running the apply. An apply from a laptop and an
# apply from CI then reach the same cluster the same way.
provider "kubernetes" {
  host                   = aws_eks_cluster.control_plane.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.control_plane.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.control_plane.token
}

provider "helm" {
  # Its own repository list and cache, not the one belonging to whoever is
  # running the apply. The provider requires a cached index for every repository
  # in the file it reads, so a developer with an unreachable repo configured for
  # some other project fails this apply on a chart it never asked for -- which is
  # what happened, on `sealed-secrets`. Kept inside the module so an apply from a
  # laptop and an apply from CI resolve the same chart the same way.
  repository_config_path = "${path.module}/.helm/repositories.yaml"
  repository_cache       = "${path.module}/.helm/cache"

  kubernetes {
    host                   = aws_eks_cluster.control_plane.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.control_plane.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.control_plane.token
  }
}
