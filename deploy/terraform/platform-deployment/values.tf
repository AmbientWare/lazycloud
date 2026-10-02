# The chart's infrastructure values (deploy/helm/lazycloud/values.yaml
# shape). Nothing here is secret. Deploy reads them from the deploy bucket
# and merges the release version into values-deployment.yaml on the
# deployment branch, so a change here reaches the cluster on the next Deploy.
resource "aws_s3_object" "values" {
  bucket       = aws_s3_bucket.storage["deploy"].id
  key          = "values.json"
  content_type = "application/json"
  content = jsonencode({
    domain    = var.domain
    awsRegion = var.region
    image     = { registry = local.core.release_registry }
    externalSecrets = {
      enabled       = true
      region        = var.region
      readerRoleArn = aws_iam_role.secrets_reader.arn
      documents     = { platform = aws_secretsmanager_secret.platform.name, operator = data.aws_secretsmanager_secret.operator.name }
    }
    # Kubelet probes, NLB health checks and the TLS-terminating host NLB
    # all come from the cluster VPC.
    networkPolicy = { nodeCIDRs = [local.core.vpc_cidr_block], hostCIDRs = [local.core.vpc_cidr_block] }
    cloudflared   = { tunnelId = cloudflare_zero_trust_tunnel_cloudflared.public.id }
    config = {
      LAZYCLOUD_OBJECT_STORE_REGION       = var.region
      LAZYCLOUD_OBJECT_STORE_BUCKET       = aws_s3_bucket.storage["objects"].id
      LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER = "aws"
      LAZYCLOUD_WORKSPACE_BUCKET_PREFIX   = local.workspace_bucket_prefix
      LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN = aws_iam_role.workspace_storage.arn
      LAZYCLOUD_FLEET_NAME                = local.fleet_name
      LAZYCLOUD_FLEET_ACCOUNT_ID          = local.account_id
      LAZYCLOUD_FLEET_NODE_ROLE_ARN       = aws_iam_role.fleet_node.arn
      LAZYCLOUD_FLEET_INSTANCE_PROFILE    = aws_iam_instance_profile.fleet_node.name
      LAZYCLOUD_FLEET_NETWORKS            = jsonencode(local.fleet_networks)
      LAZYCLOUD_AWS_PRINCIPAL_ARN         = aws_iam_role.control_plane.arn
    }
    server = {
      config = {
        LAZYCLOUD_IMAGE_REGISTRY               = local.core.ecr_registry
        LAZYCLOUD_IMAGE_REPOSITORY             = local.core.workload_image_repository
        LAZYCLOUD_IMAGE_REGISTRY_HOST_ROLE_ARN = aws_iam_role.registry_hosts.arn
        LAZYCLOUD_CLOUDFLARE_ZONE_ID           = var.cloudflare_zone_id
      }
      hostService = { certificateArn = aws_acm_certificate_validation.hosts.certificate_arn }
    }
  })
}
