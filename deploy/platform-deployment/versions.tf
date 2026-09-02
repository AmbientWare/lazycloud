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
  }
}

# No Kubernetes or Helm provider. Everything this module puts in the cluster is
# an AWS association keyed on the cluster's name, so an apply never reaches the
# Kubernetes API and needs neither a kubeconfig nor a listed address.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "lazycloud:deployment" = var.deployment
      "lazycloud:managed-by" = "terraform"
    }
  }
}
