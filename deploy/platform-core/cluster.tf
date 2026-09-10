# Auto Mode sizes workers from pod requests. node_capacity.tf owns purchase
# policy and eligible families without choosing instance sizes or node counts.

resource "aws_eks_cluster" "control_plane" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  # Auto Mode brings its own CNI, kube-proxy and CoreDNS, and refuses to be
  # created alongside the self-managed ones. Left at its default the API rejects
  # the whole cluster rather than ignoring the pair it cannot honour.
  bootstrap_self_managed_addons = false

  vpc_config {
    subnet_ids = aws_subnet.cluster[*].id
    # Reachable from outside only from the addresses `cluster_api_cidrs` names.
    # `terraform apply` runs from an operator's machine and reaches this endpoint
    # through the Kubernetes and Helm providers, so that address has to be
    # listed. GitHub Actions never applies and needs nothing here; Argo and
    # External Secrets reach the private endpoint from inside the VPC. IAM still
    # decides who may act -- the creating principal is the administrator, and a
    # deployment's identities are Pod Identity associations that never touch
    # this endpoint -- and the allowlist decides who may ask. An empty list
    # turns the public endpoint off.
    endpoint_public_access  = length(var.cluster_api_cidrs) > 0
    endpoint_private_access = true
    public_access_cidrs     = length(var.cluster_api_cidrs) > 0 ? var.cluster_api_cidrs : null
  }

  # The node role also retains EKS-managed node access. Custom capacity is
  # bootstrapped by the Helm release; no built-in On-Demand pool is enabled.
  compute_config {
    enabled       = true
    node_pools    = []
    node_role_arn = aws_iam_role.node.arn
  }

  kubernetes_network_config {
    elastic_load_balancing {
      enabled = true
    }
  }

  storage_config {
    block_storage {
      enabled = true
    }
  }

  access_config {
    # The API decides who may act in the cluster, not a ConfigMap. The
    # aws-auth ConfigMap is the older mechanism and is edited in-cluster, which
    # means a Terraform apply cannot see it and a mistake in it locks everyone
    # out with no way back that does not involve the creating principal.
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
  ]

  tags = { Name = var.name }
}

data "aws_eks_cluster_auth" "control_plane" {
  name = aws_eks_cluster.control_plane.name
}
