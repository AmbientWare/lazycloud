output "cluster_name" {
  description = "Cluster `helm` and `kubectl` target."
  value       = aws_eks_cluster.control_plane.name
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = aws_eks_cluster.control_plane.endpoint
}

output "kubernetes_namespace" {
  description = "Namespace the chart installs into."
  value       = var.kubernetes_namespace
}

output "control_plane_role_arn" {
  description = <<-EOT
    Belongs in the control stack's TrustedPrincipalArns.

    `deploy/connected-aws/bootstrap.py --trusted-principal <this>` is what lets the
    control plane assume the connected-AWS control role.
  EOT
  value       = aws_iam_role.control_plane.arn
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
    # What a customer's account is told to trust. Published here rather than
    # left to an operator, because a value carried by hand is a step that has to
    # be remembered on every stand-up and reports its absence as a refused
    # capability rather than as a missing setting.
    LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN = aws_iam_role.control_principal.arn
    # The origin customers and the SDK reach this deployment on, taken from the
    # zone whose tunnel serves it rather than written twice. The default is
    # localhost, which is correct for the local stack and silently wrong here:
    # nothing fails, and the authorization templates and OAuth redirects a
    # customer receives point at their own machine.
    LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL = "https://${data.terraform_remote_state.cloudflare.outputs.records.apex}"
    # Where GitHub returns a person after they sign in. Configuration rather than
    # something derived from the request: the Host header belongs to whoever sent
    # it, so deriving the callback would let a caller choose a redirect target
    # GitHub then honours. Absent, sign-in is refused as provider_unavailable and
    # the dashboard reports that sign-ins are not supported.
    LAZYCLOUD_GITHUB_REDIRECT_URI = "https://${data.terraform_remote_state.cloudflare.outputs.records.apex}/auth/github/callback"
    # `rediss`, because the replication group requires TLS in transit. A plain
    # `redis://` here connects, gets refused at the handshake, and reports it as
    # a connection error rather than as a scheme.
    LAZYCLOUD_REDIS_URL = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
    # Managed, and stated rather than defaulted. The runtime's own default is
    # `disabled`, so a deployment that says nothing comes up with no tailnet
    # device at all: it serves, reports healthy, and no worker can reach it.
    # Stated because the runtime will not infer it. A tailnet daemon is opted
    # into rather than inherited, so the default is off and a deployment that
    # says nothing serves, reports healthy, and holds no address any worker can
    # reach. Everything else about the tailnet is a default that is already
    # right: the device name is a constant the enrolment origin is built from,
    # and the two tags match the policy `deploy/tailnet` grants.
    LAZYCLOUD_TAILNET_MODE = "managed"
  }
}

output "deployment" {
  description = "Prefix every globally-named resource and secret path carries."
  value       = var.deployment
}

output "control_principal_arn" {
  description = <<-EOT
    The principal a customer's account authorizes.

    Carried to the deployment by `runtime_configuration`, not by hand. This
    output remains for an operator reading who the trust names, and for the
    connection template's own reference; it is not a step anyone performs.
  EOT
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

output "cloudflare_tunnel_id" {
  description = <<-EOT
    Tunnel the in-cluster connectors run.

    Read from the module that owns it rather than pasted, for the same reason its
    credentials are: a tunnel replaced in `deploy/cloudflare` would otherwise
    leave this naming one that no longer exists.
  EOT
  value       = data.terraform_remote_state.cloudflare.outputs.tunnel_id
}
