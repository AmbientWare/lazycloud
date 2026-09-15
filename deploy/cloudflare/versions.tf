terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  # Coordinates come from the shared operator backend JSON.
  backend "s3" {}

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.23.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.7.2"
    }
  }
}

# Terraform reads CLOUDFLARE_API_TOKEN from the operator environment. The
# certificate controller reuses that token through the operator secret document.
provider "cloudflare" {}
