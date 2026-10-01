# The chart's infrastructure values for this deployment, in the shape of
# deploy/helm/lazycloud/values.yaml. Nothing here is a secret. The apply
# writes them where the Deploy workflow reads them; Deploy merges them with
# the release version into values-deployment.yaml on the deployment branch.
locals {
  helm_values = {
    domain    = local.apex
    awsRegion = var.region
    image = {
      registry = "${local.ecr_registry}/${data.terraform_remote_state.core.outputs.ecr_repository_prefix}/release"
    }
    externalSecrets = {
      enabled       = true
      region        = var.region
      readerRoleArn = aws_iam_role.secrets_reader.arn
      documents = {
        platform = aws_secretsmanager_secret.platform.name
        operator = aws_secretsmanager_secret.operator.name
      }
    }
    serviceAccounts = {
      server        = var.platform_service_accounts.server
      scheduler     = var.platform_service_accounts.scheduler
      secretsReader = var.secrets_reader_service_account
    }
    # Kubelet probes, load balancer health checks and the TLS-terminating
    # host load balancer all come from the cluster VPC.
    networkPolicy = {
      nodeCIDRs = [local.cluster_vpc_cidr]
      hostCIDRs = [local.cluster_vpc_cidr]
    }
    cloudflared = {
      tunnelId = data.terraform_remote_state.cloudflare.outputs.tunnel_id
    }
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
      LAZYCLOUD_FLEET_NETWORKS            = jsonencode({ for region, network in local.fleet_networks : region => network.network })
      LAZYCLOUD_AWS_PRINCIPAL_ARN         = aws_iam_role.control_plane.arn
    }
    server = {
      config = {
        LAZYCLOUD_IMAGE_REGISTRY     = local.ecr_registry
        LAZYCLOUD_IMAGE_REPOSITORY   = trimprefix(local.workload_image_repository, "${local.ecr_registry}/")
        LAZYCLOUD_CLOUDFLARE_ZONE_ID = data.terraform_remote_state.cloudflare.outputs.zone_id
      }
      hostService = {
        certificateArn = aws_acm_certificate_validation.hosts.certificate_arn
      }
    }
  }
}

resource "aws_s3_object" "helm_values" {
  bucket       = aws_s3_bucket.storage["deploy"].id
  key          = "${var.deployment}/values.json"
  content      = jsonencode(local.helm_values)
  content_type = "application/json"
}

output "helm_values" {
  description = "Chart values the apply published for the Deploy workflow."
  value       = local.helm_values
}
