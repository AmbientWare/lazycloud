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

# The API token comes from CLOUDFLARE_API_TOKEN in the operator's environment, so
# it cannot reach a tfvars file or a saved plan. It needs Account > Cloudflare
# Tunnel > Edit, Zone > DNS > Edit, and Zone > SSL and Certificates > Edit — a
# wider scope than the token the control plane runs with, which is why they are
# two separate tokens and only the narrow one belongs in the deployment .env.
provider "cloudflare" {}
