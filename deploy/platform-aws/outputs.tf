output "control_plane_role_arn" {
  description = <<-EOT
    Belongs in the control stack's TrustedPrincipalArns.

    `deploy/connected-aws/bootstrap.py --trusted-principal <this>` is what lets the
    control plane assume the connected-AWS control role.
  EOT
  value       = aws_iam_role.control_plane.arn
}

output "control_plane_instance_id" {
  description = "Target for `aws ssm start-session` and for the CI redeploy command."
  value       = aws_instance.control_plane.id
}

output "control_plane_public_ip" {
  description = "Elastic address of the control plane host."
  value       = aws_eip.control_plane.public_ip
}

output "deploy_bucket" {
  description = "Where CI writes the bundle the host converges onto."
  value       = aws_s3_bucket.deploy.id
}

output "ecr_registry" {
  description = "Registry the deployment override pins image digests against."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_repositories" {
  description = "Repository URL per image name."
  value       = { for name, repository in aws_ecr_repository.image : name => repository.repository_url }
}

output "secret_arns" {
  description = "Secrets the host is permitted to read. Values are written outside Terraform."
  value       = { for name, secret in aws_secretsmanager_secret.runtime : name => secret.arn }
}

output "runtime_configuration" {
  description = "Non-secret deployment values the Compose environment consumes."
  value = {
    LAZYCLOUD_OBJECT_STORE_BUCKET      = aws_s3_bucket.objects["objects"].id
    LAZYCLOUD_OBJECT_STORE_REGION_NAME = var.region
    # Empty is meaningful and distinct from omitted: it tells the object store to
    # resolve the platform role through the SDK credential chain and to presign
    # against S3 itself rather than a local endpoint.
    LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL           = ""
    LAZYCLOUD_OBJECT_STORE_PRESIGNED_ENDPOINT_URL = ""
    LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID          = ""
    LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY      = ""
    LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE       = "false"
  }
}

output "deployment" {
  description = "Prefix every globally-named resource and secret path carries."
  value       = var.deployment
}
