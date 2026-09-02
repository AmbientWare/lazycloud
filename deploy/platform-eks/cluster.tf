# The cluster the control plane runs on.
#
# Auto Mode provisions nodes, so there is no node group, no launch template and
# no AMI to keep current here, and no NodePool either: `general-purpose` below is
# a built-in that EKS creates and reconciles, and its instance families and sizes
# are not this module's to set.
#
# That is deliberate rather than a limitation. Karpenter sizes the cluster from
# what the pods request, so the way to obtain more machine here is to request
# accurately, in `deploy/chart/values.yaml`, in the Argo CD values in
# `argocd.tf`, and in `deploy/argocd/apps/external-secrets.yaml`. A floor on
# instance size would buy one shortfall at a fixed price, be wrong again at the
# next one, and hide the pod that caused it.
#
# The failure this pool is blamed for is always the same one: a pod that
# requests nothing still runs, so the node is sized as though it were not there.
# Nothing reports the gap, because by every number the scheduler holds the pods
# fit.

resource "aws_eks_cluster" "control_plane" {
  name     = var.deployment
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
    # decides who may act -- `aws_eks_access_entry` below names them -- and the
    # allowlist decides who may ask. An empty list turns the public endpoint off.
    endpoint_public_access  = length(var.cluster_api_cidrs) > 0
    endpoint_private_access = true
    public_access_cidrs     = length(var.cluster_api_cidrs) > 0 ? var.cluster_api_cidrs : null
  }

  # Auto Mode. Without both blocks the cluster comes up with no compute at all
  # and every pod stays Pending with no node to place it on.
  compute_config {
    enabled       = true
    node_pools    = ["general-purpose"]
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

  tags = { Name = var.deployment }
}

data "aws_eks_cluster_auth" "control_plane" {
  name = aws_eks_cluster.control_plane.name
}
