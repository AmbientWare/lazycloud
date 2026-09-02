# The identities this deployment's workloads hold, and which service account
# holds which.
#
# Pod Identity, for every workload pod. An association names a cluster, a
# namespace and a service account beside the cluster, so a service account that
# does not exist yet is an association waiting rather than a pod authenticating
# as nobody, and the names are checked against the cluster instead of matching
# a string by luck. The namespace is this deployment's, which is what keeps
# `lazycloud-staging/control-plane` from holding prod's role.
#
# The one exception is `secrets_reader.tf`, which explains itself.

resource "aws_eks_pod_identity_association" "control_plane" {
  for_each = toset(var.control_plane_service_accounts)

  cluster_name    = local.cluster_name
  namespace       = var.deployment
  service_account = each.value
  role_arn        = aws_iam_role.control_plane.arn
}

resource "aws_eks_pod_identity_association" "wireguard_bootstrap" {
  cluster_name    = local.cluster_name
  namespace       = var.deployment
  service_account = var.wireguard_bootstrap_service_account
  role_arn        = aws_iam_role.wireguard_bootstrap.arn
}

resource "aws_iam_role" "wireguard_bootstrap" {
  name        = "${var.deployment}-wireguard-bootstrap"
  description = "Initializes this deployment's WireGuard key document."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

data "aws_iam_policy_document" "wireguard_bootstrap" {
  statement {
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
    ]
    resources = [aws_secretsmanager_secret.wireguard.arn]
  }
}

resource "aws_iam_role_policy" "wireguard_bootstrap" {
  name   = "wireguard-bootstrap"
  role   = aws_iam_role.wireguard_bootstrap.id
  policy = data.aws_iam_policy_document.wireguard_bootstrap.json
}
