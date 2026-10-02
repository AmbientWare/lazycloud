# One deployment on platform-core's cluster: its database, buckets, secret
# documents, fleet networks, workload identities, public ingress and Stripe
# webhook, and the chart values the Deploy workflow records.

data "terraform_remote_state" "core" {
  backend = "s3"
  config  = merge(jsondecode(file(var.terraform_backend_config)), { key = var.core_state_key })

  lifecycle {
    postcondition {
      condition     = self.outputs.region == var.region
      error_message = "The cluster is in ${self.outputs.region}; a deployment runs where its cluster does."
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  core       = data.terraform_remote_state.core.outputs
  account_id = data.aws_caller_identity.current.account_id
  arn_prefix = "arn:${data.aws_partition.current.partition}"
  # Every fleet instance carries lazycloud:fleet=<this>, here and in
  # connected accounts; launch and terminate rights hang on it.
  fleet_name = var.deployment
}
