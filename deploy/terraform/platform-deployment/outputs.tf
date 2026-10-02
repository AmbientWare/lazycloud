output "deploy_role_arn" {
  description = "The github root sets it as AWS_DEPLOY_ROLE_ARN on this deployment's environment."
  value       = aws_iam_role.deploy.arn
}

output "control_plane_role_arn" {
  description = "The principal customer connection roles trust."
  value       = aws_iam_role.control_plane.arn
}
