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
    # Reads the OIDC issuer's certificate, whose fingerprint is what makes the
    # federation trustworthy rather than merely configured.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
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
