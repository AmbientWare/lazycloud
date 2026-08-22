# Identities the cluster itself needs, and the federation that lets a workload
# hold one without the node holding it too.

resource "aws_iam_role" "cluster" {
  name        = "${var.deployment}-cluster"
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
  name        = "${var.deployment}-node"
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

# No OIDC provider, and no certificate thumbprint to keep current.
#
# Auto Mode nodes carry Pod Identity built in, which AWS documents as the
# recommended way to give a pod an AWS identity. It replaces a federated trust
# whose condition is a string built from the issuer URL with an association
# naming a cluster, a namespace and a service account -- the same three facts,
# stated where they can be checked rather than concatenated into a policy.

# Reads the secret values the deployment's containers are given.
#
# Scoped to this deployment's prefix and to reading. External Secrets copies
# values into the cluster; it never writes one back, and a role that could would
# make the cluster an authority on credentials Terraform declares.
resource "aws_iam_role" "external_secrets" {
  name        = "${var.deployment}-external-secrets"
  description = "External Secrets Operator: reads this deployment's secret values."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid       = "ReadDeploymentSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["${local.arn_prefix}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.deployment}/*"]
  }

  statement {
    sid       = "FindThemByName"
    actions   = ["secretsmanager:ListSecrets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "external-secrets"
  role   = aws_iam_role.external_secrets.id
  policy = data.aws_iam_policy_document.external_secrets.json
}

# Who may act inside the cluster. The deploy role runs `helm upgrade`, so it
# needs to reach the Kubernetes API as an administrator; nothing else does.
resource "aws_eks_access_entry" "deploy" {
  cluster_name  = aws_eks_cluster.control_plane.name
  principal_arn = aws_iam_role.deploy.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "deploy" {
  cluster_name  = aws_eks_cluster.control_plane.name
  principal_arn = aws_iam_role.deploy.arn
  policy_arn    = "${local.arn_prefix}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

# Which service account holds which role. Pod Identity resolves this at the
# cluster rather than from a claim the pod presents, so a service account that
# does not exist yet is an association waiting rather than a pod authenticating
# as nobody, and the names are checked against the cluster instead of matching a
# string by luck.
resource "aws_eks_pod_identity_association" "control_plane" {
  for_each = toset(var.control_plane_service_accounts)

  cluster_name    = aws_eks_cluster.control_plane.name
  namespace       = var.kubernetes_namespace
  service_account = each.value
  role_arn        = aws_iam_role.control_plane.arn
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  cluster_name    = aws_eks_cluster.control_plane.name
  namespace       = var.kubernetes_namespace
  service_account = var.external_secrets_service_account
  role_arn        = aws_iam_role.external_secrets.arn
}
