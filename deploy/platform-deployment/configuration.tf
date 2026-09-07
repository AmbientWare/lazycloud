# Application CI reads resource identities here, never the secret-bearing state.
resource "aws_s3_object" "infrastructure" {
  bucket       = aws_s3_bucket.deploy.id
  key          = "configuration/infrastructure-v1.json"
  content_type = "application/json"
  content = jsonencode({
    schema_version    = 3
    deployment        = var.deployment
    region            = var.region
    registry          = local.ecr_registry
    repository_prefix = data.terraform_remote_state.core.outputs.ecr_repository_prefix
    storage_class     = data.terraform_remote_state.core.outputs.storage_class_name
    service_accounts = {
      controlPlane       = var.control_plane_service_accounts.controlPlane
      scheduler          = var.control_plane_service_accounts.scheduler
      secretsReader      = var.secrets_reader_service_account
      wireguardBootstrap = var.wireguard_bootstrap_service_account
    }
    object_bucket = aws_s3_bucket.objects["objects"].id
    image_archive = {
      bucket       = cloudflare_r2_bucket.image_archives.name
      endpoint_url = "https://${local.cloudflare_account_id}.r2.cloudflarestorage.com"
    }
    workspace_bucket_prefix    = local.workspace_bucket_prefix
    workspace_storage_role_arn = aws_iam_role.workspace_storage.arn
    workload_image_repository  = local.workload_image_repository
    control_principal_arn      = aws_iam_role.control_principal.arn
    public_origin              = "https://${data.terraform_remote_state.cloudflare.outputs.records.apex}"
    redis_host                 = aws_elasticache_replication_group.redis.primary_endpoint_address
    hetzner_node_images        = var.hetzner_node_images
    fleet = {
      account_id        = data.aws_caller_identity.current.account_id
      role_arn          = aws_iam_role.fleet_connection.arn
      vpc_id            = aws_vpc.fleet.id
      subnet_ids        = aws_subnet.fleet[*].id
      security_group_id = aws_security_group.fleet_node.id
    }
    secret_documents = {
      platform  = aws_secretsmanager_secret.platform.name
      operator  = aws_secretsmanager_secret.operator.name
      wireguard = aws_secretsmanager_secret.wireguard.name
    }
    secrets_reader_role_arn         = aws_iam_role.secrets_reader.arn
    cloudflare_tunnel_id            = data.terraform_remote_state.cloudflare.outputs.tunnel_id
    database_max_connections        = var.database_max_connections
    database_pooler_max_connections = var.database_pooler_max_connections
  })
}

output "infrastructure_config_uri" {
  description = "Set INFRASTRUCTURE_CONFIG_URI on this deployment's GitHub environment."
  value       = "s3://${aws_s3_bucket.deploy.id}/${aws_s3_object.infrastructure.key}"
}
