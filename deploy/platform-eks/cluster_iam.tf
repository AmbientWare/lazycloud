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

# The federation itself. Every workload identity below is a condition on this
# provider's subject claim, which is what makes "this service account" a thing
# AWS can be asked about.
data "tls_certificate" "cluster" {
  url = aws_eks_cluster.control_plane.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "cluster" {
  url             = aws_eks_cluster.control_plane.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster.certificates[0].sha1_fingerprint]
}

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
      Principal = { Federated = aws_iam_openid_connect_provider.cluster.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_subject_key}:aud" = "sts.amazonaws.com"
          "${local.oidc_subject_key}:sub" = "system:serviceaccount:${var.kubernetes_namespace}:${var.external_secrets_service_account}"
        }
      }
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
