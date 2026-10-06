terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  # Coordinates come from the operator's backend file (deploy/terraform/README.md).
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.67.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.26.0"
    }
    planetscale = {
      source  = "planetscale/planetscale"
      version = "= 1.11.1"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.9.1"
    }
    # Stripe publishes only pre-releases and asks for an exact pin.
    stripe = {
      source  = "stripe/stripe"
      version = "= 0.3.0-beta.4"
    }
  }
}

# Credentials come from the operator's environment: AWS_PROFILE=default,
# CLOUDFLARE_API_TOKEN, PLANETSCALE_SERVICE_TOKEN_ID and
# PLANETSCALE_SERVICE_TOKEN, STRIPE_API_KEY. None reaches a variable, so
# none reaches a plan file.
provider "cloudflare" {}

provider "planetscale" {}

provider "stripe" {}

locals {
  default_tags = { "lazycloud:deployment" = var.deployment, "lazycloud:managed-by" = "terraform" }
}

# Resources in other regions, the fleet networks and layer copies, set
# their own region argument.
provider "aws" {
  region = var.region
  default_tags {
    tags = local.default_tags
  }
}
