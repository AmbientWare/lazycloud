output "deploy_role_arn" {
  description = "The github root sets it as AWS_DEPLOY_ROLE_ARN on this deployment's environment."
  value       = aws_iam_role.deploy.arn
}

output "operator_secret" {
  description = "Document the operator writes GitHub, Stripe, Resend and Cloudflare credentials into."
  value       = aws_secretsmanager_secret.operator.name
}

output "control_plane_role_arn" {
  description = "The principal customer connection roles trust."
  value       = aws_iam_role.control_plane.arn
}
