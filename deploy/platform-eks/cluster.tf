# The cluster the control plane runs on.
#
# Auto Mode provisions nodes, so there is no node group, no launch template and
# no AMI to keep current here. What it does not decide is where a pod that needs
# a TUN device may be scheduled, which is why `tailnet` below is its own node
# pool rather than a label on the default one.

resource "aws_eks_cluster" "control_plane" {
  name     = var.deployment
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids = aws_subnet.cluster[*].id
    # Reachable from outside, because the deploy runs from GitHub Actions and an
    # operator runs `kubectl` from a laptop; neither sits in this VPC. Access is
    # an IAM decision rather than a network one -- `aws_eks_access_entry` below
    # names who may act, and unauthenticated callers reach an endpoint that
    # refuses them.
    endpoint_public_access  = true
    endpoint_private_access = true
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

# Where the control plane runs, and the reason this pool exists.
#
# The control plane holds its own tailnet device: it needs NET_ADMIN, NET_RAW and
# a real /dev/net/tun, because workers reach it inbound by tailnet name and
# userspace networking cannot bind that. Auto Mode's own node pools are managed
# and give no say over the node image, so a pool this deployment owns is what
# makes the device a property of the node rather than a hope about it.
resource "aws_eks_node_group" "tailnet" {
  cluster_name    = aws_eks_cluster.control_plane.name
  node_group_name = "${var.deployment}-tailnet"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.cluster[*].id
  instance_types  = [var.tailnet_node_instance_type]
  ami_type        = "AL2023_x86_64_STANDARD"

  scaling_config {
    desired_size = var.tailnet_node_count
    min_size     = var.tailnet_node_count
    max_size     = var.tailnet_node_count
  }

  # Only workloads that ask land here. The control plane tolerates it; nothing
  # else does, so a general workload cannot drift onto the nodes whose whole
  # purpose is a device it does not need.
  taint {
    key    = "lazycloud.dev/tailnet"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  labels = {
    "lazycloud.dev/tailnet" = "true"
  }

  depends_on = [
    aws_iam_role_policy_attachment.node,
  ]

  tags = { Name = "${var.deployment}-tailnet" }
}

data "aws_eks_cluster_auth" "control_plane" {
  name = aws_eks_cluster.control_plane.name
}
