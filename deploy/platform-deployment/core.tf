# The cluster this deployment runs on, read from the module that owns it.
#
# `deploy/platform-core` creates the cluster, its network, the image
# repositories and the OIDC provider once, for every deployment. This module
# attaches one deployment to it: a namespace's worth of Pod Identity
# associations, a reader role the cluster's federation trusts, a Redis that the
# cluster's VPC may reach. Nothing here is copied by hand; a value that lived in
# two states would be two values the first time one moved.
data "terraform_remote_state" "core" {
  backend = "s3"

  config = {
    bucket = var.state_bucket
    key    = var.core_state_key
    region = var.region
  }

  lifecycle {
    postcondition {
      condition     = self.outputs.region == var.region
      error_message = "The cluster is in ${self.outputs.region}; this deployment says ${var.region}. A deployment runs where its cluster does."
    }
  }
}

locals {
  cluster_name                  = data.terraform_remote_state.core.outputs.cluster_name
  cluster_vpc_id                = data.terraform_remote_state.core.outputs.vpc_id
  cluster_vpc_cidr              = data.terraform_remote_state.core.outputs.vpc_cidr_block
  cluster_subnet_ids            = data.terraform_remote_state.core.outputs.subnet_ids
  ecr_registry                  = data.terraform_remote_state.core.outputs.ecr_registry
  ecr_repository_arns           = data.terraform_remote_state.core.outputs.ecr_repository_arns
  workload_image_repository     = data.terraform_remote_state.core.outputs.workload_image_repository
  workload_image_repository_arn = data.terraform_remote_state.core.outputs.workload_image_repository_arn
  oidc_provider_arn             = data.terraform_remote_state.core.outputs.oidc_provider_arn
  oidc_issuer_host              = data.terraform_remote_state.core.outputs.oidc_issuer_host
}
