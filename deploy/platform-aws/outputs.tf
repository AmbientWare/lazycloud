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
    LAZYCLOUD_OBJECT_STORE_BUCKET                  = aws_s3_bucket.objects["objects"].id
    LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX = local.workspace_bucket_prefix
    # Carries the deployment name because a tailnet is shared across accounts.
    # Two deployments advertising one hostname collide, and the loser keeps a
    # "-1" suffix that every configured origin naming the old name then misses.
    LAZYCLOUD_TAILNET_REPLICA_HOSTNAME = "${var.deployment}-control-plane"
    LAZYCLOUD_OBJECT_STORE_REGION_NAME = var.region
    # Empty is meaningful and distinct from omitted: it tells the object store to
    # resolve the platform role through the SDK credential chain and to presign
    # against S3 itself rather than a local endpoint.
    LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL           = ""
    LAZYCLOUD_OBJECT_STORE_PRESIGNED_ENDPOINT_URL = ""
    LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID          = ""
    LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY      = ""
    LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE       = "false"
    # JSON, because the settings field is a map and the host environment carries
    # strings. Empty advertises no instance types, which is a control plane with
    # no managed capacity rather than a failure.
    LAZYCLOUD_AWS_CAPACITY_INSTANCE_HOURLY_MICROS = jsonencode(var.instance_hourly_micros)
  }
}

output "deployment" {
  description = "Prefix every globally-named resource and secret path carries."
  value       = var.deployment
}

output "control_principal_arn" {
  description = "Set as LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN in the deployment."
  value       = aws_iam_role.control_principal.arn
}

output "fleet_network" {
  description = <<-EOT
    The network the platform's own pools launch into.

    Supplied to the connection as `network` when registering this account in
    existing-role mode. Exactly two subnets, in two zones, because that is what
    `AwsAccountNetwork` accepts.
  EOT
  value = {
    vpc_id            = aws_vpc.fleet.id
    subnet_ids        = aws_subnet.fleet[*].id
    security_group_id = aws_security_group.fleet_node.id
  }
}

output "fleet_connection_role_arn" {
  description = "Role the control plane assumes to manage the platform's own capacity."
  value       = aws_iam_role.fleet_connection.arn
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
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository secret."
  value       = aws_iam_role.deploy.arn
}

output "secret_environment" {
  description = <<-EOT
    Which Secrets Manager entry each environment variable is read from.

    Published so the release can put it in the bundle. It used to be written by
    user data, which meant changing the map required replacing the machine.
  EOT
  value       = local.secret_environment
}
