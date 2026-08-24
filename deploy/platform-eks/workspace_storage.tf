# The role a workspace's storage credential is cut from.
#
# Separate from the control plane's own role because of what a session policy
# can and cannot do: it narrows, never widens. The control plane holds broad
# access so it can create and reconcile every workspace bucket, and a credential
# cut from that role would inherit the breadth even when scoped, because the
# ceiling it intersects with is already everything.
#
# This role holds the same breadth for a different reason: which workspaces
# exist is not known until customers do, so it is scoped by prefix. What makes a
# worker's credential specific is the session policy naming one bucket, and this
# role is the ceiling that policy cuts down from.
resource "aws_iam_role" "workspace_storage" {
  name        = "${var.deployment}-workspace-storage"
  description = "Cut per-workspace, bucket-scoped credentials for container mounts."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.control_plane.arn }
      # `sts:TagSession` is not optional. A Pod Identity session carries tags, so
      # a trust granting only the assume refuses the call in terms of the
      # tagging, which is the same reason it appears on `control_principal`.
      Action = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

data "aws_iam_policy_document" "workspace_storage" {
  statement {
    sid = "WorkspaceBuckets"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
      "s3:GetBucketLocation",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:PutObject",
    ]
    resources = [
      "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*",
      "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*/*",
    ]
  }
}

resource "aws_iam_role_policy" "workspace_storage" {
  name   = "workspace-storage"
  role   = aws_iam_role.workspace_storage.id
  policy = data.aws_iam_policy_document.workspace_storage.json
}
