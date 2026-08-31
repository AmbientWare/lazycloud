terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  backend "s3" {}

  required_providers {
    stripe = {
      source = "stripe/stripe"
      # Stripe's own provider, generated from their internal tooling, is pinned
      # exactly. The version is a pre-release because that is what Stripe
      # publishes today; its README asks for an exact pin in production, and a
      # floating constraint would let a generator run change this account's shape.
      version = "= 0.3.0-beta.4"
    }
  }
}

# The API key comes from STRIPE_API_KEY in the operator's environment. It is not a
# variable, so it cannot reach a tfvars file or a saved plan.
provider "stripe" {}
