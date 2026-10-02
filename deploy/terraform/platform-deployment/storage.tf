# objects holds sources, artifacts and build contexts; deploy holds the
# chart values. Workspace buckets (<prefix>-<workspace id>) hold volumes and
# disks; the server creates them, so they stay outside Terraform.
locals {
  workspace_bucket_prefix = "${var.deployment}-workspace"
  workspace_bucket_arn    = "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*"
}

resource "aws_s3_bucket" "storage" {
  for_each = toset(["objects", "deploy"])
  bucket   = "${var.deployment}-${each.key}-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "storage" {
  for_each                = aws_s3_bucket.storage
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "storage" {
  for_each = aws_s3_bucket.storage
  bucket   = each.value.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# What a host's grant assumes. The server narrows each session to one
# workspace bucket's volumes/ and disks/ with a session policy
# (storage.hostPolicy); this role bounds every session to workspace buckets.
resource "aws_iam_role" "workspace_storage" {
  name = "${var.deployment}-workspace-storage"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { AWS = aws_iam_role.control_plane.arn }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "workspace_storage" {
  name = "workspace-objects"
  role = aws_iam_role.workspace_storage.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketMultipartUploads"], Resource = local.workspace_bucket_arn },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.workspace_bucket_arn}/*"
      },
    ]
  })
}
