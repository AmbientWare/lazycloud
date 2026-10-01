data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  arn_prefix = "arn:${data.aws_partition.current.partition}"
  account_id = data.aws_caller_identity.current.account_id
  # Every instance the fleet launches carries lazycloud:fleet=<this>, in this
  # account and in connected ones; launch and terminate rights hang on it.
  fleet_name = var.deployment
}

# The identity the server and scheduler pods run as, through Pod Identity
# (identity.tf). It is the principal customer connection roles trust
# (LAZYCLOUD_AWS_PRINCIPAL_ARN), so its name is an external contract once a
# customer connects.
resource "aws_iam_role" "control_plane" {
  name        = "${var.deployment}-control-plane"
  description = "Identity of the LazyCloud server and scheduler workloads."

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
  source_policy_documents = [data.aws_iam_policy_document.reference_control_plane.json]

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

  # The server sets the dashboard's CORS rule on the objects bucket at start.
  statement {
    sid       = "ReadApplicationBucket"
    actions   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutBucketCORS"]
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
    sid       = "AuthenticateToRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Builds push images, caches and filesystem images under the workload
  # image repository's path; platform-core's creation template makes each
  # repository on first push.
  statement {
    sid = "PublishAndReadWorkloadImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchDeleteImage",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:CreateRepository",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [local.workload_image_repository_arn, "${local.workload_image_repository_arn}/*"]
  }

  # Fleet instances in this account, by RunInstances with the host id as
  # client token. Launches must carry the fleet tag; terminations reach only
  # tagged instances.
  statement {
    sid = "DescribeFleet"
    actions = [
      "ec2:DescribeInstances", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
      "ec2:DescribeAvailabilityZones", "ec2:DescribeImages", "ec2:DescribeSpotPriceHistory",
      "ssm:GetParameters",
    ]
    resources = ["*"]
  }

  statement {
    sid     = "LaunchTaggedFleetHosts"
    actions = ["ec2:RunInstances", "ec2:CreateTags"]
    resources = [
      "${local.arn_prefix}:ec2:*:${local.account_id}:instance/*",
      "${local.arn_prefix}:ec2:*:${local.account_id}:volume/*",
      "${local.arn_prefix}:ec2:*:${local.account_id}:network-interface/*",
      "${local.arn_prefix}:ec2:*:${local.account_id}:spot-instances-request/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/lazycloud:fleet"
      values   = [local.fleet_name]
    }
  }

  statement {
    sid     = "LaunchInFleetNetworks"
    actions = ["ec2:RunInstances"]
    resources = [
      "${local.arn_prefix}:ec2:*:${local.account_id}:subnet/*",
      "${local.arn_prefix}:ec2:*:${local.account_id}:security-group/*",
      "${local.arn_prefix}:ec2:*::image/*",
    ]
  }

  statement {
    sid       = "TerminateFleetHosts"
    actions   = ["ec2:TerminateInstances"]
    resources = ["${local.arn_prefix}:ec2:*:${local.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/lazycloud:fleet"
      values   = [local.fleet_name]
    }
  }

  statement {
    sid       = "PassFleetNodeRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.fleet_node.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  statement {
    sid       = "SpotServiceRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["spot.amazonaws.com"]
    }
  }

  # Customer connection roles, with their external id. Not narrowed to a
  # role name: existing-role connections name any role, and the validation
  # probe proves the customer's side enforces the external id.
  statement {
    sid       = "AssumeCustomerConnectionRoles"
    actions   = ["sts:AssumeRole"]
    resources = ["${local.arn_prefix}:iam::*:role/*"]
  }
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "control-plane"
  role   = aws_iam_role.control_plane.name
  policy = data.aws_iam_policy_document.control_plane.json
}
