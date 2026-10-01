output "cluster_name" {
  description = "Cluster this deployment runs on; `kubectl -n <deployment>` targets it."
  value       = local.cluster_name
}

output "deployment" {
  description = "Namespace and prefix of every globally-named resource and secret path."
  value       = var.deployment
}

output "control_plane_role_arn" {
  description = "Identity of the server and scheduler pods, and the principal customer connection roles trust."
  value       = aws_iam_role.control_plane.arn
}

output "secrets_reader_role_arn" {
  description = "Annotated onto the chart's secrets-reader service account; what the SecretStore assumes."
  value       = aws_iam_role.secrets_reader.arn
}

output "operator_secret" {
  description = "Document an operator writes externally obtained credentials into."
  value       = aws_secretsmanager_secret.operator.name
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret on the GitHub environment named by github_environment."
  value       = aws_iam_role.deploy.arn
}

output "hosts_hostname" {
  description = "Name agents dial. Point it at the server-hosts load balancer through the cloudflare root."
  value       = local.hosts_hostname
}

output "platform_database" {
  description = "PlanetScale database the platform connects to."
  value       = planetscale_postgres_branch.platform.database
}
