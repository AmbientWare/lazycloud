data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  arn_prefix = "arn:${data.aws_partition.current.partition}"
}

# The identity the control plane and scheduler processes run as. It is also the
# principal the connected-AWS control role trusts, so its ARN belongs in the
# control stack's TrustedPrincipalArns.
resource "aws_iam_role" "control_plane" {
  name        = "${var.deployment}-control-plane"
  description = "Identity of the LazyCloud control plane host."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "${var.deployment}-control-plane"
  role = aws_iam_role.control_plane.name
}

# Session Manager, so operators reach the host without an inbound rule or a key
# pair, and so CI can run the redeploy command without holding SSH credentials.
resource "aws_iam_role_policy_attachment" "control_plane_ssm" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "control_plane" {
  statement {
    sid       = "PullControlPlaneImages"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "ReadOwnRepositories"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }

  statement {
    sid       = "ReadDeploymentBundle"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"]
    resources = [aws_s3_bucket.deploy.arn, "${aws_s3_bucket.deploy.arn}/*"]
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [for secret in aws_secretsmanager_secret.runtime : secret.arn]
  }

  # A bucket per workspace, created on demand by `control.service.create_workspace_storage`.
  # The name is `workspace-<uuid>`, which is why this is scoped by prefix rather
  # than enumerated: the set is not known until customers exist.
  statement {
    sid = "OwnPlatformAndWorkspaceBuckets"
    actions = [
      "s3:CreateBucket",
      "s3:DeleteObject",
      "s3:GetBucketLocation",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = concat(
      [for bucket in aws_s3_bucket.objects : bucket.arn],
      [for bucket in aws_s3_bucket.objects : "${bucket.arn}/*"],
      ["${local.arn_prefix}:s3:::workspace-*", "${local.arn_prefix}:s3:::workspace-*/*"],
    )
  }
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "control-plane"
  role   = aws_iam_role.control_plane.name
  policy = data.aws_iam_policy_document.control_plane.json
}
