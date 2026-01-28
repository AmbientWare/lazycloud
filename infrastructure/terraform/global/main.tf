# -----------------------------------------------------------------------------
# Global Infrastructure
# -----------------------------------------------------------------------------
# Shared resources used by all clusters (secrets, etc.)
#
# Usage:
#   terraform init -backend-config=backend.hcl
#   terraform plan
#   terraform apply

terraform {
  required_version = ">= 1.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Partial backend configuration - complete with backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
}
