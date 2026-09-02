# Identities the cluster itself needs. Every workload identity is a deployment's
# and lives in `deploy/platform-deployment`.

resource "aws_iam_role" "cluster" {
  name        = "${var.name}-cluster"
  description = "EKS control plane: manages network interfaces and load balancers."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  for_each = toset([
    "AmazonEKSClusterPolicy",
    # Auto Mode provisions and retires nodes itself, and does it as the cluster
    # role rather than as a separate one.
    "AmazonEKSComputePolicy",
    "AmazonEKSBlockStoragePolicy",
    "AmazonEKSLoadBalancingPolicy",
    "AmazonEKSNetworkingPolicy",
  ])

  role       = aws_iam_role.cluster.name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/${each.value}"
}

# What a node is, before any workload lands on it. Deliberately small: a node
# pulls images and registers itself, and everything a workload needs is reached
# through its own role rather than through the machine underneath it.
resource "aws_iam_role" "node" {
  name        = "${var.name}-node"
  description = "EKS nodes: registration and image pulls."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEC2ContainerRegistryReadOnly",
    "AmazonEKS_CNI_Policy",
  ])

  role       = aws_iam_role.node.name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/${each.value}"
}
