output "cluster_name" {
  description = "Cluster this deployment runs on; `kubectl -n <deployment>` targets it."
  value       = local.cluster_name
}

output "kubernetes_namespace" {
  description = "Namespace the deployment runs in, which is its name."
  value       = var.deployment
}

output "control_plane_role_arn" {
  description = "Identity of the control plane and scheduler pods, and the principal the control role trusts."
  value       = aws_iam_role.control_plane.arn
}

output "secrets_reader_role_arn" {
  description = "Annotated onto the chart's `secrets-reader` service account; what the SecretStore assumes."
  value       = aws_iam_role.secrets_reader.arn
}

output "deploy_bucket" {
  description = "Non-secret infrastructure descriptor storage."
  value       = aws_s3_bucket.storage["deploy"].id
}

output "ecr_registry" {
  description = "Registry the images this deployment runs are pulled from. The cluster's, re-exported so a deploy reads one state."
  value       = local.ecr_registry
}

output "secret_arns" {
  description = "The documents this deployment's credentials live in."
  value = {
    platform = aws_secretsmanager_secret.platform.arn
    operator = aws_secretsmanager_secret.operator.arn
  }
}

output "operator_secret" {
  description = "Entry an operator writes the externally-obtained credentials into."
  value       = aws_secretsmanager_secret.operator.name
}

output "deployment" {
  description = "Prefix every globally-named resource and secret path carries."
  value       = var.deployment
}

output "control_principal_arn" {
  description = <<-EOT
    The principal a customer's account authorizes.

    Carried to the deployment by the infrastructure descriptor, not by hand. This
    output remains for an operator reading who the trust names, and for the
    connection template's own reference; it is not a step anyone performs.
  EOT
  value       = aws_iam_role.control_principal.arn
}

output "acceptance_role_arns" {
  description = "Acceptance roles, when this account creates them."
  value = var.create_acceptance_roles ? {
    operator         = aws_iam_role.acceptance_operator[0].arn
    stack_execution  = aws_iam_role.customer_stack_execution[0].arn
    node_diagnostics = aws_iam_role.node_diagnostics[0].arn
  } : {}
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret on the GitHub environment named by github_environment."
  value       = aws_iam_role.deploy.arn
}

output "cloudflare_tunnel_id" {
  description = <<-EOT
    Tunnel the in-cluster connectors run.

    Read from the module that owns it rather than pasted, for the same reason its
    credentials are: a tunnel replaced in `deploy/cloudflare` would otherwise
    leave this naming one that no longer exists.
  EOT
  value       = data.terraform_remote_state.cloudflare.outputs.tunnel_id
}
output "release_bucket" {
  value = aws_s3_bucket.storage["releases"].id
}

output "release_public_url" {
  value = "https://${local.release_hostname}"
}
