terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  # Partial configuration: the operator's private backend file supplies the
  # state bucket, as for the other roots (`terraform init -backend-config=...`).
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.67.0"
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
