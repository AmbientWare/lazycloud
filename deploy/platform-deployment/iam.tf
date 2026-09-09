data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  arn_prefix = "arn:${data.aws_partition.current.partition}"

}

# The identity the control plane and scheduler pods run as. It is also the
# principal the connected-AWS control role trusts, so its ARN belongs in the
# control stack's TrustedPrincipalArns.
#
# Held by the workload rather than by the node it happens to land on. Which
# service accounts may hold it is an association declared beside the cluster, not
# a condition in this policy: a wildcard there would let any pod in the namespace
# reach every workspace bucket.
resource "aws_iam_role" "control_plane" {
  name        = "${var.deployment}-control-plane"
  description = "Identity of the LazyCloud control plane and scheduler workloads."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

data "aws_iam_policy_document" "control_plane" {
  statement {
    sid       = "IssueWorkspaceStorageCredentials"
    actions   = ["sts:AssumeRole", "sts:TagSession"]
    resources = [aws_iam_role.workspace_storage.arn]
  }

  statement {
    sid = "ManageWorkspaceBuckets"
    actions = [
      "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucketLocation", "s3:ListBucket",
      "s3:ListBucketMultipartUploads", "s3:GetBucketPolicy", "s3:PutBucketPolicy",
      "s3:PutBucketCORS", "s3:PutLifecycleConfiguration",
      "s3:GetBucketLogging", "s3:PutBucketLogging",
    ]
    resources = [local.workspace_bucket_arn]
  }

  statement {
    sid       = "ReadStorageAccessLogs"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.storage_access.arn}/access/*"]
  }

  statement {
    sid       = "ConsumeStorageAccessDeliveries"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.storage_access.arn]
  }

  statement {
    sid       = "ReadApplicationBucket"
    actions   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"]
    resources = [aws_s3_bucket.storage["objects"].arn]
  }

  statement {
    sid = "ManageApplicationAndWorkspaceObjects"
    actions = [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
      "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.storage["objects"].arn}/*", "${local.workspace_bucket_arn}/*"]
  }

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
    resources = local.ecr_repository_arns
  }

  statement {
    sid = "PublishAndReadWorkloadImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchDeleteImage",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [local.workload_image_repository_arn]
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.platform.arn, aws_secretsmanager_secret.operator.arn]
  }

}

resource "aws_iam_role_policy" "control_plane" {
  name   = "control-plane"
  role   = aws_iam_role.control_plane.name
  policy = data.aws_iam_policy_document.control_plane.json
}
