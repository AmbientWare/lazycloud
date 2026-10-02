data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  arn_prefix = "arn:${data.aws_partition.current.partition}"
}

# EKS Auto Mode with no built-in node pools: node_capacity.tf declares the
# one pool, and Auto Mode sizes nodes from pod requests. The API answers
# outside the VPC only to cluster_api_cidrs; IAM access entries decide who
# may act.
resource "aws_eks_cluster" "control_plane" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  # Auto Mode brings its own CNI, kube-proxy and CoreDNS.
  bootstrap_self_managed_addons = false

  vpc_config {
    subnet_ids              = aws_subnet.cluster[*].id
    endpoint_public_access  = length(var.cluster_api_cidrs) > 0
    endpoint_private_access = true
    public_access_cidrs     = length(var.cluster_api_cidrs) > 0 ? var.cluster_api_cidrs : null
  }

  compute_config {
    enabled    = true
    node_pools = []
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
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

resource "aws_iam_role" "cluster" {
  name = "${var.name}-cluster"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  for_each   = toset(["AmazonEKSClusterPolicy", "AmazonEKSComputePolicy", "AmazonEKSBlockStoragePolicy", "AmazonEKSLoadBalancingPolicy", "AmazonEKSNetworkingPolicy"])
  role       = aws_iam_role.cluster.name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/${each.value}"
}

# Nodes register and pull images; workloads reach AWS through their own
# Pod Identity roles, never the node's.
resource "aws_iam_role" "node" {
  name = "${var.name}-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryReadOnly", "AmazonEKS_CNI_Policy"])
  role       = aws_iam_role.node.name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/${each.value}"
}

resource "aws_eks_access_entry" "node" {
  cluster_name  = aws_eks_cluster.control_plane.name
  principal_arn = aws_iam_role.node.arn
  type          = "EC2"
}

resource "aws_eks_access_policy_association" "node" {
  cluster_name  = aws_eks_access_entry.node.cluster_name
  principal_arn = aws_eks_access_entry.node.principal_arn
  policy_arn    = "${local.arn_prefix}:eks::aws:cluster-access-policy/AmazonEKSAutoNodePolicy"

  access_scope {
    type = "cluster"
  }
}

# The federation External Secrets presents each deployment's secrets-reader
# token to (IRSA). Workload pods use Pod Identity instead.
resource "aws_iam_openid_connect_provider" "cluster" {
  url            = aws_eks_cluster.control_plane.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

# Auto Mode runs the EBS CSI driver but declares no StorageClass. Cluster
# scoped, so here rather than in a deployment's chart.
resource "kubernetes_storage_class_v1" "ebs" {
  metadata {
    name = "lazycloud-ebs"
  }
  storage_provisioner    = "ebs.csi.eks.amazonaws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  reclaim_policy         = "Delete"
  allow_volume_expansion = true
  parameters             = { type = "gp3", encrypted = "true" }
}
