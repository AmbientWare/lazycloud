locals {
  workspace_bucket_prefix = "${var.deployment}-workspace"
  workspace_bucket_arn    = "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*"
}

resource "aws_s3_bucket" "storage" {
  for_each = toset(["objects", "deploy", "releases"])
  bucket   = "${var.deployment}-${each.key}-${data.aws_caller_identity.current.account_id}"

  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "storage" {
  for_each = aws_s3_bucket.storage

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "storage" {
  for_each = aws_s3_bucket.storage
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "storage" {
  for_each = aws_s3_bucket.storage
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "storage" {
  for_each = aws_s3_bucket.storage
  bucket   = each.value.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_cors_configuration" "objects" {
  bucket = aws_s3_bucket.storage["objects"].id
  cors_rule {
    allowed_origins = ["https://${data.terraform_remote_state.cloudflare.outputs.records.apex}"]
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 3600
  }
}

resource "aws_iam_role" "workspace_storage" {
  name        = "${var.deployment}-workspace-storage"
  description = "Short-lived sessions restricted to one workspace bucket by the issuer."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.control_plane.arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy" "workspace_storage" {
  name = "workspace-objects"
  role = aws_iam_role.workspace_storage.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
        Resource = local.workspace_bucket_arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
          "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
        ]
        Resource = "${local.workspace_bucket_arn}/*"
      },
    ]
  })
}

resource "aws_vpc_endpoint" "fleet_storage" {
  vpc_id            = aws_vpc.fleet.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.fleet.id]
  tags              = { Name = "${var.deployment}-fleet-storage" }
}

resource "aws_s3_bucket" "storage_access" {
  bucket = "${var.deployment}-storage-access-${data.aws_caller_identity.current.account_id}"
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "storage_access" {
  bucket                  = aws_s3_bucket.storage_access.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  rule {
    id     = "expire-delivered-access-logs"
    status = "Enabled"
    filter { prefix = "access/" }
    expiration { days = 30 }
  }
}

resource "aws_s3_bucket_policy" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "logging.s3.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.storage_access.arn}/access/*"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = [aws_s3_bucket.storage["objects"].arn, local.workspace_bucket_arn] }
      }
    }]
  })
}

resource "aws_s3_bucket_logging" "objects" {
  bucket        = aws_s3_bucket.storage["objects"].id
  target_bucket = aws_s3_bucket.storage_access.id
  target_prefix = "access/"
  depends_on    = [aws_s3_bucket_policy.storage_access, aws_s3_bucket_notification.storage_access]
}

resource "aws_sqs_queue" "storage_access_dead_letters" {
  name                      = "${var.deployment}-storage-access-dead-letters"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "storage_access" {
  name                       = "${var.deployment}-storage-access"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 120
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.storage_access_dead_letters.arn
    maxReceiveCount     = 10
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "storage_access" {
  queue_url = aws_sqs_queue.storage_access_dead_letters.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.storage_access.arn]
  })
}

resource "aws_sqs_queue_policy" "storage_access" {
  queue_url = aws_sqs_queue.storage_access.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.storage_access.arn
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.storage_access.arn }
      }
    }]
  })
}

resource "aws_s3_bucket_notification" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  queue {
    queue_arn     = aws_sqs_queue.storage_access.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "access/"
  }
  depends_on = [aws_sqs_queue_policy.storage_access]
}
